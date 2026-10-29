from __future__ import annotations
import json
import os
import secrets
import time
import uuid
from contextvars import ContextVar
from multiprocessing.connection import Client
from pathlib import Path

from .storage import home, read_json, write_json, seal, unseal, settings
from .registry import resolve

OWNER = ContextVar("vmbridge_owner", default=None)

def owner():
    value = OWNER.get() or os.environ.get("CODEX_THREAD_ID") or os.environ.get("VMBRIDGE_OWNER")
    if not value:
        raise ValueError("Set VMBRIDGE_OWNER to a stable per-conversation UUID, or use CODEX_THREAD_ID")
    return value

def session_path(session_id):
    uuid.UUID(session_id)
    return home() / "sessions" / session_id / "state.json"

def states():
    folder = home() / "sessions"
    result = []
    for path in folder.glob("*/state.json"):
        state = read_json(path)
        if state:
            if state.get("status") == "ready" and worker_presence(state) is False:
                state.update(status="closed", password=None, error="worker_exited")
                write_json(path, state)
            result.append(state)
    return result

def worker_presence(state):
    """True/False/None: never treat access denied or query failure as proven dead."""
    if not state.get("pid") or not state.get("process_created"): return False
    try:
        import win32api, win32con, win32process
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, state["pid"])
        try:
            if win32process.GetExitCodeProcess(handle) != 259:
                return False
            created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
            return abs(created - state.get("process_created", -1)) < 0.01
        finally:
            handle.Close()
    except Exception as exc:
        if getattr(exc,"winerror",None) == 87: return False
        return None

def worker_alive(state):
    return worker_presence(state) is True

def public(state):
    fields = ("id", "vm_id", "vm_name", "owner", "status", "pid", "created", "last_used", "job_id", "error")
    return {k: state.get(k) for k in fields if k in state}

def request(session_id, operation, **payload):
    state = read_json(session_path(session_id))
    if not state:
        raise ValueError("Unknown session")
    if state["owner"] != owner():
        raise PermissionError("This VM session belongs to another chat")
    if state["status"] not in {"ready", "starting"}:
        raise ConnectionError(f"Session is {state['status']}; open a new connection")
    from .storage import unseal
    connection = Client(state["pipe"], family="AF_PIPE",
                        authkey=bytes.fromhex(unseal(state["auth"])))
    try:
        connection.send_bytes(json.dumps({"op": operation, **payload}).encode())
        if not connection.poll(15):
            raise TimeoutError("VM worker did not respond; observe before retrying input")
        result = json.loads(connection.recv_bytes(1024 * 1024))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "VM operation failed"))
        return result["result"]
    finally:
        connection.close()

def _launch(state_file, module="vm_bridge.worker", label="VM Bridge console"):
    from .lifecycle import launch
    return launch(state_file,module,label,owner())

def connect(target, password_env=None, encoding="raw", idle_seconds=180):
    from .image_cache import cleanup
    from .memory import prune_history
    cleanup()
    prune_history()
    vm = resolve(target)
    if vm["power"] != "running":
        raise ValueError("The VM is stopped; this tool does not power VMs on automatically")
    endpoint = vm["console"]
    if endpoint["status"] != "configured":
        raise ValueError("Console not configured. Use prepare-console to review a change for this VM.")
    if not 30 <= idle_seconds <= 1800:
        raise ValueError("Idle timeout must be 30 to 1800 seconds")
    for state in states():
        if state.get("vm_id") == vm["id"] and state.get("status") == "ready":
            if state["owner"] == owner():
                try:
                    return request(state["id"], "status")
                except (OSError, EOFError, ConnectionError):
                    pass
            else:
                if worker_alive(state):
                    raise PermissionError("The VM is controlled by another chat")
    session_id = str(uuid.uuid4())
    state_file = session_path(session_id)
    password = None
    stored = settings()["profiles"].get(vm["id"], {}).get("console_secret")
    if stored:
        password = unseal(stored)
    if password_env:
        password = os.environ.get(password_env)
        if password is None:
            raise ValueError("The named password environment variable is not set")
    state = {"id": session_id, "vm_id": vm["id"], "vm_name": vm["name"],
        "owner": owner(), "status": "starting", "created": time.time(),
        "pipe": r"\\.\pipe\vmbridge-" + session_id,
        "auth": seal(secrets.token_hex(32)), "host": endpoint["host"], "port": endpoint["port"],
        "password": seal(password) if password else None, "encoding": encoding,
        "idle_seconds": idle_seconds}
    write_json(state_file, state)
    try:
        job = _launch(state_file)
        for _ in range(100):
            fresh = read_json(state_file)
            if fresh["status"] == "ready":
                fresh["job_id"] = job["id"]
                write_json(state_file, fresh)
                return request(session_id, "status")
            if fresh["status"] == "failed":
                raise RuntimeError(fresh.get("error", "VNC connection failed"))
            time.sleep(0.1)
        raise TimeoutError("The console worker did not become ready")
    except BaseException:
        if "job" in locals():
            from .lifecycle import stop
            stop(job,owner())
        raise

def disconnect(session_id):
    state = read_json(session_path(session_id))
    if state and state["owner"] != owner():
        raise PermissionError("This session belongs to another chat")
    if state and state["status"] == "ready":
        result = request(session_id, "close")
        for _ in range(50):
            state = read_json(session_path(session_id))
            if state["status"] in {"closed", "failed"}:
                return result
            time.sleep(0.1)
        raise TimeoutError("Worker shutdown has not completed")
    return {"closed": True, "session": session_id}

def close_owned():
    result = []
    for state in states():
        if state.get("owner") == owner() and state.get("status") == "ready":
            try:
                result.append(disconnect(state["id"]))
            except Exception as exc:
                result.append({"session": state["id"], "error": str(exc)})
    from .viewer import stop
    for path in (home() / "viewers").glob("*/state.json"):
        state = read_json(path)
        if state.get("owner") == owner() and state.get("status") == "ready":
            try:
                result.append({"viewer": state["id"], **stop(state["id"])})
            except Exception as exc:
                result.append({"viewer": state["id"], "error": str(exc)})
    return result

