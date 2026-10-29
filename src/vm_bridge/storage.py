from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager
from functools import wraps
import hashlib

@contextmanager
def configuration_lock():
    import win32event
    name = hashlib.sha256(str(home()).casefold().encode()).hexdigest()[:24]
    handle = win32event.CreateMutex(None, False, "Local\\VMBridgeConfig-" + name)
    acquired = False
    try:
        code = win32event.WaitForSingleObject(handle, 10000)
        if code not in (0, 128):
            raise TimeoutError("Another VM Bridge operation is changing configuration")
        acquired = True
        yield
    finally:
        if acquired: win32event.ReleaseMutex(handle)
        handle.Close()

def locked_configuration(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with configuration_lock():
            return function(*args, **kwargs)
    return wrapped

def home() -> Path:
    root = Path(os.environ.get("VMBRIDGE_HOME", Path(os.environ["LOCALAPPDATA"]) / "VMBridge" / "state"))
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import win32api, win32con, win32security, ntsecuritycon
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        for sid in (user, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(str(root), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    return root

def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default

def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def settings():
    return read_json(home() / "settings.json", {"roots": [], "paths": [], "profiles": {}})

def save_settings(data):
    write_json(home() / "settings.json", data)

def seal(value: str) -> str:
    import win32crypt
    return win32crypt.CryptProtectData(value.encode(), "VMBridge", None, None, None, 0).hex()

def unseal(value: str) -> str:
    import win32crypt
    return win32crypt.CryptUnprotectData(bytes.fromhex(value), None, None, None, 0)[1].decode()

