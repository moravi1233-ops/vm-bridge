"""Reviewable, stopped-VM-only console provisioning. Never changes power state."""
from __future__ import annotations
import hashlib
import re
import secrets
import socket
import time
import uuid
import os
import tempfile
from pathlib import Path

from . import registry
from .storage import home, read_json, write_json, seal, settings, save_settings, locked_configuration

def digest(data):
    return hashlib.sha256(data).hexdigest()

def _available(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

@locked_configuration
def plan(target, port=None):
    vm = registry.resolve(target)
    data = registry.discover()
    used = {v["console"]["port"] for v in data["vms"] if v["console"]["status"] == "configured"}
    for path in (home() / "plans").glob("*.json"):
        pending = read_json(path)
        if pending and not pending.get("applied") and time.time() - pending["created"] < 86400:
            used.add(pending["port"])
    if port is None:
        port = next((p for p in range(15900, 16900) if p not in used and _available(p)), None)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Choose a console port between 1024 and 65535")
    if port in used and port != vm["console"]["port"]:
        raise ValueError("Port already assigned to a discovered VM")
    identifier = str(uuid.uuid4())
    result = {"id": identifier, "vm_id": vm["id"], "vm_name": vm["name"],
              "path": vm["path"], "sha256": digest(Path(vm["path"]).read_bytes()),
              "created": time.time(), "power": vm["power"], "port": port,
              "changes": {"RemoteDisplay.vnc.enabled": "TRUE", "RemoteDisplay.vnc.ip": "127.0.0.1",
                          "RemoteDisplay.vnc.port": str(port),
                          "RemoteDisplay.vnc.password": "<random eight-character secret generated on apply>"},
              "requires_stopped_vm": True, "starts_vm": False}
    path = home() / "plans" / (identifier + ".json")
    write_json(path, result)
    return {**result, "plan_file": str(path)}

@locked_configuration
def apply(plan_id, expected_hash):
    uuid.UUID(plan_id)
    path = home() / "plans" / (plan_id + ".json")
    proposed = read_json(path)
    if not proposed or proposed.get("applied"):
        raise ValueError("Unknown or already applied plan")
    if time.time() - proposed["created"] > 86400:
        raise ValueError("Plan expired; prepare it again")
    vm = registry.resolve(proposed["vm_id"])
    running, error = registry.running_paths()
    if error:
        raise RuntimeError("Cannot verify VM power state: " + error)
    if registry._canonical(vm["path"]) in {registry._canonical(p) for p in running}:
        raise ValueError("VM is running; leave it running and apply during a requested shutdown")
    vmx = Path(vm["path"])
    if list(vmx.parent.glob(vmx.name + ".lck")):
        raise ValueError("VM configuration is locked; retry after VMware releases it")
    original = vmx.read_bytes()
    if digest(original) != proposed["sha256"] or expected_hash != proposed["sha256"]:
        raise ValueError("Configuration changed or review hash mismatched; prepare a new plan")
    if not _available(proposed["port"]):
        raise ValueError("The planned console port is no longer available")
    if any(other["id"] != vm["id"] and other["console"]["status"] == "configured"
           and other["console"]["port"] == proposed["port"] for other in registry.discover()["vms"]):
        raise ValueError("The planned console port has been assigned to another VM")
    password = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789") for _ in range(8))
    changes = dict(proposed["changes"])
    changes["RemoteDisplay.vnc.password"] = password
    content = original.decode("utf-8-sig")
    keys = {k.lower() for k in changes} | {"remotedisplay.vnc.key"}
    lines = [line for line in content.splitlines()
             if not ((match := re.match(r"\s*([\w.:-]+)\s*=", line)) and match[1].lower() in keys)]
    lines += [f'{key} = "{value}"' for key, value in changes.items()]
    backup = home() / "backups" / (plan_id + ".vmx")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb") as stream:
        stream.write(original)
    # Publish the complete VMX atomically; never leave a half-written file.
    descriptor, temporary = tempfile.mkstemp(prefix=".vmbridge-", dir=vmx.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(("\n".join(lines) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if digest(vmx.read_bytes()) != proposed["sha256"]:
            raise ValueError("VMX changed before commit; prepare a new plan")
        live, error = registry.running_paths()
        if error or registry._canonical(vmx) in {registry._canonical(p) for p in live} or list(vmx.parent.glob(vmx.name + ".lck")):
            raise ValueError("VM started or became locked before commit; configuration was preserved")
        os.replace(temporary, vmx)
    finally:
        Path(temporary).unlink(missing_ok=True)
    config = settings()
    profile = config["profiles"].setdefault(vm["id"], {})
    profile["console_secret"] = seal(password)
    save_settings(config)
    proposed.update(applied=time.time(), backup=str(backup), applied_sha256=digest(vmx.read_bytes()))
    write_json(path, proposed)
    return {"applied": True, "vm_id": vm["id"], "backup": str(backup),
            "port": proposed["port"], "binding": "127.0.0.1", "power": "stopped",
            "credential_storage": "Windows DPAPI; VMware VMX contains its VNC credential",
            "restart_required": "next normal VM start; this tool does not start it"}
