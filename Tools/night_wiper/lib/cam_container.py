"""Lossless Falcon BMS .cam container parsing and rebuild helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct
from typing import Callable

from .lzss import lzss_compress, lzss_expand


class CamFormatError(RuntimeError):
    """Raised when a CAM/TRN/TAC container or entry is malformed."""


Compressor = Callable[[bytes], bytes]


@dataclass(frozen=True)
class CamEntry:
    name: str
    offset: int
    length: int


@dataclass(frozen=True)
class DecodedPayload:
    data: bytes
    is_compressed: bool
    compression_kind: str | None
    metadata: dict[str, int]


@dataclass
class DecodedEntry:
    name: str
    offset: int
    length: int
    raw: bytes
    decoded: bytes
    is_compressed: bool
    compression_kind: str | None
    metadata: dict[str, int] = field(default_factory=dict)
    modified: bool = False
    _original_decoded: bytes = field(default=b"", repr=False)

    @classmethod
    def from_parts(cls, entry: CamEntry, raw: bytes, payload: DecodedPayload) -> "DecodedEntry":
        return cls(
            name=entry.name,
            offset=entry.offset,
            length=entry.length,
            raw=raw,
            decoded=payload.data,
            is_compressed=payload.is_compressed,
            compression_kind=payload.compression_kind,
            metadata=dict(payload.metadata),
            _original_decoded=payload.data,
        )

    def set_decoded(self, data: bytes) -> None:
        self.decoded = data
        self.modified = data != self._original_decoded


class CamContainer:
    """In-memory, lossless representation of a .cam-like container."""

    def __init__(self, entries: list[DecodedEntry], original_blob: bytes | None = None):
        self.entries = entries
        self.original_blob = original_blob

    @classmethod
    def from_bytes(cls, blob: bytes) -> "CamContainer":
        entries: list[DecodedEntry] = []
        for entry in parse_container(blob):
            raw = blob[entry.offset : entry.offset + entry.length]
            payload = decode_entry_payload(entry.name, raw)
            entries.append(DecodedEntry.from_parts(entry, raw, payload))
        return cls(entries, original_blob=blob)

    @classmethod
    def from_path(cls, path: str | Path) -> "CamContainer":
        return cls.from_bytes(Path(path).read_bytes())

    def get_entry(self, name: str) -> DecodedEntry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def set_entry_decoded(self, name: str, data: bytes) -> DecodedEntry:
        entry = self.get_entry(name)
        entry.set_decoded(data)
        return entry

    def rebuild_bytes(
        self,
        *,
        preserve_unchanged_raw: bool = True,
        compressor: Compressor = lzss_compress,
    ) -> bytes:
        if preserve_unchanged_raw and self.original_blob is not None and not any(
            entry.modified for entry in self.entries
        ):
            return self.original_blob

        raw_entries: list[tuple[str, bytes]] = []
        for entry in self.entries:
            if preserve_unchanged_raw and not entry.modified:
                raw_entries.append((entry.name, entry.raw))
                continue
            raw_entries.append(
                (
                    entry.name,
                    encode_entry_payload(
                        entry.name,
                        entry.decoded,
                        metadata=entry.metadata,
                        compression_kind=entry.compression_kind,
                        is_compressed=entry.is_compressed,
                        compressor=compressor,
                    ),
                )
            )
        return build_container_blob(raw_entries)


def parse_container(blob: bytes) -> list[CamEntry]:
    if len(blob) < 8:
        raise CamFormatError("file is too small to be a CAM/TRN/TAC container")

    directory_offset = _read_u32_le(blob, 0)
    if directory_offset >= len(blob):
        raise CamFormatError(f"directory offset {directory_offset} is past EOF")

    entry_count = _read_u32_le(blob, directory_offset)
    cursor = directory_offset + 4
    entries: list[CamEntry] = []

    for index in range(entry_count):
        if cursor >= len(blob):
            raise CamFormatError(f"unexpected end of directory at entry {index}")
        name_len = blob[cursor]
        cursor += 1
        if cursor + name_len + 8 > len(blob):
            raise CamFormatError(f"directory entry {index} is truncated")

        name = blob[cursor : cursor + name_len].decode("ascii", errors="replace")
        cursor += name_len
        offset = _read_u32_le(blob, cursor)
        cursor += 4
        length = _read_u32_le(blob, cursor)
        cursor += 4

        if offset + length > len(blob):
            raise CamFormatError(f"entry {name!r} points outside file bounds")
        entries.append(CamEntry(name=name, offset=offset, length=length))

    return entries


def decode_entry_payload(name: str, raw: bytes) -> DecodedPayload:
    ext = Path(name).suffix.lower()
    if ext == ".cmp":
        return _decode_cmp_payload(name, raw)

    if ext in {".obd", ".uni"}:
        return _decode_counted_lzss_payload(name, raw, ext)

    if ext == ".obj":
        return _decode_obj_payload(name, raw)

    return DecodedPayload(raw, False, None, {})


def encode_entry_payload(
    name: str,
    data: bytes,
    *,
    metadata: dict[str, int],
    compression_kind: str | None,
    is_compressed: bool,
    compressor: Compressor = lzss_compress,
) -> bytes:
    if not is_compressed or compression_kind is None:
        return data

    payload = compressor(data)
    if compression_kind == "cmp":
        return struct.pack("<II", len(payload) + 4, len(data)) + payload
    if compression_kind == "uni":
        record_count = metadata.get("record_count")
        if not isinstance(record_count, int):
            raise CamFormatError(f"{name}: missing record_count")
        return struct.pack("<IHI", len(payload) + 6, record_count, len(data)) + payload
    if compression_kind == "obj":
        num_objectives = metadata.get("num_objectives")
        if not isinstance(num_objectives, int):
            raise CamFormatError(f"{name}: missing num_objectives")
        return struct.pack("<HII", num_objectives, len(data), len(payload)) + payload
    raise CamFormatError(f"{name}: unsupported compression kind {compression_kind!r}")


def build_container_blob(raw_entries: list[tuple[str, bytes]]) -> bytes:
    if not raw_entries:
        raise CamFormatError("cannot build an empty CAM/TRN/TAC container")

    output = bytearray(b"\x00\x00\x00\x00")
    offsets: list[tuple[str, int, int]] = []
    for name, raw in raw_entries:
        entry_offset = len(output)
        output.extend(raw)
        offsets.append((name, entry_offset, len(raw)))

    directory_offset = len(output)
    output.extend(struct.pack("<I", len(offsets)))
    for name, offset, length in offsets:
        encoded = name.encode("ascii")
        if len(encoded) > 255:
            raise CamFormatError(f"entry name too long: {name!r}")
        output.append(len(encoded))
        output.extend(encoded)
        output.extend(struct.pack("<II", offset, length))

    struct.pack_into("<I", output, 0, directory_offset)
    return bytes(output)


def _decode_cmp_payload(name: str, raw: bytes) -> DecodedPayload:
    if len(raw) < 8:
        raise CamFormatError(f"{name}: too short for .cmp header")

    compressed_size = _read_u32_le(raw, 0)
    uncompressed_size = _read_u32_le(raw, 4)
    _validate_expected_length(name, raw, compressed_size + 4)

    payload = raw[8 : 8 + (compressed_size - 4)]
    decoded, consumed = lzss_expand(payload, uncompressed_size)
    _validate_lzss_consumption(name, payload, consumed)
    return DecodedPayload(
        decoded,
        True,
        "cmp",
        {
            "compressed_size": compressed_size,
            "uncompressed_size": uncompressed_size,
        },
    )


def _decode_counted_lzss_payload(name: str, raw: bytes, ext: str) -> DecodedPayload:
    if len(raw) < 10:
        raise CamFormatError(f"{name}: too short for {ext} header")

    compressed_size = _read_u32_le(raw, 0)
    record_count = _read_u16_le(raw, 4)
    uncompressed_size = _read_u32_le(raw, 6)
    _validate_expected_length(name, raw, compressed_size + 4)

    payload = raw[10 : 10 + (compressed_size - 6)]
    decoded, consumed = lzss_expand(payload, uncompressed_size)
    if consumed != len(payload):
        raise CamFormatError(
            f"{name}: LZSS consumed {consumed} bytes, expected {len(payload)}"
        )
    return DecodedPayload(
        decoded,
        True,
        "uni",
        {
            "compressed_size": compressed_size,
            "record_count": record_count,
            "uncompressed_size": uncompressed_size,
        },
    )


def _decode_obj_payload(name: str, raw: bytes) -> DecodedPayload:
    if len(raw) < 10:
        raise CamFormatError(f"{name}: too short for .obj header")

    num_objectives = _read_u16_le(raw, 0)
    uncompressed_size = _read_u32_le(raw, 2)
    compressed_size = _read_u32_le(raw, 6)
    if len(raw) != 10 + compressed_size:
        raise CamFormatError(f"{name}: invalid .obj compressed size")

    payload = raw[10 : 10 + compressed_size]
    decoded, consumed = lzss_expand(payload, uncompressed_size)
    if consumed != len(payload):
        raise CamFormatError(
            f"{name}: LZSS consumed {consumed} bytes, expected {len(payload)}"
        )
    return DecodedPayload(
        decoded,
        True,
        "obj",
        {
            "compressed_size": compressed_size,
            "num_objectives": num_objectives,
            "uncompressed_size": uncompressed_size,
        },
    )


def _validate_expected_length(name: str, raw: bytes, expected_length: int) -> None:
    if expected_length > len(raw):
        raise CamFormatError(f"{name}: expected entry length {expected_length}, got {len(raw)}")
    if expected_length < len(raw) and any(raw[expected_length:]):
        raise CamFormatError(f"{name}: nonzero trailing bytes after compressed payload")


def _validate_lzss_consumption(name: str, payload: bytes, consumed: int) -> None:
    if consumed != len(payload) and any(payload[consumed:]):
        raise CamFormatError(f"{name}: nonzero trailing bytes after LZSS payload")


def _read_u16_le(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise CamFormatError(f"cannot read u16 at offset {offset}")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32_le(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise CamFormatError(f"cannot read u32 at offset {offset}")
    return struct.unpack_from("<I", data, offset)[0]
