"""Minimal RGBA PNG reader/writer on top of `zlib` and `numpy`.

Exists so the pet's layer-cutting script (`make_pet_layers.py`) needs nothing beyond what the
project already installs: Pillow is not a dependency of Nox and one image script is not a good
reason to make it one. Deliberately narrow — it reads 8-bit colour types 2 (RGB) and 6 (RGBA),
non-interlaced, and writes colour type 6 only. Anything else raises instead of guessing.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BYTES_PER_PIXEL_BY_COLOR_TYPE = {2: 3, 6: 4}


class PngFormatError(ValueError):
    """The file is a PNG this module deliberately does not handle."""


def _iter_chunks(data: bytes):
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        yield kind, payload
        offset += 12 + length


def _undo_filters(raw: bytes, width: int, height: int, channels: int) -> np.ndarray:
    """Reverse the per-scanline PNG filters (spec 9.2) into an (h, w, channels) uint8 array."""
    stride = width * channels
    out = np.zeros((height, stride), dtype=np.uint8)
    previous = np.zeros(stride, dtype=np.int16)
    position = 0
    for row in range(height):
        filter_type = raw[position]
        position += 1
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=position).astype(np.int16)
        position += stride
        if filter_type == 0:
            current = line
        elif filter_type == 1:
            current = line.copy()
            for i in range(channels, stride):
                current[i] = (current[i] + current[i - channels]) & 0xFF
        elif filter_type == 2:
            current = (line + previous) & 0xFF
        elif filter_type == 3:
            current = line.copy()
            for i in range(stride):
                left = current[i - channels] if i >= channels else 0
                current[i] = (current[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            current = line.copy()
            for i in range(stride):
                left = int(current[i - channels]) if i >= channels else 0
                up = int(previous[i])
                up_left = int(previous[i - channels]) if i >= channels else 0
                estimate = left + up - up_left
                d_left, d_up, d_up_left = (
                    abs(estimate - left),
                    abs(estimate - up),
                    abs(estimate - up_left),
                )
                if d_left <= d_up and d_left <= d_up_left:
                    predictor = left
                elif d_up <= d_up_left:
                    predictor = up
                else:
                    predictor = up_left
                current[i] = (current[i] + predictor) & 0xFF
        else:
            raise PngFormatError(f"unknown scanline filter {filter_type}")
        out[row] = current.astype(np.uint8)
        previous = current
    return out.reshape(height, width, channels)


def read_rgba(path: Path) -> np.ndarray:
    """Read a non-interlaced 8-bit RGB/RGBA PNG as an (h, w, 4) uint8 array."""
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise PngFormatError(f"{path.name} is not a PNG")
    width = height = color_type = 0
    compressed = bytearray()
    for kind, payload in _iter_chunks(data):
        if kind == b"IHDR":
            width, height, depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            if depth != 8:
                raise PngFormatError(f"{path.name}: only 8-bit samples are supported")
            if interlace != 0:
                raise PngFormatError(f"{path.name}: interlaced PNGs are not supported")
            if color_type not in BYTES_PER_PIXEL_BY_COLOR_TYPE:
                raise PngFormatError(f"{path.name}: colour type {color_type} is not supported")
        elif kind == b"IDAT":
            compressed += payload
        elif kind == b"IEND":
            break
    channels = BYTES_PER_PIXEL_BY_COLOR_TYPE[color_type]
    pixels = _undo_filters(zlib.decompress(bytes(compressed)), width, height, channels)
    if channels == 3:
        opaque = np.full((height, width, 1), 255, dtype=np.uint8)
        pixels = np.concatenate([pixels, opaque], axis=2)
    return pixels


def write_rgba(path: Path, image: np.ndarray) -> None:
    """Write an (h, w, 4) uint8 array as a colour-type-6 PNG (filter 0, default compression)."""
    if image.ndim != 3 or image.shape[2] != 4 or image.dtype != np.uint8:
        raise ValueError("write_rgba expects an (h, w, 4) uint8 array")
    height, width, _ = image.shape
    rows = np.concatenate(
        [np.zeros((height, 1), dtype=np.uint8), image.reshape(height, width * 4)], axis=1
    )
    chunks = [
        _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        _chunk(b"IDAT", zlib.compress(rows.tobytes(), 9)),
        _chunk(b"IEND", b""),
    ]
    path.write_bytes(PNG_SIGNATURE + b"".join(chunks))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(
        ">I", zlib.crc32(kind + payload) & 0xFFFFFFFF
    )
