"""Choose registered Codex helpers or the bundled Windows Job Object guardian."""
from __future__ import annotations
from contextvars import ContextVar
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

MCP_OWNER_PID = ContextVar("vmbridge_mcp_owner_pid", default=None)

def backend():
    choice = os.environ.get("VMBRIDGE_LIFECYCLE", "auto")
    if choice not in {"auto","registered","bundled"}:
        raise ValueError("VMBRIDGE_LIFECYCLE must be auto, registered or bundled")
    manager = Path(os.environ.get("CODEX_HOME",Path.home()/".codex"))/"skills/windows-process-lifecycle/scripts/process_manager.py"
    if choice == "registered" or (choice == "auto" and os.environ.get("CODEX_THREAD_ID") and manager.is_file()):
        if not manager.is_file(): raise RuntimeError("Registered process manager is not installed")
        return "registered", manager
    return "bundled", Path(__file__).with_name("guardian.py")

def launch(state_file, module, label, owner):
    from .storage import home
    name, manager = backend()
    site_path = str(Path(importlib.util.find_spec("vncdotool").origin).resolve().parents[1])
    bootstrap = "import site,runpy;site.addsitedir("+repr(site_path)+");runpy.run_module("+repr(module)+",run_name='__main__')"
    command = [sys._base_executable,str(manager)]
    if name == "bundled": command += ["--root",str(home()/"helpers")]
    command += ["start","--session",owner,"--label",label+" "+state_file.parent.name,
                "--cwd",str(Path(__file__).resolve().parents[2])]
    pid = MCP_OWNER_PID.get() or os.environ.get("VMBRIDGE_OWNER_PID")
    if pid: command += ["--owner-pid",str(int(pid))]
    command += ["--",sys._base_executable,"-c",bootstrap,"--state-file",str(state_file)]
    result = subprocess.run(command,capture_output=True,text=True,timeout=12,creationflags=0x08000000)
    if result.returncode: raise RuntimeError("Worker launch failed: "+(result.stderr or result.stdout)[-1000:])
    return {**json.loads(result.stdout),"backend":name}

def stop(job, owner):
    from .storage import home
    name, manager = backend()
    if job.get("backend") and job["backend"] != name:
        name = job["backend"]
        manager = Path(__file__).with_name("guardian.py") if name == "bundled" else Path(os.environ.get("CODEX_HOME",Path.home()/".codex"))/"skills/windows-process-lifecycle/scripts/process_manager.py"
    command = [sys._base_executable,str(manager)]
    if name == "bundled": command += ["--root",str(home()/"helpers")]
    command += ["cleanup","--session",owner,"--id",job["id"]]
    subprocess.run(command,capture_output=True,timeout=8,creationflags=0x08000000,check=True)
