"""On-demand local noVNC viewer. No services, browser launch or host input."""
from __future__ import annotations
import argparse
import hmac
import json
import logging
import os
import secrets
import socket
import struct
import threading
import time
import uuid
from pathlib import Path

from .storage import home, read_json, write_json, seal, unseal, settings
from . import registry, sessions

class ReadOnlyRFB:
    """Incremental RFB 3.8 client filter: permit negotiation/refresh, drop input."""
    def __init__(self, control=False):
        self.buffer = bytearray()
        self.stage = "version"
        self.control = control
        self.keys = set()
        self.pointer = None

    def release_messages(self):
        data = b"".join(struct.pack("!BBHI", 4, 0, 0, key) for key in self.keys)
        if self.pointer:
            data += struct.pack("!BBHH", 5, 0, *self.pointer)
        self.keys.clear()
        self.pointer = None
        return data

    def feed(self, chunk):
        self.buffer.extend(chunk)
        if len(self.buffer) > 1_048_576:
            raise ValueError("Viewer message too large")
        output = bytearray()
        while self.buffer:
            size, forward = 0, True
            if self.stage == "version":
                size = 12
                if len(self.buffer) < size: break
                if bytes(self.buffer[:12]) != b"RFB 003.008\n":
                    raise ValueError("Read-only viewer requires RFB 3.8")
                self.stage = "security"
            elif self.stage == "security":
                size = 1
                choice = self.buffer[0]
                if choice not in (1, 2): raise ValueError("Unsupported viewer authentication")
                self.stage = "auth" if choice == 2 else "init"
            elif self.stage == "auth":
                size = 16
                if len(self.buffer) < size: break
                self.stage = "init"
            elif self.stage == "init":
                size = 1
                # Force a shared connection so watching cannot evict another client.
                self.buffer[0] = 1
                self.stage = "normal"
            else:
                kind = self.buffer[0]
                if kind == 0: size = 20
                elif kind == 2:
                    if len(self.buffer) < 4: break
                    size = 4 + 4 * struct.unpack("!H", self.buffer[2:4])[0]
                elif kind == 3: size = 10
                elif kind == 4: size, forward = 8, self.control
                elif kind == 5: size, forward = 6, self.control
                elif kind == 6:
                    if len(self.buffer) < 8: break
                    length = struct.unpack("!I", self.buffer[4:8])[0]
                    if length & 0x80000000: length = (1 << 32) - length
                    size, forward = 8 + length, False
                elif kind == 150: size = 10
                elif kind == 248:
                    if len(self.buffer) < 9: break
                    size = 9 + self.buffer[8]
                elif kind == 250: size, forward = 4, False
                elif kind == 251:
                    if len(self.buffer) < 8: break
                    size, forward = 8 + 16 * self.buffer[6], False
                else:
                    raise ValueError("Unsupported read-only viewer message")
            if size > 1_048_576: raise ValueError("Viewer message too large")
            if len(self.buffer) < size: break
            if self.control and self.stage == "normal" and self.buffer[0] == 4 and size == 8:
                key = struct.unpack("!I", self.buffer[4:8])[0]
                if self.buffer[1]: self.keys.add(key)
                else: self.keys.discard(key)
            elif self.control and self.stage == "normal" and self.buffer[0] == 5 and size == 6:
                self.pointer = struct.unpack("!HH", self.buffer[2:6]) if self.buffer[1] else None
            if forward: output.extend(self.buffer[:size])
            del self.buffer[:size]
        return bytes(output)

def start(target, password_env=None, control=False, idle_seconds=60):
    vm = registry.resolve(target)
    if vm["power"] != "running" or vm["console"]["status"] != "configured":
        raise ValueError("Viewer needs a running VM with a configured console")
    if not 30 <= idle_seconds <= 600:
        raise ValueError("Viewer idle timeout must be 30 to 600 seconds")
    for state in (read_json(p) for p in (home() / "viewers").glob("*/state.json")):
        if state.get("owner") == sessions.owner() and state.get("vm_id") == vm["id"] and sessions.worker_alive(state):
            return {"id": state["id"], "url": state["origin"] + "/#" + unseal(state["token"]), "control": state["control"], "reused": True}
    if control and any(s.get("vm_id") == vm["id"] and s.get("status") == "ready" for s in sessions.states()):
        raise ValueError("Close the assistant console session before opening a manual-control viewer")
    password = settings()["profiles"].get(vm["id"], {}).get("console_secret")
    if password_env:
        value = os.environ.get(password_env)
        if value is None: raise ValueError("Password environment variable is not set")
        password = seal(value)
    identifier = str(uuid.uuid4())
    path = home() / "viewers" / identifier / "state.json"
    state = {"id": identifier, "vm_id": vm["id"], "vm_name": vm["name"],
             "owner": sessions.owner(), "host": vm["console"]["host"], "port": vm["console"]["port"],
             "token": seal(secrets.token_urlsafe(32)), "password": password,
             "control": control, "idle_seconds": idle_seconds, "status": "starting"}
    write_json(path, state)
    job = sessions._launch(path, "vm_bridge.viewer", "VM Bridge live viewer")
    for _ in range(80):
        state = read_json(path)
        if state["status"] == "ready":
            state["job_id"] = job["id"]
            write_json(path, state)
            return {"id": identifier, "url": state["origin"] + "/#" + unseal(state["token"]),
                    "control": control, "vm_name": vm["name"], "idle_seconds": idle_seconds}
        if state["status"] == "failed":
            raise RuntimeError(state.get("error", "Viewer failed"))
        time.sleep(.1)
    raise TimeoutError("Viewer did not start")

def stop(identifier):
    uuid.UUID(identifier)
    path = home() / "viewers" / identifier / "state.json"
    state = read_json(path)
    if not state or state["owner"] != sessions.owner():
        raise PermissionError("Unknown or foreign viewer")
    if not sessions.worker_alive(state): return {"closed": True}
    import urllib.request
    request = urllib.request.Request(state["origin"] + "/close", data=b"", method="POST",
        headers={"Authorization": "Bearer " + unseal(state["token"]), "Origin": state["origin"]})
    with urllib.request.urlopen(request, timeout=3) as response:
        response.read()
    for _ in range(50):
        if not sessions.worker_alive(state): return {"closed": True}
        time.sleep(.1)
    raise TimeoutError("Viewer is still stopping")

def run(path):
    # websockify prints a harmless Unix daemon warning on Windows; keep stdio clean.
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        from websockify.websocketproxy import LibProxyServer, ProxyRequestHandler
        from websockify.auth_plugins import AuthenticationError
    state = read_json(path)
    token = unseal(state["token"])
    server = None
    mutex = None
    closing = threading.Event()
    sockets = set()
    socket_lock = threading.Lock()
    last_used = [time.monotonic()]
    began = time.monotonic()
    assets = Path(__file__).parent / "assets"

    class Auth:
        def authenticate(self, headers, target_host, target_port):
            protocols = [p.strip() for p in headers.get("Sec-WebSocket-Protocol", "").split(",")]
            if headers.get("Origin") != state["origin"] or not any(hmac.compare_digest(p, "vmbridge." + token) for p in protocols):
                raise AuthenticationError(response_msg="Viewer authorization required")

    class Handler(ProxyRequestHandler):
        def __init__(self, *args):
            self.filter = ReadOnlyRFB(control=state["control"])
            super().__init__(*args)

        def translate_path(self, value):
            from urllib.parse import urlsplit, unquote
            clean = unquote(urlsplit(value).path)
            if clean == "/": clean = "/viewer.html"
            candidate = (assets / clean.lstrip("/")).resolve()
            if not candidate.is_relative_to(assets.resolve()):
                return str(assets / "missing")
            return str(candidate)

        def log_message(self, *args): pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
            super().end_headers()

        def do_POST(self):
            allowed = self.headers.get("Origin") == state["origin"] and hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)
            if not allowed:
                self.send_error(403); return
            last_used[0] = time.monotonic()
            if self.path == "/config":
                result = {"name": state["vm_name"], "control": state["control"],
                          "password": unseal(state["password"]) if state.get("password") else None}
            elif self.path == "/heartbeat": result = {"ok": True}
            elif self.path == "/close":
                result = {"closed": True}; closing.set()
            else:
                self.send_error(404); return
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def new_websocket_client(self):
            with socket_lock: sockets.add(self.request)
            try:
                return super().new_websocket_client()
            finally:
                with socket_lock:
                    sockets.discard(self.request)
                    if not sockets:
                        closing.set()

        def recv_frames(self):
            frames, closed = super().recv_frames()
            filtered = [self.filter.feed(frame) for frame in frames]
            frames = [frame for frame in filtered if frame]
            return frames, closed

        def do_proxy(self, target):
            try:
                return super().do_proxy(target)
            finally:
                try:
                    release = self.filter.release_messages()
                    if release: target.sendall(release)
                except OSError: pass

    class Server(LibProxyServer):
        daemon_threads = True
        block_on_close = False

    try:
        if state["control"]:
            import win32api, win32event, winerror
            mutex = win32event.CreateMutex(None, True, "Local\\VMBridge-" + state["vm_id"])
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                raise PermissionError("Another session already owns this VM console")
        server = Server(Handler, listen_host="127.0.0.1", listen_port=0, web=str(assets),
                        target_host=state["host"], target_port=state["port"])
        server.timeout = .25
        server.file_only = True
        server.unix_listen = None
        # LibProxyServer clears auth_plugin in its constructor; set it afterwards.
        server.auth_plugin = Auth()
        state["origin"] = f"http://127.0.0.1:{server.server_port}"
        import win32api, win32process
        state.update(status="ready", pid=os.getpid(), process_created=win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"].timestamp())
        write_json(path, state)
        while not closing.is_set() and time.monotonic() - last_used[0] < state["idle_seconds"] and time.monotonic() - began < 1800:
            server.handle_request()
        state.update(status="closed", password=None, token=None)
    except BaseException as exc:
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}", password=None, token=None)
    finally:
        with socket_lock:
            for connection in sockets:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                connection.close()
        if server: server.server_close()
        if mutex:
            try:
                import win32event
                win32event.ReleaseMutex(mutex)
            except Exception: pass
            mutex.Close()
        write_json(path, state)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    run(parser.parse_args().state_file)

if __name__ == "__main__": main()
