"""Run in its own process so each Twisted lifecycle is isolated."""
from __future__ import annotations
import argparse
import io
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fixtures.rfb_server import Desktop
from vm_bridge.vnc import VNCSession
from PIL import Image

def _different_thread(session):
    try:
        session.capture()
    except RuntimeError:
        return True
    return False

p = argparse.ArgumentParser()
p.add_argument("--encoding", default="raw")
p.add_argument("--output", required=True)
p.add_argument("--wrong-password", action="store_true")
args = p.parse_args()
with Desktop(1280, 720, password="fixture", encoding=args.encoding) as desktop:
    if args.wrong_password:
        try:
            VNCSession("127.0.0.1", desktop.port, password="wrong", timeout=2)
        except Exception as exc:
            print(json.dumps({"authentication_rejected": True, "error_type": type(exc).__name__}))
        else:
            raise AssertionError("Wrong password accepted")
    else:
        s = VNCSession("127.0.0.1", desktop.port, password="fixture", encoding=args.encoding)
        try:
            before = s.capture()
            assert Image.open(io.BytesIO(before.png)).getpixel((5, 5)) == (16, 36, 58)
            s.click(80, 150)
            assert desktop.clicks == 1
            s.press(["ctrl", "a"])
            s.type_text("Hello-é-€")
            s.drag(50, 150, 200, 180, duration=0.05)
            s.scroll(80, 150, "down", 2)
            s.key_down("shift")
            s.mouse_down(80, 150)
            s.release_all()
            shot = s.capture()
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_bytes(shot.png)
            times = [s.capture().elapsed_ms for _ in range(10)]
            refused = []
            t = threading.Thread(target=lambda: refused.append(_different_thread(s)))
            t.start()
            t.join()
            assert refused == [True]
            try:
                s.click(2000, 0)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid coordinates accepted")
            assert all(event[1] == 1 for event in desktop.events if event[0] == "connect")
            print(json.dumps({"encoding": args.encoding, "size": [shot.width, shot.height],
                              "clicks": desktop.clicks, "events": len(desktop.events),
                              "capture_median_ms": round(statistics.median(times), 2),
                              "image": str(Path(args.output).resolve()), "thread_guard": True}))
        finally:
            s.close()

