"""CMP entry parser API."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from lib.cam.cam_content import (
    BinaryReader,
    ParseContext,
    _decode_fixed_ascii,
    _format_campaign_time_z,
    _read_vuid,
)
from lib.cam.types import ParsedCmpData


_CMP_AIR_UNIT_ROW_SIZE = 196
_CMP_AIR_UNIT_BASE_OFFSET = 19
_CMP_AIR_UNIT_BASE_SIZE = 81
_CMP_AIR_UNIT_FLAGS_OFFSET = 100
_CMP_AIR_UNIT_CAMP_ID_OFFSET = 104
_CMP_AIR_UNIT_NAME_OFFSET = 108
_CMP_AIR_UNIT_NAME_SIZE = 80


def _decode_ascii_field(field: bytes) -> str:
    return field.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def _looks_like_air_unit_row(data: bytes, offset: int) -> bool:
    if offset < 0 or offset + _CMP_AIR_UNIT_ROW_SIZE > len(data):
        return False
    flight_num = struct.unpack_from("<I", data, offset)[0]
    creator = struct.unpack_from("<I", data, offset + 4)[0]
    if not (0 < flight_num <= 1_000_000):
        return False
    if creator > 65_535:
        return False
    packed_ct_and_flags = struct.unpack_from("<I", data, offset + 8)[0]
    ct_index = packed_ct_and_flags & 0xFFFF
    if not (0 <= ct_index <= 10_000):
        return False
    base_name = _decode_ascii_field(
        data[offset + _CMP_AIR_UNIT_BASE_OFFSET : offset + _CMP_AIR_UNIT_BASE_OFFSET + _CMP_AIR_UNIT_BASE_SIZE]
    )
    return len(base_name) >= 3


def _extract_air_unit_rows(data: bytes) -> list[dict[str, Any]]:
    best_start: int | None = None
    best_count = 0
    for offset in range(0, max(0, len(data) - _CMP_AIR_UNIT_ROW_SIZE) + 1):
        if not _looks_like_air_unit_row(data, offset):
            continue
        count = 0
        probe = offset
        while _looks_like_air_unit_row(data, probe):
            count += 1
            probe += _CMP_AIR_UNIT_ROW_SIZE
        if count > best_count:
            best_start = offset
            best_count = count
    if best_start is None or best_count < 4:
        return []

    rows: list[dict[str, Any]] = []
    for index in range(best_count):
        offset = best_start + index * _CMP_AIR_UNIT_ROW_SIZE
        row_blob = data[offset : offset + _CMP_AIR_UNIT_ROW_SIZE]
        flight_num = struct.unpack_from("<I", row_blob, 0)[0]
        creator = struct.unpack_from("<I", row_blob, 4)[0]
        packed_ct_and_flags = struct.unpack_from("<I", row_blob, 8)[0]
        row_flags = struct.unpack_from("<I", row_blob, _CMP_AIR_UNIT_FLAGS_OFFSET)[0]
        raw_camp_id = struct.unpack_from("<I", row_blob, _CMP_AIR_UNIT_CAMP_ID_OFFSET)[0]
        base_name = _decode_ascii_field(
            row_blob[_CMP_AIR_UNIT_BASE_OFFSET : _CMP_AIR_UNIT_BASE_OFFSET + _CMP_AIR_UNIT_BASE_SIZE]
        )
        display_name = _decode_ascii_field(
            row_blob[_CMP_AIR_UNIT_NAME_OFFSET : _CMP_AIR_UNIT_NAME_OFFSET + _CMP_AIR_UNIT_NAME_SIZE]
        )
        rows.append(
            {
                "index": index,
                "offset": offset,
                "size": _CMP_AIR_UNIT_ROW_SIZE,
                "flight_id": {"num": flight_num, "creator": creator},
                "ct_index": packed_ct_and_flags & 0xFFFF,
                "header_flags": (packed_ct_and_flags >> 16) & 0xFFFF,
                "base_name": base_name,
                "display_name": display_name,
                "row_flags": row_flags,
                "camp_id": raw_camp_id & 0xFFFF,
                "control_slot_raw": (raw_camp_id >> 16) & 0xFFFF,
                "is_human_controlled": ((raw_camp_id >> 16) & 0xFFFF) == 0,
            }
        )
    return rows


def _parse_cmp(data: bytes, _ctx: ParseContext) -> dict[str, Any]:
    """Parse `.cmp` campaign state fields needed for summary extraction.

    The parser intentionally decodes a stable subset (current time, bullseye,
    identifying strings, player squadron id) and stores the remaining tail as
    opaque bytes metadata.
    """

    reader = BinaryReader(data)
    parsed: dict[str, Any] = {
        "file_type": "cmp",
        "size": len(data),
    }

    current_time = reader.read_u32()
    parsed["current_time"] = current_time
    parsed["current_time_z"] = _format_campaign_time_z(current_time)

    parsed["te_start_time"] = reader.read_u32()
    parsed["te_time_limit"] = reader.read_u32()
    parsed["te_victory_points"] = reader.read_i32()
    parsed["te_type"] = reader.read_i32()
    parsed["te_number_teams"] = reader.read_i32()

    # Skip arrays/fields that are currently not needed by the summary surface.
    reader.skip(8 * 4)
    reader.skip(8 * 4)
    reader.read_i32()
    reader.skip(8 * 4)
    reader.read_i32()

    for _ in range(8):
        reader.read_u8()
        reader.read_u8()
        reader.read_bytes(20)
        reader.read_bytes(200)

    reader.skip(4 * 4)

    for _ in range(9):
        reader.read_i16()

    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()

    parsed["bullseye_name_id"] = reader.read_u8()
    parsed["bullseye_x"] = reader.read_i16()
    parsed["bullseye_y"] = reader.read_i16()

    parsed["theater_name"] = _decode_fixed_ascii(reader.read_bytes(40))
    parsed["scenario"] = _decode_fixed_ascii(reader.read_bytes(40))
    parsed["save_file"] = _decode_fixed_ascii(reader.read_bytes(40))
    parsed["ui_name"] = _decode_fixed_ascii(reader.read_bytes(40))
    parsed["player_squadron_id"] = _read_vuid(reader)
    parsed["air_unit_rows"] = _extract_air_unit_rows(data)

    tail = reader.read_bytes(reader.remaining())
    parsed["undocumented_tail_size"] = len(tail)
    parsed["undocumented_tail_head_hex"] = tail[:64].hex()
    return parsed


def parse_cmp(
    data: bytes,
    *,
    container_version: int | None = None,
    support_base_dir: str | Path | None = None,
    decode_metadata: dict[str, int] | None = None,
) -> ParsedCmpData:
    ctx = ParseContext(
        container_version=container_version,
        decode_metadata=decode_metadata,
        support_base_dir=Path(support_base_dir).resolve() if support_base_dir is not None else None,
    )
    return ParsedCmpData.from_dict(_parse_cmp(data, ctx))


__all__ = ["_parse_cmp", "parse_cmp"]
