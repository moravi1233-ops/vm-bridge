"""Read-only VMware inventory discovery. No VM lifecycle or VMX writes."""
from __future__ import annotations
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .storage import home, read_json, write_json, settings, save_settings, locked_configuration

VMX_KEY = re.compile(r'^\s*([\w.:-]+)\s*=\s*"(.*)"\s*$', re.M)

def parse_vmx(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return {k.lower(): v.replace('\\\\', '\\').replace('\\\"', '"') for k, v in VMX_KEY.findall(text)}

def vmware_binary(name="vmrun.exe"):
    found = shutil.which(name)
    candidates = [found] if found else []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for flags in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\VMware, Inc.\VMware Workstation",
                                        0, winreg.KEY_READ | flags) as key:
                        directory = winreg.QueryValueEx(key, "InstallPath")[0]
                        candidates.append(str(Path(directory) / name))
                except OSError:
                    pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(str(Path(base) / "VMware" / "VMware Workstation" / name))
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)

def running_paths():
    binary = vmware_binary()
    if not binary:
        return [], "VMware vmrun was not found"
    run = subprocess.run([binary, "list"], capture_output=True, timeout=10,
                         encoding="utf-8", errors="replace", creationflags=0x08000000)
    if run.returncode:
        return [], run.stdout.strip() or run.stderr.strip()
    return [str(Path(x.strip()).resolve()) for x in run.stdout.splitlines()
            if x.strip().lower().endswith(".vmx")], None

def walk_vmx(root: Path, max_depth=4):
    if not root.is_dir():
        return
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if len(relative.parts) >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for file in files:
            count += 1
            if count > 15000:
                return
            if file.lower().endswith(".vmx"):
                yield Path(directory) / file

def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))

def discover():
    config = settings()
    running, warning = running_paths()
    running_set = {_canonical(p) for p in running}
    paths = set(running + config["paths"])
    inventory = Path(os.environ.get("APPDATA", "")) / "VMware" / "inventory.vmls"
    if inventory.is_file():
        data = parse_vmx(inventory)
        paths.update(v for k, v in data.items() if k.endswith(".config") and v.lower().endswith(".vmx"))
    for root in [Path.home() / "Documents" / "Virtual Machines",
                 *(Path(p) for p in config["roots"])]:
        paths.update(str(p.resolve()) for p in walk_vmx(root))
    records = []
    for filename in sorted(paths):
        path = Path(filename)
        if not path.is_file():
            continue
        try:
            data = parse_vmx(path)
            canonical = _canonical(path)
            identity = data.get("uuid.bios", "")
            # First boot may create uuid.bios; keep the connection profile stable.
            vm_id = hashlib.sha256((os.environ.get("COMPUTERNAME", "") + "|" +
                                    canonical).encode()).hexdigest()[:20]
            profile = config["profiles"].get(vm_id, {})
            enabled = data.get("remotedisplay.vnc.enabled", "").lower() == "true"
            port_text = data.get("remotedisplay.vnc.port", "5900")
            port = int(port_text) if port_text.isdigit() else 0
            console = {"status": "configured" if enabled and 0 < port < 65536 else "not_configured",
                       "host": "127.0.0.1", "port": port,
                       "password_configured": bool(data.get("remotedisplay.vnc.password")),
                       "binding": data.get("remotedisplay.vnc.ip", "all_interfaces")}
            if profile.get("vnc"):
                console.update(profile["vnc"])
                console["status"] = "configured"
            records.append({"id": vm_id, "name": data.get("displayname", path.stem),
                "provider": "vmware", "path": str(path.resolve()), "uuid": identity,
                "power": "running" if canonical in running_set else "stopped",
                "guest_os_hint": data.get("guestos", "unknown"),
                "console": console, "terminal": {"status": "configured" if profile.get("ssh") else "not_configured"},
                "ssh": profile.get("ssh"), "identity_warning": None})
        except (OSError, ValueError):
            continue
    uuids = {}
    for item in records:
        if item["uuid"]:
            uuids.setdefault(item["uuid"], []).append(item)
    for group in uuids.values():
        if len(group) > 1:
            for item in group:
                item["identity_warning"] = "duplicate_guest_uuid; use the unique path-bound id"
    write_json(home() / "inventory.json", {"vms": records, "warning": warning})
    return {"vms": records, "warning": warning, "vmware": vmware_binary()}

def resolve(target: str, refresh=True):
    data = discover() if refresh else read_json(home() / "inventory.json", {"vms": []})
    matches = [v for v in data["vms"] if v["id"] == target or v["name"] == target or _canonical(v["path"]) == _canonical(target)]
    if len(matches) != 1:
        raise ValueError("VM not found or name is ambiguous; discover and use its exact id")
    return matches[0]

@locked_configuration
def add_path(path: str):
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != ".vmx":
        raise ValueError("Provide an existing .vmx file")
    config = settings()
    config["paths"] = sorted(set(config["paths"] + [str(path)]))
    save_settings(config)
    return discover()

@locked_configuration
def add_root(path: str):
    path = Path(path).resolve()
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("Provide an existing VM folder, not a drive root")
    config = settings()
    config["roots"] = sorted(set(config["roots"] + [str(path)]))
    save_settings(config)
    return discover()

@locked_configuration
def configure(target: str, ssh=None, vnc=None):
    vm = resolve(target)
    config = settings()
    profile = config["profiles"].setdefault(vm["id"], {})
    if ssh is not None:
        host = ssh.get("host", "")
        if not re.fullmatch(r"[\w.:[\]-]+", host) or host.startswith("-"):
            raise ValueError("Invalid SSH hostname or config alias")
        if not 1 <= int(ssh.get("port", 22)) <= 65535:
            raise ValueError("Invalid SSH port")
        profile["ssh"] = ssh
    if vnc is not None:
        if not 1 <= int(vnc["port"]) <= 65535:
            raise ValueError("Invalid VNC port")
        if vnc.get("host", "127.0.0.1") not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Use a loopback VNC endpoint; tunnel remote endpoints first")
        profile["vnc"] = vnc
    save_settings(config)
    return resolve(vm["id"])

