"""Small little-endian binary reader."""

from __future__ import annotations

import struct


class BinaryParseError(RuntimeError):
    """Raised when a binary read would pass EOF."""


class BinaryReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def tell(self) -> int:
        return self.offset

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def skip(self, size: int) -> None:
        self._ensure(size)
        self.offset += size

    def read_bytes(self, size: int) -> bytes:
        self._ensure(size)
        value = self.data[self.offset : self.offset + size]
        self.offset += size
        return value

    def u8(self) -> int:
        self._ensure(1)
        value = self.data[self.offset]
        self.offset += 1
        return value

    def i16(self) -> int:
        self._ensure(2)
        value = struct.unpack_from("<h", self.data, self.offset)[0]
        self.offset += 2
        return value

    def u16(self) -> int:
        self._ensure(2)
        value = struct.unpack_from("<H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def i32(self) -> int:
        self._ensure(4)
        value = struct.unpack_from("<i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def u32(self) -> int:
        self._ensure(4)
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def vu_id(self) -> tuple[int, int]:
        return self.u32(), self.u32()

    def _ensure(self, size: int) -> None:
        if size < 0:
            raise ValueError("size must be >= 0")
        if self.offset + size > len(self.data):
            raise BinaryParseError(
                f"need {size} bytes at offset {self.offset}, only {self.remaining()} remain"
            )

