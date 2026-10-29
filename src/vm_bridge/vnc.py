"""Synchronous VNC adapter. One instance belongs to one worker thread/process."""
from __future__ import annotations

import io
import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image
from vncdotool import api, rfb
from vncdotool.client import VNCDoToolClient, VNCDoToolFactory

MAX_PIXELS = 16_777_216
MAX_MESSAGE = 64 * 1024 * 1024


class GuardedClient(VNCDoToolClient):
    def __init__(self):
        super().__init__()
        self.rx_bytes = 0
        self.frame_requests = 0
        self.frame_commits = 0
        self.resize_pending = False

    def dataReceived(self, data):
        self.rx_bytes += len(data)
        return super().dataReceived(data)

    def framebufferUpdateRequest(self, *args, **kwargs):
        self.frame_requests += 1
        return super().framebufferUpdateRequest(*args, **kwargs)

    def request_frame(self):
        self.factory.frame_event.clear()
        self.framebufferUpdateRequest(incremental=False)
        return self

    def updateDesktopSize(self, width, height):
        self._dimensions(width, height)
        self.width, self.height = width, height
        self.resize_pending = True
        return super().updateDesktopSize(width, height)

    def commitUpdate(self, rectangles=None):
        self.frame_commits += 1
        if self.resize_pending and not rectangles:
            self.framebufferUpdateRequest(incremental=False)
            return
        if rectangles:
            self.resize_pending = False
        if self.screen is not None:
            with self.factory.frame_lock:
                self.factory.latest_frame = self.screen.copy()
                self.factory.frame_time = time.monotonic()
            self.factory.frame_event.set()
        # Empty updates still acknowledge an unchanged VMware framebuffer.
        if self.deferred is not None:
            pending, self.deferred = self.deferred, None
            pending.callback(self)

    def sendPassword(self, password):
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
        from cryptography.hazmat.primitives.ciphers import Cipher, modes
        key = password.encode("latin-1")[:8].ljust(8, b"\0")
        key = bytes(int(f"{value:08b}"[::-1], 2) for value in key)
        encoder = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
        self.transport.write(encoder.update(self._challenge) + encoder.finalize())

    def keyEvent(self, key, down=True):
        # The synchronous proxy chains results as the protocol for its next call.
        super().keyEvent(key, down=down)
        return self

    # The library logs clipboard content at INFO, so the adapter never delegates it.
    def copy_text(self, text):
        self.factory.clipboard_text = text

    def expect(self, handler, size, *args, **kwargs):
        if size < 0 or size > MAX_MESSAGE:
            raise ValueError("VNC message exceeds the configured limit")
        return super().expect(handler, size, *args, **kwargs)

    def _handleServerInit(self, block):
        width, height, _, name_length = struct.unpack("!HH16sI", block)
        self._dimensions(width, height)
        if name_length > 4096:
            raise ValueError("VNC desktop name is too large")
        return super()._handleServerInit(block)

    @staticmethod
    def _dimensions(width, height):
        if not 0 < width <= 16384 or not 0 < height <= 16384 or width * height > MAX_PIXELS:
            raise ValueError("VNC display exceeds the configured pixel limit")

    def _handleRectangle(self, block):
        x, y, width, height, encoding = struct.unpack("!HHHHi", block)
        if encoding == rfb.Encoding.PSEUDO_DESKTOP_SIZE:
            self._dimensions(width, height)
        elif encoding == rfb.Encoding.PSEUDO_CURSOR:
            if width > 512 or height > 512:
                raise ValueError("VNC cursor exceeds the configured limit")
        elif encoding >= 0:
            if x + width > self.width or y + height > self.height:
                raise ValueError("VNC rectangle falls outside the display")
        return super()._handleRectangle(block)


class GuardedFactory(VNCDoToolFactory):
    protocol = GuardedClient
    force_caps = True
    shared = True
    clipboard_text = ""

    def __init__(self):
        super().__init__()
        self.frame_event = threading.Event()
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.frame_time = 0


@dataclass
class Capture:
    png: bytes
    width: int
    height: int
    full_width: int
    full_height: int
    origin: tuple[int, int]
    elapsed_ms: float
    refresh_method: str = "frame_update"


class VNCSession:
    def __init__(self, host: str, port: int, password: str | None = None,
                 timeout: float = 4, encoding: str = "raw"):
        self.thread = threading.get_ident()
        self.client = None
        self.held_keys: set[str] = set()
        self.held_buttons: set[int] = set()
        self.timeout = timeout
        self.cancelled: Callable[[], bool] = lambda: False
        self.encoding = encoding
        self.endpoint, self.password = None, password
        logging.getLogger("vncdotool").setLevel(logging.ERROR)
        logging.getLogger("twisted").setLevel(logging.ERROR)
        endpoint = f"[{host}]::{port}" if ":" in host else f"{host}::{port}"
        self.endpoint = endpoint
        if encoding not in {"raw", "hextile", "zrle"}:
            raise ValueError("Encoding must be raw, hextile or zrle")
        factory = type("SessionFactory", (GuardedFactory,), {})
        # VNCDoToolClient reads encoding on the protocol rather than its factory.
        factory.protocol = type("SessionClient", (GuardedClient,),
                                {"encoding": getattr(rfb.Encoding, encoding.upper())})
        self.factory_class = factory
        try:
            self.client = api.connect(endpoint, password=password, timeout=timeout,
                                      factory_class=factory)
            self.client.refreshScreen(incremental=False)
        except BaseException:
            self.close()
            raise

    def _check(self):
        if threading.get_ident() != self.thread:
            raise RuntimeError("VNC operations must run on the owning worker thread")
        if self.client is None:
            raise ConnectionError("VNC connection is closed")
        if self.cancelled():
            raise InterruptedError("VM action was cancelled")

    @property
    def size(self):
        self._check()
        return self.client.protocol.screen.size

    def diagnostics(self):
        self._check()
        protocol = self.client.protocol
        return {"received_bytes": protocol.rx_bytes, "frame_requests": protocol.frame_requests,
                "frame_commits": protocol.frame_commits,
                "wire_size": [protocol.width, protocol.height],
                "cached_size": list(self.size)}

    def _point(self, x, y):
        self._check()
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise ValueError("Coordinates must be integer native pixels")
        width, height = self.size
        if not 0 <= x < width or not 0 <= y < height:
            raise ValueError(f"Coordinates outside {width}x{height}")

    def capture(self, region=None) -> Capture:
        self._check()
        started = time.perf_counter()
        # A full refresh has a bounded timeout even when the desktop is static.
        self.client.request_frame()
        method = "frame_update"
        if self.client.factory.frame_event.wait(0.15):
            with self.client.factory.frame_lock:
                screen = self.client.factory.latest_frame.copy()
        else:
            if self.held_keys or self.held_buttons:
                raise TimeoutError("No fresh frame while input is held; release input before observing again")
            # VMware may withhold an unchanged framebuffer even for a full request.
            # A shared observer's initial frame supplies a current image without
            # injecting input or presenting a stale cache as a new observation.
            observer = api.connect(self.endpoint, password=self.password,
                                   timeout=self.timeout, factory_class=self.factory_class)
            try:
                observer.refreshScreen(incremental=False)
                with observer.factory.frame_lock:
                    screen = observer.factory.latest_frame.copy()
            except BaseException:
                if observer.protocol is not None:
                    observer.disconnect()
                raise
            old, self.client = self.client, observer
            old.disconnect()
            method = "shared_observer_initial_frame"
        full_width, full_height = screen.size
        origin = (0, 0)
        if region is not None:
            if len(region) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in region):
                raise ValueError("Region must contain x, y, width, height")
            x, y, width, height = region
            self._point(x, y)
            if width <= 0 or height <= 0 or x + width > full_width or y + height > full_height:
                raise ValueError("Region outside the display")
            screen = screen.crop((x, y, x + width, y + height))
            origin = (x, y)
        out = io.BytesIO()
        screen.save(out, format="PNG", compress_level=2)
        return Capture(out.getvalue(), *screen.size, full_width, full_height, origin,
                       (time.perf_counter() - started) * 1000, method)

    def move(self, x, y):
        self._point(x, y)
        self.client.mouseMove(x, y)

    def click(self, x, y, button="left", count=1):
        self.move(x, y)
        if count not in (1, 2, 3):
            raise ValueError("Click count must be 1, 2 or 3")
        for i in range(count):
            self.mouse_down(x, y, button)
            time.sleep(0.05)
            self.mouse_up(x, y, button)
            if i + 1 < count:
                time.sleep(0.08)

    @staticmethod
    def _button(button):
        if button not in {"left", "middle", "right"}:
            raise ValueError("Unknown mouse button")
        return {"left": 1, "middle": 2, "right": 3}[button]

    def mouse_down(self, x, y, button="left"):
        self.move(x, y)
        number = self._button(button)
        self.held_buttons.add(number)
        self.client.mouseDown(number)

    def mouse_up(self, x, y, button="left"):
        self.move(x, y)
        number = self._button(button)
        self.client.mouseUp(number)
        self.held_buttons.discard(number)

    def drag(self, x1, y1, x2, y2, duration=0.3, button="left"):
        self._point(x2, y2)
        if not 0 <= duration <= 2:
            raise ValueError("Drag duration must be between 0 and 2 seconds")
        self.mouse_down(x1, y1, button)
        try:
            steps = max(1, int(duration * 30))
            for i in range(1, steps + 1):
                self._check()
                self.move(round(x1 + (x2 - x1) * i / steps),
                          round(y1 + (y2 - y1) * i / steps))
                if duration:
                    time.sleep(duration / steps)
        finally:
            self.release_all()

    def scroll(self, x, y, direction="down", amount=1):
        self.move(x, y)
        if direction not in {"up", "down", "left", "right"} or not 1 <= amount <= 20:
            raise ValueError("Scroll needs a direction and amount between 1 and 20")
        number = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
        for _ in range(amount):
            self._check()
            self.client.mousePress(number)

    @staticmethod
    def key_name(key):
        if not isinstance(key, str) or not key or len(key) > 32:
            raise ValueError("Invalid key name")
        return {"escape": "esc", "backspace": "bsp", "pageup": "pgup",
                "pagedown": "pgdn", "win": "super", "-": "minus"}.get(key.lower(), key)

    def key_down(self, key):
        self._check()
        name = self.key_name(key)
        self.held_keys.add(name)
        self.client.keyDown(name)

    def key_up(self, key):
        self._check()
        name = self.key_name(key)
        self.client.keyUp(name)
        self.held_keys.discard(name)

    def press(self, keys):
        self._check()
        if isinstance(keys, str):
            keys = [keys]
        if not keys or len(keys) > 5:
            raise ValueError("Use between one and five keys")
        try:
            for key in keys:
                self.key_down(key)
            time.sleep(0.05)
        finally:
            self.release_all()

    def type_text(self, text):
        self._check()
        if not isinstance(text, str) or len(text) > 160:
            raise ValueError("Text must be at most 160 characters per action; use chunks or guest terminal")
        for char in text:
            self._check()
            if char == "\r":
                continue
            if char in "\n\t":
                self.press(["enter" if char == "\n" else "tab"])
            elif ord(char) <= 255:
                self.press(["minus" if char == "-" else char])
            else:
                self.client.keyEvent(0x01000000 | ord(char), down=True)
                time.sleep(0.05)
                self.client.keyEvent(0x01000000 | ord(char), down=False)

    def release_all(self):
        if self.client is None:
            return
        for key in tuple(self.held_keys):
            try:
                self.client.keyUp(key)
            except Exception:
                pass
        for button in tuple(self.held_buttons):
            try:
                self.client.mouseUp(button)
            except Exception:
                pass
        self.held_keys.clear()
        self.held_buttons.clear()

    def close(self):
        self.release_all()
        if self.client is not None:
            try:
                if self.client.protocol is not None:
                    self.client.disconnect()
            except Exception:
                pass
            self.client = None
        api.shutdown()

