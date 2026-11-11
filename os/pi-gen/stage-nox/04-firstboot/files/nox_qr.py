"""A minimal QR code encoder for Nox-OS's console (the setup hotspot's Wi-Fi QR code).

Root draws the code at boot (nox_setup.py), so it may not run anything a lower layer can change -
not Nox's Python environment, which Nox updates itself. This file is part of the image's
root-owned setup programs (/usr/local/lib/nox-os, root 0644) and uses the standard library only.

Scope, deliberately small: byte mode, error correction level M, versions 1-10 (up to 213 bytes -
a Wi-Fi payload has well under 120), all eight masks, the lowest penalty chosen as the standard
asks (ISO/IEC 18004). Written for Nox (Apache-2.0) after the standard's tables and the well-known
structure of Project Nayuki's QR code generator; tests compare every mask against segno.

    matrix(data: bytes, mask: int | None = None) -> list[list[bool]]   True = a dark module
"""

from __future__ import annotations

__all__ = ["matrix", "version_for"]

#: Level M, versions 1-10: (error-correction codewords per block, [(blocks, data codewords)]).
_BLOCKS: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGNMENT: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}
#: Level M in the format information.
_ECL_M = 0


def _data_codewords(version: int) -> int:
    return sum(count * size for count, size in _BLOCKS[version][1])


def version_for(length: int) -> int:
    """The smallest version that holds `length` bytes in byte mode at level M."""
    for version in _BLOCKS:
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * length <= 8 * _data_codewords(version):
            return version
    raise ValueError("too much data for a version 1-10 QR code")


# ---- Reed-Solomon over GF(256), polynomial 0x11D -------------------------------------------


def _multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _multiply(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _multiply(root, 0x02)
    return result


def _remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _multiply(coefficient, factor)
    return result


# ---- the codewords -------------------------------------------------------------------------


def _codewords(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    capacity = 8 * _data_codewords(version)
    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity - len(bits)))  # terminator
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    ec_length, groups = _BLOCKS[version]
    divisor = _divisor(ec_length)
    blocks, offset = [], 0
    for count, size in groups:
        for _ in range(count):
            block = words[offset : offset + size]
            offset += size
            blocks.append((block, _remainder(block, divisor)))
    out: list[int] = []
    for i in range(max(len(b) for b, _ in blocks)):
        out += [b[i] for b, _ in blocks if i < len(b)]
    for i in range(ec_length):
        out += [e[i] for _, e in blocks]
    return out


# ---- the symbol ----------------------------------------------------------------------------


class _Symbol:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def finder(self, cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def alignment(self, cx: int, cy: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(self, mask: int) -> None:
        data = _ECL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(self.size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, self.size - 15 + i, bit(i))
        self.set(8, self.size - 8, True)  # the dark module

    def version_bits(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, dark)
            self.set(b, a, dark)

    def function_patterns(self) -> None:
        for i in range(self.size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        self.finder(3, 3)
        self.finder(self.size - 4, 3)
        self.finder(3, self.size - 4)
        positions = _ALIGNMENT[self.version]
        last = len(positions) - 1
        for i, x in enumerate(positions):
            for j, y in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                self.alignment(x, y)
        self.format_bits(0)  # reserved; drawn for real with the mask
        self.version_bits()

    def place(self, codewords: list[int]) -> None:
        bits = [(w >> (7 - k)) & 1 for w in codewords for k in range(8)]
        bits += [0] * _REMAINDER_BITS[self.version]
        i = 0
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = self.size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(bits):
                        self.dark[y][x] = bits[i] == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.function[y][x]:
                    continue
                if mask == 0:
                    invert = (x + y) % 2 == 0
                elif mask == 1:
                    invert = y % 2 == 0
                elif mask == 2:
                    invert = x % 3 == 0
                elif mask == 3:
                    invert = (x + y) % 3 == 0
                elif mask == 4:
                    invert = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    invert = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    invert = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    invert = ((x + y) % 2 + x * y % 3) % 2 == 0
                if invert:
                    self.dark[y][x] = not self.dark[y][x]


def _penalty(dark: list[list[bool]]) -> int:
    """ISO/IEC 18004 mask evaluation: runs, 2x2 blocks, finder-like patterns, balance."""
    size = len(dark)
    score = 0
    lines = dark + [list(col) for col in zip(*dark, strict=True)]
    for line in lines:
        run, color = 0, None
        for module in line:
            if module == color:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, color = 1, module
        if run >= 5:
            score += 3 + run - 5
        text = "".join("1" if m else "0" for m in line)
        for pattern in ("10111010000", "00001011101"):
            start = text.find(pattern)
            while start != -1:
                score += 40
                start = text.find(pattern, start + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if dark[y][x] == dark[y][x + 1] == dark[y + 1][x] == dark[y + 1][x + 1]:
                score += 3
    total = sum(sum(row) for row in dark)
    percent = total * 100 // (size * size)
    score += 10 * (abs(percent - 50) // 5)
    return score


def matrix(data: bytes, mask: int | None = None) -> list[list[bool]]:
    """The QR code for `data` (byte mode, level M) as rows of modules; `mask` None picks the
    lowest penalty."""
    version = version_for(len(data))
    codewords = _codewords(data, version)
    best: tuple[int, list[list[bool]]] | None = None
    for candidate in range(8) if mask is None else (mask,):
        symbol = _Symbol(version)
        symbol.function_patterns()
        symbol.place(codewords)
        symbol.apply_mask(candidate)
        symbol.format_bits(candidate)
        score = _penalty(symbol.dark)
        if best is None or score < best[0]:
            best = (score, symbol.dark)
    assert best is not None
    return best[1]
