"""VM Bridge portable temporary process guardian (MIT).

Own only the tool process trees started through this module.

No resident service. Each managed background task has one temporary guardian.
The guardian owns a Windows kill-on-close job containing only the task it starts.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

if os.name != "nt":
    raise SystemExit("This manager requires Windows.")

K = C.WinDLL("kernel32", use_last_error=True)
HANDLE = W.HANDLE
SIZE = C.c_size_t


class BasicLimits(C.Structure):
    _fields_ = [("ProcessTime", C.c_int64), ("JobTime", C.c_int64),
                ("Flags", W.DWORD), ("MinWorkingSet", SIZE),
                ("MaxWorkingSet", SIZE), ("ActiveProcesses", W.DWORD),
                ("Affinity", SIZE), ("Priority", W.DWORD), ("Scheduling", W.DWORD)]


class IoCounters(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in
                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class ExtendedLimits(C.Structure):
    _fields_ = [("Basic", BasicLimits), ("Io", IoCounters),
                ("ProcessMemory", SIZE), ("JobMemory", SIZE),
                ("PeakProcessMemory", SIZE), ("PeakJobMemory", SIZE)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR),
                ("desktop", W.LPWSTR), ("title", W.LPWSTR),
                *[(n, W.DWORD) for n in ("x", "y", "width", "height", "charsX", "charsY", "fill", "flags")],
                ("show", W.WORD), ("reservedSize", W.WORD),
                ("reservedBytes", C.POINTER(W.BYTE)),
                ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE),
                ("pid", W.DWORD), ("tid", W.DWORD)]


class ProcessEntry(C.Structure):
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                ("heap", SIZE), ("module", W.DWORD), ("threads", W.DWORD),
                ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                ("exe", W.WCHAR * 260)]


def bind(name, args, result):
    fn = getattr(K, name)
    fn.argtypes, fn.restype = args, result
    return fn


close = bind("CloseHandle", [HANDLE], W.BOOL)
open_process = bind("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], HANDLE)
wait = bind("WaitForSingleObject", [HANDLE, W.DWORD], W.DWORD)
terminate = bind("TerminateProcess", [HANDLE, W.UINT], W.BOOL)
times = bind("GetProcessTimes", [HANDLE, *([C.POINTER(W.FILETIME)] * 4)], W.BOOL)
image_name = bind("QueryFullProcessImageNameW", [HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL)
create_job = bind("CreateJobObjectW", [C.c_void_p, W.LPCWSTR], HANDLE)
set_job = bind("SetInformationJobObject", [HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)
assign_job = bind("AssignProcessToJobObject", [HANDLE, HANDLE], W.BOOL)
current_process = bind("GetCurrentProcess", [], HANDLE)
terminate_job = bind("TerminateJobObject", [HANDLE, W.UINT], W.BOOL)
resume = bind("ResumeThread", [HANDLE], W.DWORD)
create_process = bind("CreateProcessW", [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p,
                      W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR,
                      C.POINTER(StartupInfo), C.POINTER(ProcessInfo)], W.BOOL)
snapshot = bind("CreateToolhelp32Snapshot", [W.DWORD, W.DWORD], HANDLE)
first_process = bind("Process32FirstW", [HANDLE, C.POINTER(ProcessEntry)], W.BOOL)
next_process = bind("Process32NextW", [HANDLE, C.POINTER(ProcessEntry)], W.BOOL)

RUNTIMES = {"node.exe", "python.exe", "pythonw.exe", "pwsh.exe", "powershell.exe",
            "cmd.exe", "uv.exe", "uvx.exe", "deno.exe", "bun.exe", "dotnet.exe"}
ACTIVE = {"starting", "running"}


def checked(ok):
    if not ok:
        raise C.WinError(C.get_last_error())
    return ok


def creation(handle):
    values = [W.FILETIME() for _ in range(4)]
    checked(times(handle, *(C.byref(v) for v in values)))
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def identity(pid):
    h = open_process(0x1000 | 0x100000, False, pid)
    if not h:
        raise ProcessLookupError(pid)
    try:
        if wait(h, 0) == 0:
            raise ProcessLookupError(pid)
        buf, length = C.create_unicode_buffer(32768), W.DWORD(32768)
        checked(image_name(h, 0, buf, C.byref(length)))
        return {"pid": pid, "created": creation(h), "exe": buf.value}
    finally:
        close(h)


def exact_handle(record, allow_terminate=False):
    rights = 0x1000 | 0x100000 | (1 if allow_terminate else 0)
    h = open_process(rights, False, int(record["pid"]))
    if not h:
        return None
    try:
        if wait(h, 0) == 0:
            close(h)
            return None
        if creation(h) != int(record["created"]):
            close(h)
            return None
        buf, length = C.create_unicode_buffer(32768), W.DWORD(32768)
        if not image_name(h, 0, buf, C.byref(length)):
            # A process can terminate between the creation-time and image queries.
            if wait(h, 0) == 0:
                close(h)
                return None
            raise C.WinError(C.get_last_error())
        if os.path.normcase(buf.value) != os.path.normcase(record["exe"]):
            close(h)
            return None
        return h
    except Exception:
        close(h)
        raise


def owner_identity(explicit=None):
    if explicit:
        return identity(explicit)
    h = checked(snapshot(2, 0))
    if h == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    table = {}
    try:
        entry = ProcessEntry()
        entry.size = C.sizeof(entry)
        more = first_process(h, C.byref(entry))
        while more:
            table[entry.pid] = (entry.parent, entry.exe.lower())
            more = next_process(h, C.byref(entry))
    finally:
        close(h)
    cursor, seen = os.getppid(), set()
    for _ in range(30):
        if cursor in seen or cursor not in table:
            break
        seen.add(cursor)
        parent, exe = table[cursor]
        if exe in {"codex.exe", "chatgpt.exe"}:
            return identity(cursor)
        cursor = parent
    raise RuntimeError("No stable owner found; supply --owner-pid with a long-lived application or terminal PID.")


def save(path, data):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.01)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def emit(data):
    print(json.dumps(data, ensure_ascii=False))


def session_arg(value):
    result = value or os.environ.get("CODEX_THREAD_ID") or os.environ.get("VMBRIDGE_OWNER")
    if not result:
        raise ValueError("A session id is required; supply --session.")
    return result


def worker(root, job_id):
    import msvcrt
    folder = root / str(uuid.UUID(job_id))
    request_file = folder / "request.json"
    request = load(request_file)
    record = {k: request[k] for k in ("id", "session", "lifetime", "label", "token")}
    record.update(status="starting", worker=identity(os.getpid()), launched_at=time.time())
    job, owner, pi = None, None, ProcessInfo()
    try:
        owner = exact_handle(request["owner"])
        if not owner or wait(owner, 0) != 258:
            raise RuntimeError("The owning application has exited.")
        job = checked(create_job(None, None))
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(set_job(job, 9, C.byref(limits), C.sizeof(limits)))
        # Put the dedicated guardian in the job before it creates anything.
        # Children inherit membership from birth, including during startup.
        checked(assign_job(job, current_process()))
        with open(os.devnull, "rb") as inp, open(folder / "stdout.log", "ab") as out, open(folder / "stderr.log", "ab") as err:
            startup = StartupInfo()
            startup.cb, startup.flags = C.sizeof(startup), 0x100
            handles = [msvcrt.get_osfhandle(f.fileno()) for f in (inp, out, err)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup.stdin, startup.stdout, startup.stderr = handles
            command = C.create_unicode_buffer(subprocess.list2cmdline(request["argv"]))
            checked(create_process(request["argv"][0], command, None, None, True,
                                   0x08000000 | 0x4, None, request["cwd"],
                                   C.byref(startup), C.byref(pi)))
            record.update(status="running", child=identity(pi.pid))
            save(folder / "state.json", record)
            request_file.unlink()  # Do not retain launch arguments in the registry.
            if resume(pi.thread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            close(pi.thread)
            pi.thread = None
            reason = "completed"
            while wait(pi.process, 200) == 258:
                marker = folder / "stop.json"
                if marker.exists() and load(marker).get("token") == record["token"]:
                    reason = "stopped"
                    break
                if wait(owner, 0) != 258:
                    reason = "owner-exited"
                    break
            record["status"] = reason
    except Exception as exc:
        record.update(status="error", error=str(exc))
    finally:
        for handle in (pi.thread, pi.process, owner):
            if handle:
                close(handle)
        record["ended_at"] = time.time()
        save(folder / "state.json", record)
        if request_file.exists():
            request_file.unlink()
        # Last operation: also terminates this guardian and any remaining children.
        if job:
            close(job)


def start(args, root):
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv:
        raise ValueError("Supply a tool executable after --.")
    exe = shutil.which(argv[0])
    if not exe:
        raise ValueError("Executable not found.")
    exe = str(Path(exe).resolve())
    if Path(exe).name.lower() not in RUNTIMES:
        raise ValueError("Only temporary CLI runtimes are allowed, never Word, VMs, or desktop applications.")
    argv[0] = exe
    folder = root / str(uuid.uuid4())
    folder.mkdir(parents=True)
    record = dict(id=folder.name, session=session_arg(args.session), label=args.label,
                  lifetime=args.lifetime, token=uuid.uuid4().hex,
                  argv=argv, cwd=str(Path(args.cwd or os.getcwd()).resolve()),
                  owner=owner_identity(args.owner_pid))
    save(folder / "request.json", record)
    guard = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root),
                              "worker", "--id", folder.name], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=0x08000000 | 0x8)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if (folder / "state.json").exists():
            state = load(folder / "state.json")
            if state["status"] == "error":
                raise RuntimeError(state["error"])
            return {k: state.get(k) for k in ("id", "session", "lifetime", "label", "status", "child")}
        if guard.poll() is not None:
            raise RuntimeError("Guardian exited before starting the helper.")
        time.sleep(.05)
    # A timed-out launch must not leave an untracked guardian running.
    save(folder / "stop.json", {"token": record["token"]})
    if guard.poll() is None:
        guard.terminate()
        guard.wait(timeout=3)
    raise RuntimeError("Guardian startup timed out; stop was requested.")


def records(root):
    for path in root.glob("*/state.json"):
        try:
            record = load(path)
            if record["id"] == path.parent.name:
                yield path.parent, record
        except (OSError, ValueError, KeyError):
            continue


def stop_one(folder, record):
    if record["status"] not in ACTIVE:
        return "already-finished"
    h = exact_handle(record["worker"], allow_terminate=True)
    if not h:
        return "guardian-already-exited"
    try:
        save(folder / "stop.json", {"token": record["token"]})
        if wait(h, 900) == 258:
            # Only this exact registered guardian; its non-inherited job handle
            # closes on exit, and Windows terminates its owned child tree.
            checked(terminate(h, 0))
            wait(h, 500)
        if wait(h, 0) != 0:
            raise RuntimeError("Guardian did not exit; inspect this managed job.")
        return "stopped"
    finally:
        close(h)


def cleanup(root, session, include_session=False, job_id=None):
    selected = [(folder, rec) for folder, rec in records(root)
                if rec["session"] == session and (not job_id or rec["id"] == job_id)
                and (include_session or rec["lifetime"] != "session")
                and rec["status"] in ACTIVE]
    # Signal together, so an Interrupt hook doesn't wait once per healthy worker.
    for folder, rec in selected:
        save(folder / "stop.json", {"token": rec["token"]})
    return [dict(id=rec["id"], result=stop_one(folder, rec)) for folder, rec in selected]


def status_rows(root, session=None):
    rows = []
    for _, record in records(root):
        if session and record["session"] != session:
            continue
        row = {k: record.get(k) for k in ("id", "session", "label", "lifetime", "status", "child")}
        if row["status"] in ACTIVE:
            try:
                h = exact_handle(record["worker"])
                if not h:
                    row["status"] = "guardian-exited"
                else:
                    close(h)
            except OSError:
                row["status"] = "unverified"
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("VMBRIDGE_HOME", str(Path(os.environ["LOCALAPPDATA"]) / "VMBridge" / "state"))) / "helpers")
    sub = parser.add_subparsers(dest="action", required=True)
    launch = sub.add_parser("start")
    launch.add_argument("--session")
    launch.add_argument("--owner-pid", type=int)
    launch.add_argument("--label", required=True)
    launch.add_argument("--lifetime", choices=["turn", "session"], default="turn")
    launch.add_argument("--cwd")
    launch.add_argument("command", nargs=argparse.REMAINDER)
    status = sub.add_parser("status")
    status.add_argument("--session")
    clean = sub.add_parser("cleanup")
    clean.add_argument("--session")
    clean.add_argument("--include-session", action="store_true")
    clean.add_argument("--id")
    guard = sub.add_parser("worker")
    guard.add_argument("--id", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == "worker":
            worker(root, args.id)
        elif args.action == "start":
            emit(start(args, root))
        elif args.action == "cleanup":
            emit(cleanup(root, session_arg(args.session), args.include_session, args.id))
        else:
            emit(status_rows(root, args.session))
    except Exception as exc:
        emit({"error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
