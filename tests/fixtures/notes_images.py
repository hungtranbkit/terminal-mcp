"""Real image bytes for the notes attachment tests -- built here rather
than committed as binary fixtures, and deliberately WITHOUT Pillow (not a
dependency of this project; notes_service.py reads dimensions straight out
of the header for the same reason). Each helper produces a structurally
valid file: a real magic number and a real, parseable header, which is
exactly what notes_service.sniff_mime/probe_dimensions are asserted
against.
"""
from __future__ import annotations

import struct
import zlib


def png_bytes(width: int = 200, height: int = 120) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(row * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


def jpeg_bytes(width: int = 300, height: int = 200) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00" \
        + b"\x00\x01\x00\x01" + b"\x00\x00"
    sof0 = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">H", height) + struct.pack(">H", width) + b"\x03" + b"\x00" * 9)
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_bytes(width: int = 64, height: int = 48) -> bytes:
    # Lossy (VP8) container: the 'VP8 ' chunk's own frame header carries the
    # 14-bit dimensions probe_dimensions reads.
    frame = (b"\x00\x00\x00" + b"\x9d\x01\x2a"
             + struct.pack("<H", width) + struct.pack("<H", height) + b"\x00" * 8)
    body = b"WEBP" + b"VP8 " + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", len(body)) + body


def gif_bytes(width: int = 32, height: int = 16) -> bytes:
    return (b"GIF89a" + struct.pack("<H", width) + struct.pack("<H", height)
            + b"\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff" + b"\x3b")


NOT_AN_IMAGE = b"#!/bin/sh\necho this is a script, not a picture\n"
