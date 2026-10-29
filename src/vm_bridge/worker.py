"""One guest console, one application thread. Never calls host input APIs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from multiprocessing.connection import Listener
from pathlib import Path

from .storage import read_json, write_json, unseal
from .vnc import VNCSession
from . import memory, image_cache

def run(state_file: Path):
    state = read_json(state_file)
    from .sessions import OWNER
    owner_token = OWNER.set(state["owner"])
    events = queue.Queue(maxsize=16)
    stop = threading.Event()
    finished = threading.Event()
    listener = None
    session = None
    handlers = []
    mutex = None
    result_state = "closed"
    error_message = None

    def save(**updates):
        current = read_json(state_file, state)
        current.update(updates)
        write_json(state_file, current)

    def handler(connection):
        try:
            if not connection.poll(3):
                return
            payload = json.loads(connection.recv_bytes(256000))
            if payload.get("op") == "close":
                stop.set()
                finished.wait(8)
                final_state = read_json(state_file, {})
                response = {"ok": True, "result": {"closed": finished.is_set(), "session": state["id"],
                    "learning":final_state.get("learning"), "cache_cleanup":final_state.get("cache_cleanup"),
                    "audit":final_state.get("audit")}}
            else:
                payload["deadline"] = time.monotonic() + 11
                event = threading.Event()
                holder = {}
                events.put((payload, event, holder), timeout=1)
                if not event.wait(12):
                    response = {"ok": False, "error": "Action deadline exceeded; execution may be unknown"}
                else:
                    response = holder["response"]
            connection.send_bytes(json.dumps(response).encode())
        except (OSError, EOFError, ValueError, queue.Full):
            pass
        finally:
            connection.close()

    def listen():
        while not stop.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                break
            thread = threading.Thread(target=handler, args=(connection,), daemon=True)
            handlers[:] = [old for old in handlers if old.is_alive()]
            handlers.append(thread)
            thread.start()

    try:
        import win32api, win32event, winerror
        mutex = win32event.CreateMutex(None, True, "Local\\VMBridge-" + state["vm_id"])
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            raise PermissionError("Another session already owns this VM console")
        session = VNCSession(state["host"], state["port"],
            password=unseal(state["password"]) if state.get("password") else None,
            timeout=3, encoding=state["encoding"])
        session.cancelled = stop.is_set
        listener = Listener(state["pipe"], family="AF_PIPE",
                            authkey=bytes.fromhex(unseal(state["auth"])))
        listener_thread = threading.Thread(target=listen, daemon=True)
        listener_thread.start()
        last_used = time.monotonic()
        geometry = session.size
        epoch = 1
        observations = {}
        last_delivered = None
        cache = state_file.parent / "images"
        cache.mkdir(exist_ok=True)
        import win32api, win32process
        created = win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"].timestamp()
        save(status="ready", pid=os.getpid(), process_created=created, password=None, last_used=time.time())

        def status():
            return {"session": state["id"], "vm_id": state["vm_id"], "vm_name": state["vm_name"],
                    "owner": state["owner"], "size": list(session.size), "status": "ready",
                    "geometry_epoch": epoch, "execution_context": "guest_console",
                    "transport": session.diagnostics(),
                    "encoding": state["encoding"], "idle_seconds": state["idle_seconds"]}

        while not stop.is_set():
            if time.monotonic() - last_used > state["idle_seconds"]:
                break
            try:
                payload, event, holder = events.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if time.monotonic() > payload.get("deadline", 0):
                    raise TimeoutError("Queued action expired before execution")
                last_used = time.monotonic()
                op = payload.get("op")
                if session.size != geometry:
                    geometry = session.size
                    epoch += 1
                    observations.clear()
                    last_delivered = None
                if op == "status":
                    value = status()
                elif op == "observe":
                    if payload.get("known_image") and payload.get("since_capture_id"):
                        raise ValueError("Use known_image or since_capture_id, not both")
                    capture = session.capture(payload.get("region"))
                    if (capture.full_width, capture.full_height) != geometry:
                        geometry = (capture.full_width, capture.full_height)
                        epoch += 1
                        observations.clear()
                        last_delivered = None
                    capture_id = str(uuid.uuid4())
                    path = cache / (capture_id + ".png")
                    path.write_bytes(capture.png)
                    image_hash = hashlib.sha256(capture.png).hexdigest()
                    reference_id = last_delivered if payload.get("known_image") else payload.get("since_capture_id")
                    reference = observations.get(reference_id)
                    unchanged = bool(reference and reference["epoch"] == epoch and time.monotonic() - reference["time"] <= 120 and
                        reference["digest"] == image_hash and reference["origin"] == capture.origin and
                        reference["size"] == (capture.width, capture.height))
                    observations[capture_id] = {"time": time.monotonic(), "epoch": epoch,
                                               "origin": capture.origin,
                                               "size": (capture.width, capture.height), "digest":image_hash}
                    value = {**status(), "capture_id": capture_id, "path": str(path.resolve()),
                             "width": capture.width, "height": capture.height,
                             "origin": list(capture.origin), "captured_at": time.time(),
                             "refresh_method": capture.refresh_method,
                             "sha256": image_hash, "png_bytes":len(capture.png),
                             "image_required":not unchanged,
                             "unchanged_from":reference_id if unchanged else None,
                             "comparison_requested":bool(payload.get("known_image") or payload.get("since_capture_id")),
                             "capture_ms": round(capture.elapsed_ms, 2)}
                    value["learning"] = memory.record_capture(state, capture_id, value, len(capture.png), image_hash)
                    # An identical fresh frame inherits the acknowledged visual
                    # state. Advance its reference so rotating old PNG files does
                    # not force redundant images while context is still known.
                    last_delivered = capture_id
                    rotation = image_cache.trim(cache, capture_id)
                    for old in rotation["removed"]:
                        observations.pop(old, None)
                    value["cache_bytes"] = rotation["cache_bytes"]
                    value["cache_soft_target_exceeded"] = rotation["soft_target_exceeded"]
                    value["cache_cleanup_pending"] = rotation["cleanup_pending"]
                elif op == "pin":
                    if payload.get("capture_id") not in observations:
                        raise ValueError("Capture no longer available in this session")
                    value = image_cache.pin(state, payload["capture_id"])
                elif op in {"verify", "review"}:
                    observation = observations.get(payload.get("capture_id"))
                    if not observation or observation["epoch"] != epoch or time.monotonic() - observation["time"] > 120:
                        raise ValueError("Verification requires a recent observation in this geometry")
                    if op == "review":
                        value = memory.review(state,payload["capture_id"],payload["body"])
                    else:
                        value = memory.verify(state, payload["action_id"], payload["capture_id"],
                                              payload["success"], payload["note"], payload.get("prior_session", False))
                elif op == "input":
                    if payload.get("action") == "release_all":
                        session.release_all()
                        holder["response"] = {"ok": True, "result": {**status(), "released": True}}
                        continue
                    observation = observations.get(payload.get("capture_id"))
                    if not observation or observation["epoch"] != epoch:
                        raise ValueError("Stale or foreign capture; observe this session again")
                    if time.monotonic() - observation["time"] > 120:
                        raise ValueError("Capture expired; observe again")
                    action = payload["action"]
                    args = dict(payload.get("args") or {})
                    if action not in {"move", "click", "drag", "scroll", "press", "type_text",
                                      "key_down", "key_up", "mouse_down", "mouse_up", "release_all"}:
                        raise ValueError("Unknown input action")
                    # Coordinates belong to the returned crop, not to its viewer scale.
                    for x_key, y_key in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
                        if x_key in args and y_key in args:
                            x, y = args[x_key], args[y_key]
                            if not isinstance(x, int) or not isinstance(y, int) or not 0 <= x < observation["size"][0] or not 0 <= y < observation["size"][1]:
                                raise ValueError("Coordinates outside the observed image")
                            args[x_key] += observation["origin"][0]
                            args[y_key] += observation["origin"][1]
                    started = time.perf_counter()
                    # Durable intent before sending. Values (especially typed secrets) never enter the journal.
                    action_id = memory.plan_action(state, action, payload.get("capture_id"))
                    try:
                        getattr(session, action)(**args)
                    except BaseException as exc:
                        memory.action_state(action_id, "unknown", error_type=type(exc).__name__)
                        raise
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    memory.action_state(action_id, "sent", elapsed)
                    value = {**status(), "action": action, "sent": True,
                             "action_id":action_id, "verification":"unverified",
                             "effect": "verify_with_observation",
                             "elapsed_ms": elapsed}
                else:
                    raise ValueError("Unknown worker operation")
                holder["response"] = {"ok": True, "result": value}
                save(last_used=time.time())
            except Exception as exc:
                holder["response"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                event.set()
    except BaseException as exc:
        result_state = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        if session:
            session.close()
        if listener:
            listener.close()
        if mutex:
            try:
                import win32event
                win32event.ReleaseMutex(mutex)
            except Exception:
                pass
            mutex.Close()
        cleanup_result, learning, audit = None, None, None
        try:
            cleanup_result = image_cache.close_session(state["id"])
            if cleanup_result["failed_count"]:
                error_message = (error_message or "") + " Cache cleanup pending"
        except Exception as exc:
            error_message = (error_message or "") + f" Cache cleanup pending: {type(exc).__name__}"
        try:
            from .review_audit import audit as run_audit
            learning = memory.session_summary(state["id"])
            audit = run_audit(save=True)
        except Exception as exc:
            audit = {"error_type":type(exc).__name__,"saved":False}
        save(status=result_state, error=error_message, password=None, auth=None,
             cache_cleanup=cleanup_result, learning=learning, audit=audit)
        OWNER.reset(owner_token)
        finished.set()
        for thread in handlers:
            thread.join(0.2)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()
    run(args.state_file)

if __name__ == "__main__":
    main()

