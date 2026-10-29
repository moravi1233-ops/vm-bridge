"""Temporary images only. Explicit evidence lives outside this cache."""
from __future__ import annotations
import hashlib
from pathlib import Path
import shutil
import time
import uuid

from .storage import home, read_json, write_json
from . import memory

RECENT_IMAGES = 12
SESSION_SOFT_BYTES = 256 * 1024 * 1024
GLOBAL_SOFT_BYTES = 1024 * 1024 * 1024
ORPHAN_SECONDS = 24 * 3600

def safe_image(path):
    root = home().absolute()
    canonical_root = root.resolve()
    original = Path(path).absolute()
    # Windows package redirection may expose both a logical and a canonical state
    # root. Accept those two representations, never a redirected child folder.
    try:
        relative = original.relative_to(root)
        boundary = root
    except ValueError:
        try:
            relative = original.relative_to(canonical_root)
            boundary = canonical_root
        except ValueError as exc:
            raise PermissionError("Image is outside the VM Bridge state root") from exc
    if len(relative.parts) != 4 or relative.parts[0] != "sessions" or relative.parts[2] != "images":
        raise PermissionError("Only native session image cache files are accepted")
    uuid.UUID(relative.parts[1])
    uuid.UUID(original.stem)
    expected = canonical_root / relative
    if original.suffix != ".png" or original.resolve() != expected:
        raise PermissionError("Only native VM Bridge session cache files may be cleaned or pinned")
    # Reject reparse points even when their final target happens to be inside root.
    for part in [original, original.parent, original.parent.parent, boundary / "sessions"]:
        if part.is_symlink() or (part.exists() and part.is_junction()):
            raise PermissionError("Reparse points are not accepted in the image cache")
    return original

def remove_image(path, reason):
    path = safe_image(path)
    for attempt in range(3):
        try:
            path.unlink(missing_ok=True)
            break
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {32,33} or attempt == 2: raise
            time.sleep(0.05)
    # Never mark an existing/locked image deleted before unlink succeeds.
    memory.deleted_capture(path.stem, reason)

def trim(cache, latest):
    files = sorted(cache.glob("*.png"), key=lambda p:p.stat().st_mtime, reverse=True)
    kept, total = [], 0
    removed = []
    for path in files:
        size = path.stat().st_size
        # Always preserve the freshly captured image, even above a soft byte target.
        if path.stem == latest or (len(kept) < RECENT_IMAGES and total + size <= SESSION_SOFT_BYTES):
            kept.append(path.stem)
            total += size
        else:
            try:
                remove_image(path, "recent_cache_rotation")
                removed.append(path.stem)
            except (OSError,ValueError):
                total += path.stat().st_size if path.exists() else 0
    return {"removed":removed, "cache_bytes":total, "soft_target_exceeded":total > SESSION_SOFT_BYTES,
            "cleanup_pending":len(files)-len(kept)-len(removed)}

def close_session(session_id):
    uuid.UUID(session_id)
    count, failures = 0, []
    for path in (home()/"sessions"/session_id/"images").glob("*.png"):
        try:
            remove_image(path, "session_closed")
            count += 1
        except (OSError,ValueError) as exc:
            failures.append({"capture_id":path.stem, "error_type":type(exc).__name__})
    return {"removed_images":count, "failures":failures[:12], "failed_count":len(failures),
            "completed":not failures}

def cleanup(now=None):
    """No daemon. Never touch a live worker, including workers owned by another chat."""
    from .sessions import worker_alive
    current = time.time() if now is None else now
    removed, skipped, failures = 0, 0, []
    for path in (home()/"sessions").glob("*/state.json"):
        state = read_json(path)
        if not state: continue
        from .sessions import worker_presence
        if worker_alive(state) or worker_presence(state) is None:
            skipped += 1
            continue
        age = current - max(state.get("last_used", 0), state.get("created", 0), path.stat().st_mtime)
        retry_pending = state.get("status") in {"closed","failed"} and (
            "cleanup pending" in (state.get("error") or "").lower() or
            (state.get("cache_cleanup") or {}).get("failed_count",0) > 0)
        if age < ORPHAN_SECONDS and not retry_pending: continue
        result = close_session(state["id"])
        removed += result["removed_images"]
        state["cache_cleanup"] = {**result,"checked_at":current}
        write_json(path,state)
        if result["failed_count"]:
            failures.append({"session":state["id"],"failed_count":result["failed_count"],"failures":result["failures"]})
    usage = sum(p.stat().st_size for p in (home()/"sessions").glob("*/images/*.png") if p.is_file())
    return {"removed_images":removed, "active_sessions_preserved":skipped,
            "cache_bytes":usage, "soft_target_bytes":GLOBAL_SOFT_BYTES,
            "soft_target_exceeded":usage > GLOBAL_SOFT_BYTES,
            "failed_sessions":failures[:12],
            "note":"Evidence is never automatically removed; cleanup runs only during activity"}

def pin(state, capture_id):
    uuid.UUID(capture_id)
    source = safe_image(home()/"sessions"/state["id"]/"images"/(capture_id+".png"))
    if not source.is_file(): raise ValueError("Capture no longer available; observe again")
    destination = home()/"evidence"/(capture_id+".png")
    destination.parent.mkdir(exist_ok=True)
    if destination.parent.is_junction() or destination.parent.is_symlink():
        raise PermissionError("Evidence folder cannot be a reparse point")
    image_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    # Exclusive create: no replacement of an existing evidence file.
    try:
        with destination.open("xb") as stream, source.open("rb") as image:
            shutil.copyfileobj(image, stream)
    except FileExistsError:
        if hashlib.sha256(destination.read_bytes()).hexdigest() != image_hash:
            raise ValueError("Existing evidence differs; refusing replacement")
    metadata = {"capture_id":capture_id, "session":state["id"], "vm":state["vm_id"],
                "sha256":image_hash, "path":str(destination), "preserved":True,
                "automatic_cleanup":False}
    write_json(destination.with_suffix(".json"), metadata)
    return metadata
