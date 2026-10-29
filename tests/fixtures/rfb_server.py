"""A synthetic RFB desktop: no access to host display or host input APIs."""
from __future__ import annotations

import hashlib
import socket
import socketserver
import struct
import threading
import time
import zlib

from PIL import Image, ImageDraw, ImageFont

PIXEL_FORMAT = struct.pack("!BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)


def exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        block = stream.recv(length - len(chunks))
        if not block:
            raise EOFError()
        chunks.extend(block)
    return bytes(chunks)


class Desktop(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, width=640, height=360, password=None, encoding="raw"):
        self.width, self.height = width, height
        self.password = password
        self.encoding = encoding
        self.events = []
        self.clicks = 0
        self.revision = 0
        self.connections = 0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def port(self):
        return self.server_address[1]

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.shutdown()
        self.server_close()
        self.thread.join(2)

    def image(self):
        with self.lock:
            img = Image.new("RGB", (self.width, self.height), "#10243a")
            d = ImageDraw.Draw(img)
            d.rectangle((20, 20, self.width - 20, 80), fill="#10a6aa")
            d.rectangle((30, 120, 250, 200), fill="#eaa42a")
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
            except OSError:
                font = ImageFont.load_default()
            d.text((30, 32), "VM Bridge test console", fill="white", font=font)
            d.text((40, 136), f"Clicks: {self.clicks}", fill="black", font=font)
            d.text((30, 230), f"Revision {self.revision}", fill="white", font=font)
            return img


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        s.settimeout(5)
        desktop = self.server
        last_mask = 0
        compressor = zlib.compressobj()
        try:
            s.sendall(b"RFB 003.008\n")
            exact(s, 12)
            s.sendall(bytes([1, 2 if desktop.password else 1]))
            security = exact(s, 1)[0]
            if security == 2:
                from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
                from cryptography.hazmat.primitives.ciphers import Cipher, modes
                challenge = bytes(range(16))
                s.sendall(challenge)
                response = exact(s, 16)
                key = desktop.password.encode("ascii")[:8].ljust(8, b"\0")
                key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
                cipher = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
                expected = cipher.update(challenge) + cipher.finalize()
                if response != expected:
                    message = b"Authentication failed"
                    s.sendall(struct.pack("!II", 1, len(message)) + message)
                    return
            s.sendall(struct.pack("!I", 0))
            shared = exact(s, 1)[0]
            name = b"VM Bridge synthetic test"
            s.sendall(struct.pack("!HH", desktop.width, desktop.height) +
                      PIXEL_FORMAT + struct.pack("!I", len(name)) + name)
            with desktop.lock:
                desktop.connections += 1
                desktop.events.append(("connect", shared))
            offered = [0]
            while not desktop.stop_event.is_set():
                try:
                    kind = exact(s, 1)[0]
                except socket.timeout:
                    continue
                if kind == 0:
                    data = exact(s, 19)
                    requested = data[3:]
                    if requested != PIXEL_FORMAT:
                        raise ValueError(f"Fixture supports RGBX32 only: {requested.hex()}")
                elif kind == 2:
                    _, count = struct.unpack("!BH", exact(s, 3))
                    offered = list(struct.unpack(f"!{count}i", exact(s, count * 4)))
                elif kind == 3:
                    exact(s, 9)
                    image = desktop.image()
                    width, height = image.size
                    enc = {"raw": 0, "hextile": 5, "zrle": 16}[desktop.encoding]
                    if enc not in offered:
                        enc = 0
                    if enc == 0:
                        data = image.tobytes("raw", "RGBX")
                    elif enc == 5:
                        tiles = []
                        for y in range(0, height, 16):
                            for x in range(0, width, 16):
                                tiles.append(b"\x01" + image.crop(
                                    (x, y, min(x + 16, width), min(y + 16, height))
                                ).tobytes("raw", "RGBX"))
                        data = b"".join(tiles)
                    else:
                        tiles = []
                        for y in range(0, height, 64):
                            for x in range(0, width, 64):
                                tiles.append(b"\x00" + image.crop(
                                    (x, y, min(x + 64, width), min(y + 64, height))
                                ).tobytes())
                        packed = compressor.compress(b"".join(tiles)) + compressor.flush(zlib.Z_SYNC_FLUSH)
                        data = struct.pack("!I", len(packed)) + packed
                    s.sendall(b"\x00\x00\x00\x01" +
                              struct.pack("!HHHHi", 0, 0, width, height, enc) + data)
                elif kind == 4:
                    down, key = struct.unpack("!B2xI", exact(s, 7))
                    with desktop.lock:
                        desktop.events.append(("key", key, bool(down)))
                        desktop.revision += 1
                elif kind == 5:
                    mask, x, y = struct.unpack("!BHH", exact(s, 5))
                    with desktop.lock:
                        desktop.events.append(("pointer", mask, x, y))
                        if mask & 1 and not last_mask & 1 and 30 <= x <= 250 and 120 <= y <= 200:
                            desktop.clicks += 1
                        desktop.revision += 1
                    last_mask = mask
                elif kind == 6:
                    _, length = struct.unpack("!3sI", exact(s, 7))
                    if length > 100000:
                        raise ValueError("Oversized fixture clipboard")
                    exact(s, length)
                else:
                    raise ValueError(f"Unknown RFB message {kind}")
        except (EOFError, OSError):
            pass
        finally:
            with desktop.lock:
                desktop.events.append(("disconnect",))

