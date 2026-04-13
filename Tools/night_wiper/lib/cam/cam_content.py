"""CAM container parsing and in-memory loading."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import struct
from typing import Any, Callable

from .lzss import LzssError, lzss_expand

logger = logging.getLogger("html_brief_log")


class CamFormatError(RuntimeError):
    """Raised when the input .CAM/.TRN/.TAC file is malformed."""


class ParseError(RuntimeError):
    """Raised when parsed entry data is malformed."""


@dataclass(frozen=True)
class CamEntry:
    name: str
    offset: int
    length: int


@dataclass(frozen=True)
class DecodeResult:
    data: bytes
    compressed: bool
    metadata: dict[str, int]


@dataclass(frozen=True)
class DecodedEntry:
    entry: CamEntry
    raw: bytes
    decoded: DecodeResult


@dataclass(frozen=True)
class ParseContext:
    container_version: int | None
    decode_metadata: dict[str, int] | None
    support_base_dir: Path | None


class BinaryReader:
    """Minimal little-endian sequential reader with bounds checks."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def _ensure(self, size: int) -> None:
        if self.offset + size > len(self.data):
            raise ParseError(
                f"need {size} bytes at offset {self.offset}, only {self.remaining()} remain"
            )

    def skip(self, size: int) -> None:
        self._ensure(size)
        self.offset += size

    def read_u8(self) -> int:
        self._ensure(1)
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_i16(self) -> int:
        self._ensure(2)
        value = struct.unpack_from("<h", self.data, self.offset)[0]
        self.offset += 2
        return value

    def read_u16(self) -> int:
        self._ensure(2)
        value = struct.unpack_from("<H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def read_i32(self) -> int:
        self._ensure(4)
        value = struct.unpack_from("<i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def read_u32(self) -> int:
        self._ensure(4)
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def read_f32(self) -> float:
        self._ensure(4)
        value = struct.unpack_from("<f", self.data, self.offset)[0]
        self.offset += 4
        return value

    def read_bytes(self, size: int) -> bytes:
        self._ensure(size)
        value = self.data[self.offset : self.offset + size]
        self.offset += size
        return value


EntryParser = Callable[[bytes, ParseContext], dict[str, Any]]
_ENTRY_PARSERS: dict[str, EntryParser] = {}
_STRINGS_BY_ID_CACHE: dict[Path, dict[int, str]] = {}


def _decode_fixed_ascii(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _read_u16_le(data: bytes, offset: int) -> int:
    if offset + 2 > len(data):
        raise CamFormatError(f"cannot read u16 at offset {offset}")
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32_le(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise CamFormatError(f"cannot read u32 at offset {offset}")
    return struct.unpack_from("<I", data, offset)[0]


def _safe_int(text: str | None, *, default: int = 0) -> int:
    if text is None:
        return default
    cleaned = text.strip()
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _to_vuid_tuple(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    num = value.get("num")
    creator = value.get("creator")
    if isinstance(num, int) and isinstance(creator, int):
        return (num, creator)
    return None


def _read_vuid(reader: BinaryReader) -> dict[str, int]:
    return {"num": reader.read_u32(), "creator": reader.read_u32()}


def _find_support_file(
    name: str,
    *,
    support_base_dir: Path | None = None,
) -> Path | None:
    script_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    if support_base_dir is not None:
        support_base_dir = support_base_dir.resolve()
        is_strings = name.lower() == "strings.txt"
        if is_strings and support_base_dir.name.lower() == "objects":
            theater_data_dir = support_base_dir.parent.parent
            candidates.extend(
                [
                    theater_data_dir / "Campaign" / name,
                    support_base_dir / name,
                ]
            )
        else:
            candidates.append(support_base_dir / name)
        if support_base_dir.name.lower() == "objects" and not is_strings:
            theater_data_dir = support_base_dir.parent.parent
            candidates.append(theater_data_dir / "Campaign" / name)
    candidates.extend([Path.cwd() / name, script_dir / name])

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            logger.debug("Support file resolved: %s -> %s", name, resolved)
            return resolved
        parent = resolved.parent
        wanted = resolved.name.lower()
        try:
            if parent.is_dir():
                for child in parent.iterdir():
                    if child.is_file() and child.name.lower() == wanted:
                        logger.debug("Support file resolved (case-insensitive): %s -> %s", name, child)
                        return child.resolve()
        except Exception:
            continue
    logger.debug("Support file not found: %s (support_base_dir=%s)", name, support_base_dir)
    return None


def _load_strings_by_id(path: Path) -> dict[int, str]:
    cached = _STRINGS_BY_ID_CACHE.get(path)
    if cached is not None:
        logger.debug("Strings cache hit: %s entries=%d", path, len(cached))
        return cached

    strings_by_id: dict[int, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.rstrip("\r\n")
            if not text:
                continue
            key_value = text.split("\t", 1)
            if len(key_value) != 2:
                continue
            key_text, value = key_value
            if not key_text.isdigit():
                continue
            strings_by_id[int(key_text)] = value

    _STRINGS_BY_ID_CACHE[path] = strings_by_id
    logger.debug("Strings loaded: %s entries=%d", path, len(strings_by_id))
    return strings_by_id


def _load_strings(support_base_dir: Path | None) -> dict[int, str]:
    strings_path = _find_support_file("Strings.txt", support_base_dir=support_base_dir)
    if strings_path is None:
        strings_path = _find_support_file("strings.txt", support_base_dir=support_base_dir)
    if strings_path is None:
        return {}
    return _load_strings_by_id(strings_path)


def _format_campaign_time_z(value_ms: int | None) -> str | None:
    if not isinstance(value_ms, int) or value_ms < 0:
        return None
    total_seconds, milliseconds = divmod(value_ms, 1000)
    hours = (total_seconds // 3600) % 24
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if milliseconds:
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}z"
    return f"{hours:02}:{minutes:02}:{seconds:02}z"


def _parse_ver(data: bytes) -> dict[str, Any]:
    text = data.decode("ascii", errors="replace").strip("\x00\r\n\t ")
    version: int | None = None
    if text.isdigit():
        version = int(text)
    return {
        "file_type": "ver",
        "size": len(data),
        "raw_ascii": text,
        "version": version,
    }


def register_entry_parser(ext: str, parser: EntryParser) -> None:
    normalized = ext.lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    _ENTRY_PARSERS[normalized] = parser


def _parse_entry_data(
    name: str,
    data: bytes,
    *,
    container_version: int | None,
    decode_metadata: dict[str, int] | None,
    support_base_dir: Path | None = None,
) -> dict[str, Any] | None:
    ext = Path(name).suffix.lower()

    if ext == ".ver":
        return _parse_ver(data)

    parser = _ENTRY_PARSERS.get(ext)
    if parser is None:
        logger.debug("No parser registered for entry %s (%s)", name, ext)
        return None

    context = ParseContext(
        container_version=container_version,
        decode_metadata=decode_metadata,
        support_base_dir=support_base_dir,
    )
    parsed = parser(data, context)
    logger.debug("Parsed entry %s (%s): size=%d", name, ext, len(data))
    return parsed


def _detect_container_version(decoded_entries: list[DecodedEntry]) -> int | None:
    for item in decoded_entries:
        if Path(item.entry.name).suffix.lower() != ".ver":
            continue
        parsed_ver = _parse_ver(item.decoded.data)
        version = parsed_ver.get("version")
        if isinstance(version, int):
            logger.debug("Container version detected from %s: %d", item.entry.name, version)
            return version
    logger.debug("Container version not detected (no parseable .ver entry)")
    return None


def register_default_parsers() -> None:
    from lib.parsers.parse_cmp import _parse_cmp
    from lib.parsers.parse_uni import _parse_uni

    register_entry_parser(".cmp", _parse_cmp)
    register_entry_parser(".uni", _parse_uni)
    logger.debug("Registered default CAM entry parsers: .cmp, .uni")


def parse_container(blob: bytes) -> list[CamEntry]:
    if len(blob) < 8:
        raise CamFormatError("file is too small to be a CAM/TRN/TAC container")

    directory_offset = _read_u32_le(blob, 0)
    if directory_offset >= len(blob):
        raise CamFormatError(
            f"directory offset {directory_offset} is past end of file ({len(blob)})"
        )

    entry_count = _read_u32_le(blob, directory_offset)
    cursor = directory_offset + 4

    entries: list[CamEntry] = []
    for index in range(entry_count):
        if cursor >= len(blob):
            raise CamFormatError(f"unexpected end of directory at entry {index}")

        name_len = blob[cursor]
        cursor += 1

        if cursor + name_len + 8 > len(blob):
            raise CamFormatError(
                f"directory entry {index} is truncated (name_len={name_len})"
            )

        name = blob[cursor : cursor + name_len].decode("ascii", errors="replace")
        cursor += name_len

        offset = _read_u32_le(blob, cursor)
        cursor += 4
        length = _read_u32_le(blob, cursor)
        cursor += 4

        if offset + length > len(blob):
            raise CamFormatError(
                f"entry {name!r} points outside file bounds: "
                f"offset={offset}, length={length}, file_size={len(blob)}"
            )

        entries.append(CamEntry(name=name, offset=offset, length=length))

    return entries


def _decode_compressed_entry(name: str, raw: bytes) -> DecodeResult:
    ext = Path(name).suffix.lower()

    if ext == ".cmp":
        if len(raw) < 8:
            raise CamFormatError(f"{name}: too short for .cmp compression header")

        compressed_size = _read_u32_le(raw, 0)
        uncompressed_size = _read_u32_le(raw, 4)

        if compressed_size < 4:
            raise CamFormatError(f"{name}: invalid compressed size {compressed_size}")
        expected_length = compressed_size + 4
        if expected_length > len(raw):
            raise CamFormatError(
                f"{name}: compressed_size+4 ({expected_length}) != entry length ({len(raw)})"
            )
        if expected_length < len(raw):
            trailing = raw[expected_length:]
            if any(byte != 0 for byte in trailing):
                raise CamFormatError(
                    f"{name}: compressed_size+4 ({expected_length}) != entry length ({len(raw)})"
                )

        payload = raw[8 : 8 + (compressed_size - 4)]
        output, consumed = lzss_expand(payload, uncompressed_size)
        if consumed != len(payload):
            remaining = payload[consumed:]
            if any(byte != 0 for byte in remaining):
                raise CamFormatError(
                    f"{name}: LZSS consumed {consumed} bytes, expected {len(payload)}"
                )

        return DecodeResult(
            data=output,
            compressed=True,
            metadata={
                "compressed_size": compressed_size,
                "uncompressed_size": uncompressed_size,
            },
        )

    if ext in {".obd", ".uni"}:
        if len(raw) < 10:
            raise CamFormatError(f"{name}: too short for {ext} compression header")

        compressed_size = _read_u32_le(raw, 0)
        record_count = _read_u16_le(raw, 4)
        uncompressed_size = _read_u32_le(raw, 6)

        if compressed_size < 6:
            raise CamFormatError(f"{name}: invalid compressed size {compressed_size}")
        expected_length = compressed_size + 4
        if expected_length > len(raw):
            raise CamFormatError(
                f"{name}: compressed_size+4 ({expected_length}) != entry length ({len(raw)})"
            )
        if expected_length < len(raw):
            trailing = raw[expected_length:]
            if any(byte != 0 for byte in trailing):
                raise CamFormatError(
                    f"{name}: compressed_size+4 ({expected_length}) != entry length ({len(raw)})"
                )

        payload = raw[10 : 10 + (compressed_size - 6)]
        output, consumed = lzss_expand(payload, uncompressed_size)
        if consumed != len(payload):
            raise CamFormatError(
                f"{name}: LZSS consumed {consumed} bytes, expected {len(payload)}"
            )

        return DecodeResult(
            data=output,
            compressed=True,
            metadata={
                "compressed_size": compressed_size,
                "uncompressed_size": uncompressed_size,
                "record_count": record_count,
            },
        )

    if ext == ".obj":
        if len(raw) < 10:
            raise CamFormatError(f"{name}: too short for .obj compression header")

        num_objectives = _read_u16_le(raw, 0)
        uncompressed_size = _read_u32_le(raw, 2)
        compressed_size = _read_u32_le(raw, 6)

        if 10 + compressed_size != len(raw):
            raise CamFormatError(
                f"{name}: 10+compressed_size ({10 + compressed_size}) != entry length ({len(raw)})"
            )

        payload = raw[10 : 10 + compressed_size]
        output, consumed = lzss_expand(payload, uncompressed_size)
        if consumed != len(payload):
            raise CamFormatError(
                f"{name}: LZSS consumed {consumed} bytes, expected {len(payload)}"
            )

        return DecodeResult(
            data=output,
            compressed=True,
            metadata={
                "compressed_size": compressed_size,
                "uncompressed_size": uncompressed_size,
                "num_objectives": num_objectives,
            },
        )

    return DecodeResult(data=raw, compressed=False, metadata={})


def _safe_output_path(base_dir: Path, entry_name: str) -> Path:
    normalized = entry_name.replace("\\", "/")
    candidate = (base_dir / normalized).resolve()
    base_resolved = base_dir.resolve()

    if candidate == base_resolved:
        raise CamFormatError(f"invalid output entry name {entry_name!r}")

    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise CamFormatError(f"unsafe output entry name {entry_name!r}") from exc

    return candidate


def extract_container(
    input_path: Path,
    output_dir: Path,
    *,
    best_effort: bool,
    parse_data: bool,
    support_base_dir: Path | None = None,
) -> list[dict[str, object]]:
    register_default_parsers()

    blob = input_path.read_bytes()
    entries = parse_container(blob)

    decoded_entries: list[DecodedEntry] = []
    for entry in entries:
        raw = blob[entry.offset : entry.offset + entry.length]
        try:
            decoded = _decode_compressed_entry(entry.name, raw)
        except (CamFormatError, LzssError):
            if not best_effort:
                raise
            decoded = DecodeResult(data=raw, compressed=False, metadata={})
        decoded_entries.append(DecodedEntry(entry=entry, raw=raw, decoded=decoded))

    container_version = _detect_container_version(decoded_entries)

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []

    for item in decoded_entries:
        entry = item.entry
        decoded = item.decoded

        output_path = _safe_output_path(output_dir, entry.name)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(decoded.data)

        manifest_item: dict[str, object] = {
            "name": entry.name,
            "offset": entry.offset,
            "length": entry.length,
            "output_path": str(output_path),
            "output_size": len(decoded.data),
            "decompressed": decoded.compressed,
        }
        if decoded.metadata:
            manifest_item.update(decoded.metadata)

        if parse_data:
            try:
                parsed = _parse_entry_data(
                    entry.name,
                    decoded.data,
                    container_version=container_version,
                    decode_metadata=decoded.metadata if decoded.metadata else None,
                    support_base_dir=support_base_dir,
                )
                if parsed is not None:
                    parsed_path = output_path.with_name(output_path.name + ".parsed.json")
                    parsed_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
                    manifest_item["parsed"] = True
                    manifest_item["parsed_path"] = str(parsed_path)
            except Exception as exc:
                manifest_item["parsed"] = False
                manifest_item["parse_error"] = str(exc)

        manifest.append(manifest_item)

    return manifest


@dataclass(frozen=True)
class CamEntryData:
    name: str
    offset: int
    length: int
    decoded_size: int
    compressed: bool
    decode_metadata: dict[str, int]
    data: bytes
    parsed: dict[str, Any] | None


@dataclass(frozen=True)
class ParsedCamData:
    source_path: Path
    bms_base_dir: Path | None
    container_version: int | None
    entries: list[CamEntryData]
    parsed_by_name: dict[str, dict[str, Any]]
    parsed_by_ext: dict[str, dict[str, Any]]

    def get_parsed(self, name: str) -> dict[str, Any] | None:
        return self.parsed_by_name.get(name)

    def get_parsed_by_ext(self, ext: str) -> dict[str, Any] | None:
        normalized = ext if ext.startswith(".") else f".{ext}"
        return self.parsed_by_ext.get(normalized.lower())

    def get_entry(self, name: str) -> CamEntryData | None:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def get_entry_by_ext(self, ext: str) -> CamEntryData | None:
        normalized = ext if ext.startswith(".") else f".{ext}"
        normalized = normalized.lower()
        for entry in self.entries:
            if Path(entry.name).suffix.lower() == normalized:
                return entry
        return None


def parse_cam_file(
    input_path: str | Path,
    *,
    bms_base_dir: str | Path | None = None,
    parse_entries: bool = True,
    best_effort: bool = False,
) -> ParsedCamData:
    source_path = Path(input_path).resolve()
    support_base_dir = Path(bms_base_dir).resolve() if bms_base_dir is not None else None
    logger.debug(
        "parse_cam_file start: input=%s support_base_dir=%s parse_entries=%s best_effort=%s",
        source_path,
        support_base_dir,
        parse_entries,
        best_effort,
    )

    blob = source_path.read_bytes()
    entries = parse_container(blob)
    logger.debug("CAM container parsed: entries=%d", len(entries))

    decoded_entries: list[DecodedEntry] = []
    for entry in entries:
        raw = blob[entry.offset : entry.offset + entry.length]
        try:
            decoded = _decode_compressed_entry(entry.name, raw)
        except (CamFormatError, LzssError) as exc:
            if not best_effort:
                raise
            logger.warning(
                "CAM decode failed for entry %s; using raw payload due to best_effort: %s",
                entry.name,
                exc,
            )
            decoded = DecodeResult(data=raw, compressed=False, metadata={})
        decoded_entries.append(DecodedEntry(entry=entry, raw=raw, decoded=decoded))

    container_version = _detect_container_version(decoded_entries)
    logger.debug("CAM container version detected: %s", container_version)

    out_entries: list[CamEntryData] = []
    parsed_by_name: dict[str, dict[str, Any]] = {}
    parsed_by_ext: dict[str, dict[str, Any]] = {}

    for decoded_item in decoded_entries:
        entry = decoded_item.entry
        decoded = decoded_item.decoded

        parsed: dict[str, Any] | None = None
        if parse_entries:
            parsed = _parse_entry_data(
                entry.name,
                decoded.data,
                container_version=container_version,
                decode_metadata=decoded.metadata if decoded.metadata else None,
                support_base_dir=support_base_dir,
            )
            if parsed is not None:
                parsed_by_name[entry.name] = parsed
                ext = Path(entry.name).suffix.lower()
                if ext and ext not in parsed_by_ext:
                    parsed_by_ext[ext] = parsed

        out_entries.append(
            CamEntryData(
                name=entry.name,
                offset=entry.offset,
                length=entry.length,
                decoded_size=len(decoded.data),
                compressed=decoded.compressed,
                decode_metadata=dict(decoded.metadata),
                data=decoded.data,
                parsed=parsed,
            )
        )

    return ParsedCamData(
        source_path=source_path,
        bms_base_dir=support_base_dir,
        container_version=container_version,
        entries=out_entries,
        parsed_by_name=parsed_by_name,
        parsed_by_ext=parsed_by_ext,
    )


__all__ = [
    "BinaryReader",
    "CamEntry",
    "CamEntryData",
    "CamFormatError",
    "DecodeResult",
    "DecodedEntry",
    "LzssError",
    "ParseContext",
    "ParseError",
    "ParsedCamData",
    "_decode_compressed_entry",
    "_decode_fixed_ascii",
    "_detect_container_version",
    "_find_support_file",
    "_format_campaign_time_z",
    "_load_strings",
    "_load_strings_by_id",
    "_parse_entry_data",
    "_read_u16_le",
    "_read_u32_le",
    "_read_vuid",
    "_safe_int",
    "_safe_output_path",
    "_to_vuid_tuple",
    "extract_container",
    "parse_cam_file",
    "parse_container",
    "register_default_parsers",
    "register_entry_parser",
]
