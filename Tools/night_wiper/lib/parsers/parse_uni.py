"""UNI entry parser and package/flight inference logic."""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from lib.cam.cam_content import (
    BinaryReader,
    ParseContext,
    ParseError,
    _find_support_file,
    _format_campaign_time_z,
    _load_strings_by_id,
    _safe_int,
    _to_vuid_tuple,
)
from lib.cam.types import ParsedUniData

_CT_CACHE: dict[Path, dict[int, dict[str, int]]] = {}
_UCD_RECORD_CACHE: dict[Path, dict[int, dict[str, Any]]] = {}
_VCD_RECORD_CACHE: dict[Path, dict[int, dict[str, int | str]]] = {}


def _load_ct_records(path: Path) -> dict[int, dict[str, int]]:
    """Load class-table (`FALCON4_CT.XML`) records indexed by CT number."""

    cached = _CT_CACHE.get(path)
    if cached is not None:
        return cached

    root = ET.parse(path).getroot()
    records: dict[int, dict[str, int]] = {}
    for node in root.findall("CT"):
        num_text = node.attrib.get("Num")
        if num_text is None:
            continue
        try:
            num = int(num_text)
        except ValueError:
            continue
        records[num] = {
            "domain": _safe_int(node.findtext("Domain")),
            "class": _safe_int(node.findtext("Class")),
            "type": _safe_int(node.findtext("Type")),
            "subtype": _safe_int(node.findtext("SubType")),
            "specific": _safe_int(node.findtext("Specific")),
            "entity_type": _safe_int(node.findtext("EntityType")),
            "entity_idx": _safe_int(node.findtext("EntityIdx"), default=-1),
        }

    _CT_CACHE[path] = records
    return records


def _load_ucd_records(path: Path) -> dict[int, dict[str, Any]]:
    """Load unit class definitions (`FALCON4_UCD.XML`) keyed by CtIdx."""

    cached = _UCD_RECORD_CACHE.get(path)
    if cached is not None:
        return cached

    root = ET.parse(path).getroot()
    records: dict[int, dict[str, Any]] = {}
    for node in root.findall("UCD"):
        ct_idx = _safe_int(node.findtext("CtIdx"), default=-1)
        if ct_idx < 0:
            continue
        name = (node.findtext("Name") or "").strip()
        vehicle_ct_indices: list[int] = []
        for index in range(16):
            value = _safe_int(node.findtext(f"VehicleCtIdx_{index}"), default=-1)
            if value >= 0:
                vehicle_ct_indices.append(value)
        records[ct_idx] = {
            "name": name,
            "vehicle_ct_indices": vehicle_ct_indices,
        }

    _UCD_RECORD_CACHE[path] = records
    return records


def _load_vcd_records(path: Path) -> dict[int, dict[str, int | str]]:
    """Load vehicle definitions (`FALCON4_VCD.XML`) keyed by CtIdx."""

    cached = _VCD_RECORD_CACHE.get(path)
    if cached is not None:
        return cached

    root = ET.parse(path).getroot()
    records: dict[int, dict[str, int | str]] = {}
    for node in root.findall("VCD"):
        ct_idx = _safe_int(node.findtext("CtIdx"), default=-1)
        if ct_idx < 0 or ct_idx in records:
            continue
        records[ct_idx] = {
            "name": (node.findtext("Name") or "").strip(),
            "callsign_idx": _safe_int(node.findtext("CallsignIdx")),
            "callsign_slots": _safe_int(node.findtext("CallsignSlots")),
        }

    _VCD_RECORD_CACHE[path] = records
    return records


def _mission_info_from_code(
    mission_code: int,
    strings_by_id: dict[int, str],
) -> dict[str, Any]:
    """Map mission code to string id/name where the mapping is known."""

    strings_id: int | None = None
    mission_name: str | None = None
    if 0 <= mission_code <= 41:
        strings_id = 300 + mission_code
        mission_name = strings_by_id.get(strings_id)
    return {
        "mission_code": mission_code,
        "mission_strings_id": strings_id,
        "mission_name": mission_name,
    }


def _extract_squadron_callsign(
    record_blob: bytes,
    strings_by_id: dict[int, str],
) -> dict[str, Any] | None:
    """Extract squadron callsign fields from a known tail offset pattern."""

    if len(record_blob) < 34:
        return None

    # Some saves encode callsign fields at [-30,-29], others at [-34,-33].
    # Probe both and pick the first candidate that resolves to a valid callsign.
    best: dict[str, Any] | None = None
    for callsign_id_idx, callsign_num_idx in [(-30, -29), (-34, -33)]:
        callsign_id = record_blob[callsign_id_idx]
        callsign_num = record_blob[callsign_num_idx]
        strings_id = 2000 + callsign_id
        callsign_root = strings_by_id.get(strings_id)
        callsign: str | None = None
        if callsign_root and 1 <= callsign_num <= 9:
            callsign = f"{callsign_root}{callsign_num}"
        candidate = {
            "callsign_id": callsign_id,
            "callsign_num": callsign_num,
            "strings_id": strings_id,
            "callsign_root": callsign_root,
            "callsign": callsign,
        }
        if callsign is not None:
            return candidate
        if best is None or (callsign_id != 0 or callsign_num != 0):
            best = candidate

    return best


def _extract_squadron_mission(
    record_blob: bytes,
    strings_by_id: dict[int, str],
) -> dict[str, Any] | None:
    """Extract squadron mission code from a known tail offset pattern."""

    if len(record_blob) < 82:
        return None

    def build_from_offset(mission_idx: int, old_idx: int | None, source: str) -> dict[str, Any]:
        mission_code = record_blob[mission_idx]
        mission_info = _mission_info_from_code(mission_code, strings_by_id)
        payload: dict[str, Any] = {
            "mission_code": mission_info["mission_code"],
            "mission_strings_id": mission_info["mission_strings_id"],
            "mission_name": mission_info["mission_name"],
            "source": source,
        }
        if old_idx is not None:
            old_code = record_blob[old_idx]
            old_info = _mission_info_from_code(old_code, strings_by_id)
            payload["old_mission_code"] = old_info["mission_code"]
            payload["old_mission_strings_id"] = old_info["mission_strings_id"]
            payload["old_mission_name"] = old_info["mission_name"]
        return payload

    # Legacy layout for many records.
    primary = build_from_offset(-78, None, "record_tail_-78")
    mission_name = primary.get("mission_name")
    mission_code = primary.get("mission_code")
    if isinstance(mission_name, str) and mission_name.strip() and mission_name.upper() != "NONE":
        return primary
    if isinstance(mission_code, int) and mission_code != 0:
        return primary

    # Some saves keep current/old squadron mission as the adjacent tail pair.
    pair_current = build_from_offset(-81, -82, "record_tail_-81/-82")
    pair_name = pair_current.get("mission_name")
    pair_code = pair_current.get("mission_code")
    if isinstance(pair_name, str) and pair_name.strip() and pair_name.upper() != "NONE":
        return pair_current
    if isinstance(pair_code, int) and pair_code != 0:
        return pair_current

    pair_old = build_from_offset(-82, -81, "record_tail_-82/-81")
    old_name = pair_old.get("mission_name")
    old_code = pair_old.get("mission_code")
    if isinstance(old_name, str) and old_name.strip() and old_name.upper() != "NONE":
        return pair_old
    if isinstance(old_code, int) and old_code != 0:
        return pair_old

    return primary


def _skip_waypoint(reader: BinaryReader, version: int) -> None:
    """Skip one serialized waypoint record while staying version-aware."""

    haves = reader.read_u8()
    reader.read_i16()
    reader.read_i16()
    reader.read_i16()
    reader.read_u32()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    if version < 72:
        reader.read_u16()
    else:
        reader.read_u32()
    if haves & 0x02:
        reader.read_u32()
        reader.read_u32()
        reader.read_u8()
    if haves & 0x01:
        reader.read_u32()


def _skip_unit_waypoints(reader: BinaryReader, version: int) -> None:
    """Skip a waypoint array and enforce a sanity cap against bad data."""

    if version >= 71:
        num_waypoints = reader.read_u16()
    else:
        num_waypoints = reader.read_u8()
    if num_waypoints > 500:
        raise ParseError(f"suspicious unit waypoint count {num_waypoints}")
    for _ in range(num_waypoints):
        _skip_waypoint(reader, version)


def _skip_campaign_and_unit_prefix(reader: BinaryReader, version: int) -> int:
    """Skip common CampaignBase+Unit prefix and return unit flags."""

    reader.read_i16()
    reader.read_u32()
    reader.read_u32()
    reader.read_u16()
    reader.read_i16()
    reader.read_i16()
    if version >= 70:
        reader.read_f32()
    reader.read_u32()
    reader.read_i16()
    reader.read_i16()
    reader.read_u8()
    reader.read_i16()

    reader.read_u32()
    reader.read_i32()
    unit_flags = reader.read_i32()
    reader.read_i16()
    reader.read_i16()
    reader.read_u32()
    reader.read_u32()
    if version > 1:
        reader.read_u32()
        reader.read_u32()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    if version >= 71:
        reader.read_u16()
    else:
        reader.read_u8()
    reader.read_i16()
    reader.read_i16()
    _skip_unit_waypoints(reader, version)
    return unit_flags


def _extract_unit_waypoints(
    record_blob: bytes,
    container_version: int | None,
) -> list[dict[str, Any]] | None:
    """Decode unit waypoints from an inferred unit record layout."""

    if not isinstance(container_version, int):
        return None

    try:
        reader = BinaryReader(record_blob)
        reader.read_i16()
        reader.read_u32()
        reader.read_u32()
        reader.read_u16()
        reader.read_i16()
        reader.read_i16()
        if container_version >= 70:
            reader.read_f32()
        reader.read_u32()
        reader.read_i16()
        reader.read_i16()
        reader.read_u8()
        reader.read_i16()

        reader.read_u32()
        reader.read_i32()
        reader.read_i32()
        reader.read_i16()
        reader.read_i16()
        reader.read_u32()
        reader.read_u32()
        if container_version > 1:
            reader.read_u32()
            reader.read_u32()
        reader.read_u8()
        reader.read_u8()
        reader.read_u8()
        if container_version >= 71:
            num_waypoints = reader.read_u16()
        else:
            num_waypoints = reader.read_u8()
        if num_waypoints < 0 or num_waypoints > 500:
            return None
        reader.read_i16()
        reader.read_i16()

        waypoints: list[dict[str, Any]] = []
        for index in range(num_waypoints):
            # `haves` bitmask gates optional target/departure fields.
            haves = reader.read_u8()
            x = reader.read_i16()
            y = reader.read_i16()
            z = reader.read_i16()
            arrive = reader.read_u32()
            action = reader.read_u8()
            route_action = reader.read_u8()
            packed_formation = reader.read_u8()
            if container_version < 72:
                flags = reader.read_u16()
            else:
                flags = reader.read_u32()

            target_id: dict[str, int] | None = None
            target_building: int | None = None
            if haves & 0x02:
                target_id = {"num": reader.read_u32(), "creator": reader.read_u32()}
                target_building = reader.read_u8()

            depart: int | None = None
            if haves & 0x01:
                depart = reader.read_u32()

            waypoints.append(
                {
                    "index": index,
                    "haves": haves,
                    "x": x,
                    "y": y,
                    "z": z,
                    "arrive_ms": arrive,
                    "arrive_z": _format_campaign_time_z(arrive),
                    "action": action,
                    "route_action": route_action,
                    "formation": packed_formation & 0x0F,
                    "formation_spacing": ((packed_formation >> 4) & 0x0F) - 8,
                    "flags": flags,
                    "target_id": target_id,
                    "target_building": target_building,
                    "depart_ms": depart,
                    "depart_z": _format_campaign_time_z(depart),
                }
            )

        return waypoints
    except ParseError:
        return None


def _extract_timing_from_waypoints(
    waypoints: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Infer takeoff/push/TOT style timings from waypoint sequences."""

    if not waypoints:
        return None

    usable = [
        wp
        for wp in waypoints
        if isinstance(wp.get("arrive_ms"), int)
        and wp["arrive_ms"] > 120000
        and not (wp.get("x") == 0 and wp.get("y") == 0 and wp.get("flags") == 0xFF000000)
    ]
    if not usable:
        return None

    takeoff_wp = next((wp for wp in usable if wp.get("action") == 1), usable[0])

    push_wp: dict[str, Any] | None = next(
        (
            wp
            for wp in usable
            if wp["index"] > takeoff_wp["index"]
            and isinstance(wp.get("arrive_ms"), int)
            and wp["arrive_ms"] > takeoff_wp["arrive_ms"]
            and wp.get("action") == 2
        ),
        None,
    )

    if push_wp is None:
        push_wp = next(
            (
                wp
                for wp in usable
                if wp["index"] > takeoff_wp["index"]
                and isinstance(wp.get("arrive_ms"), int)
                and wp["arrive_ms"] > takeoff_wp["arrive_ms"]
                and wp.get("action") in {8, 9}
            ),
            None,
        )

    if push_wp is None:
        push_wp = next(
            (
                wp
                for wp in usable
                if wp["index"] > takeoff_wp["index"]
                and isinstance(wp.get("arrive_ms"), int)
                and wp["arrive_ms"] > takeoff_wp["arrive_ms"]
            ),
            None,
        )

    pivot_index = push_wp["index"] if push_wp is not None else takeoff_wp["index"]
    pivot_time = push_wp["arrive_ms"] if push_wp is not None else takeoff_wp["arrive_ms"]
    tot_wp = next(
        (
            wp
            for wp in usable
            if wp["index"] > pivot_index
            and isinstance(wp.get("arrive_ms"), int)
            and wp["arrive_ms"] >= pivot_time
            and wp.get("action") in {10, 11, 12, 13, 14, 15, 16, 17, 18}
        ),
        None,
    )

    if tot_wp is None:
        tot_wp = next(
            (
                wp
                for wp in usable
                if wp["index"] > pivot_index
                and isinstance(wp.get("arrive_ms"), int)
                and wp["arrive_ms"] >= pivot_time
            ),
            None,
        )

    return {
        "takeoff_time_ms": takeoff_wp.get("arrive_ms"),
        "takeoff_time_z": takeoff_wp.get("arrive_z"),
        "takeoff_waypoint_index": takeoff_wp.get("index"),
        "push_time_ms": push_wp.get("arrive_ms") if push_wp is not None else None,
        "push_time_z": push_wp.get("arrive_z") if push_wp is not None else None,
        "push_waypoint_index": push_wp.get("index") if push_wp is not None else None,
        "time_on_target_ms": tot_wp.get("arrive_ms") if tot_wp is not None else None,
        "time_on_target_z": tot_wp.get("arrive_z") if tot_wp is not None else None,
        "tot_waypoint_index": tot_wp.get("index") if tot_wp is not None else None,
        "time_off_target_ms": tot_wp.get("depart_ms") if tot_wp is not None else None,
        "time_off_target_z": tot_wp.get("depart_z") if tot_wp is not None else None,
    }


def _waypoint_array_quality(waypoints: list[dict[str, Any]] | None) -> int:
    if not isinstance(waypoints, list) or not waypoints:
        return -999
    actions = [wp.get("action") for wp in waypoints if isinstance(wp, dict)]
    arrive = [wp.get("arrive_ms") for wp in waypoints if isinstance(wp, dict)]
    good_actions = sum(1 for action in actions if isinstance(action, int) and 0 <= action <= 32)
    good_coords = sum(
        1
        for wp in waypoints
        if isinstance(wp, dict)
        and isinstance(wp.get("x"), int)
        and isinstance(wp.get("y"), int)
        and -4096 <= wp["x"] <= 4096
        and -4096 <= wp["y"] <= 4096
    )
    plausible_arrive = sum(
        1 for value in arrive if isinstance(value, int) and 120000 <= value <= 600000000
    )
    has_takeoff = any(action == 1 for action in actions)
    has_terminal = any(
        isinstance(action, int) and action in {10, 11, 12, 13, 14, 15, 16, 17, 18}
        for action in actions
    )
    score = good_actions * 3 + good_coords + plausible_arrive * 2 + len(waypoints)
    if has_takeoff:
        score += 20
    if has_terminal:
        score += 8
    first_action = actions[0] if actions else None
    if first_action == 1:
        score += 15
    elif isinstance(first_action, int) and first_action > 32:
        score -= 20
    if len(waypoints) <= 1:
        score -= 30
    return score


def _extract_unit_waypoints_scan(
    record_blob: bytes,
    container_version: int | None,
) -> list[dict[str, Any]] | None:
    """Fallback scan for waypoint arrays when prefix-based decoding misaligns."""

    if not isinstance(container_version, int):
        return None

    def parse_from_offset(offset: int) -> list[dict[str, Any]] | None:
        reader = BinaryReader(record_blob[offset:])
        if container_version >= 71:
            num_waypoints = reader.read_u16()
        else:
            num_waypoints = reader.read_u8()
        if num_waypoints <= 1 or num_waypoints > 20:
            return None
        waypoints: list[dict[str, Any]] = []
        for index in range(num_waypoints):
            haves = reader.read_u8()
            x = reader.read_i16()
            y = reader.read_i16()
            z = reader.read_i16()
            arrive = reader.read_u32()
            action = reader.read_u8()
            route_action = reader.read_u8()
            packed_formation = reader.read_u8()
            if container_version < 72:
                flags = reader.read_u16()
            else:
                flags = reader.read_u32()

            target_id: dict[str, int] | None = None
            target_building: int | None = None
            if haves & 0x02:
                target_id = {"num": reader.read_u32(), "creator": reader.read_u32()}
                target_building = reader.read_u8()

            depart: int | None = None
            if haves & 0x01:
                depart = reader.read_u32()

            waypoints.append(
                {
                    "index": index,
                    "haves": haves,
                    "x": x,
                    "y": y,
                    "z": z,
                    "arrive_ms": arrive,
                    "arrive_z": _format_campaign_time_z(arrive),
                    "action": action,
                    "route_action": route_action,
                    "formation": packed_formation & 0x0F,
                    "formation_spacing": ((packed_formation >> 4) & 0x0F) - 8,
                    "flags": flags,
                    "target_id": target_id,
                    "target_building": target_building,
                    "depart_ms": depart,
                    "depart_z": _format_campaign_time_z(depart),
                }
            )
        return waypoints

    def score_candidate(waypoints: list[dict[str, Any]]) -> int:
        score = _waypoint_array_quality(waypoints)
        if not waypoints:
            return score

        first_action = waypoints[0].get("action")
        if first_action == 1:
            score += 40
        else:
            score -= 25

        timing = _extract_timing_from_waypoints(waypoints)
        if not isinstance(timing, dict):
            return score - 40

        takeoff_ms = timing.get("takeoff_time_ms")
        push_ms = timing.get("push_time_ms")
        tot_ms = timing.get("time_on_target_ms")

        if isinstance(takeoff_ms, int):
            score += 20
        else:
            score -= 20

        if isinstance(push_ms, int):
            score += 25
        else:
            score -= 15

        if isinstance(tot_ms, int):
            score += 25
        else:
            score -= 10

        if isinstance(takeoff_ms, int) and isinstance(push_ms, int):
            dt = push_ms - takeoff_ms
            if 180000 <= dt <= 7200000:
                score += 10
            else:
                score -= 20
        if isinstance(push_ms, int) and isinstance(tot_ms, int):
            dt = tot_ms - push_ms
            if 180000 <= dt <= 7200000:
                score += 10
            else:
                score -= 20
        return score

    for require_takeoff_start in (True, False):
        best_waypoints: list[dict[str, Any]] | None = None
        best_score = -999
        for offset in range(0, max(0, len(record_blob) - 4)):
            try:
                waypoints = parse_from_offset(offset)
            except ParseError:
                continue
            if not waypoints:
                continue
            if require_takeoff_start and waypoints[0].get("action") != 1:
                continue
            score = score_candidate(waypoints)
            if score > best_score:
                best_score = score
                best_waypoints = waypoints
        if best_waypoints is not None:
            return best_waypoints
    return None


def _waypoint_merge_key(waypoint: dict[str, Any]) -> tuple[Any, ...]:
    target_id = waypoint.get("target_id")
    target_key: tuple[int, int] | None = None
    if isinstance(target_id, dict):
        num = target_id.get("num")
        creator = target_id.get("creator")
        if isinstance(num, int) and isinstance(creator, int):
            target_key = (num, creator)
    return (
        waypoint.get("x"),
        waypoint.get("y"),
        waypoint.get("z"),
        waypoint.get("arrive_ms"),
        waypoint.get("depart_ms"),
        waypoint.get("action"),
        waypoint.get("route_action"),
        target_key,
        waypoint.get("target_building"),
    )


def _sanitize_waypoint_sequence(
    waypoints: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not isinstance(waypoints, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for waypoint in waypoints:
        if not isinstance(waypoint, dict):
            continue

        arrive_ms = waypoint.get("arrive_ms")
        if isinstance(arrive_ms, int) and arrive_ms < 120000:
            continue

        if (
            waypoint.get("x") == 0
            and waypoint.get("y") == 0
            and waypoint.get("z") == 0
            and waypoint.get("action") == 0
            and waypoint.get("route_action") == 0
            and waypoint.get("flags") == 0xFF000000
        ):
            continue

        cleaned.append(dict(waypoint))

    deduped: list[dict[str, Any]] = []
    last_key: tuple[Any, ...] | None = None
    for waypoint in cleaned:
        current_key = _waypoint_merge_key(waypoint)
        if current_key == last_key:
            continue
        deduped.append(waypoint)
        last_key = current_key

    truncated: list[dict[str, Any]] = []
    for waypoint in deduped:
        truncated.append(waypoint)
        if waypoint.get("action") in {7, 27} and len(truncated) > 1:
            break

    for index, waypoint in enumerate(truncated):
        waypoint["index"] = index
    return truncated


def _scan_waypoint_candidates(
    record_blob: bytes,
    container_version: int | None,
) -> list[list[dict[str, Any]]]:
    if not isinstance(container_version, int):
        return []

    candidates: list[tuple[int, list[dict[str, Any]]]] = []

    for offset in range(0, max(0, len(record_blob) - 4)):
        try:
            reader = BinaryReader(record_blob[offset:])
            if container_version >= 71:
                num_waypoints = reader.read_u16()
            else:
                num_waypoints = reader.read_u8()
            if num_waypoints <= 1 or num_waypoints > 20:
                continue

            waypoints: list[dict[str, Any]] = []
            for index in range(num_waypoints):
                haves = reader.read_u8()
                x = reader.read_i16()
                y = reader.read_i16()
                z = reader.read_i16()
                arrive = reader.read_u32()
                action = reader.read_u8()
                route_action = reader.read_u8()
                packed_formation = reader.read_u8()
                if container_version < 72:
                    flags = reader.read_u16()
                else:
                    flags = reader.read_u32()

                target_id: dict[str, int] | None = None
                target_building: int | None = None
                if haves & 0x02:
                    target_id = {"num": reader.read_u32(), "creator": reader.read_u32()}
                    target_building = reader.read_u8()

                depart: int | None = None
                if haves & 0x01:
                    depart = reader.read_u32()

                waypoints.append(
                    {
                        "index": index,
                        "haves": haves,
                        "x": x,
                        "y": y,
                        "z": z,
                        "arrive_ms": arrive,
                        "arrive_z": _format_campaign_time_z(arrive),
                        "action": action,
                        "route_action": route_action,
                        "formation": packed_formation & 0x0F,
                        "formation_spacing": ((packed_formation >> 4) & 0x0F) - 8,
                        "flags": flags,
                        "target_id": target_id,
                        "target_building": target_building,
                        "depart_ms": depart,
                        "depart_z": _format_campaign_time_z(depart),
                    }
                )
        except ParseError:
            continue

        score = _waypoint_array_quality(waypoints)
        if score < 0:
            continue
        candidates.append((score, waypoints))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [waypoints for _, waypoints in candidates]


def _merge_waypoint_tail_candidates(
    primary_waypoints: list[dict[str, Any]] | None,
    candidate_waypoints: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    merged = _sanitize_waypoint_sequence(primary_waypoints)
    if not merged:
        return []

    seen = {_waypoint_merge_key(waypoint) for waypoint in merged}
    last_key = _waypoint_merge_key(merged[-1])

    for candidate in candidate_waypoints:
        sanitized_candidate = _sanitize_waypoint_sequence(candidate)
        if len(sanitized_candidate) < 2:
            continue

        merged_keys = [_waypoint_merge_key(waypoint) for waypoint in merged]
        candidate_keys = [_waypoint_merge_key(waypoint) for waypoint in sanitized_candidate]

        overlap_len = 0
        for start_idx in range(len(merged_keys)):
            current_overlap = 0
            while (
                start_idx + current_overlap < len(merged_keys)
                and current_overlap < len(candidate_keys)
                and merged_keys[start_idx + current_overlap] == candidate_keys[current_overlap]
            ):
                current_overlap += 1
            if current_overlap > overlap_len:
                overlap_len = current_overlap

        if overlap_len < 2:
            continue

        appended = False
        for waypoint in sanitized_candidate[overlap_len:]:
            key = _waypoint_merge_key(waypoint)
            if key in seen:
                continue
            merged.append(dict(waypoint))
            seen.add(key)
            appended = True
        if appended:
            last_key = _waypoint_merge_key(merged[-1])

    for index, waypoint in enumerate(merged):
        waypoint["index"] = index
    return merged


def _extract_flight_mission(
    record_blob: bytes,
    container_version: int | None,
    strings_by_id: dict[int, str],
) -> dict[str, Any] | None:
    """Decode mission block from a flight record when layout is compatible."""

    if not isinstance(container_version, int):
        return None
    try:
        reader = BinaryReader(record_blob)
        _skip_campaign_and_unit_prefix(reader, container_version)
        reader.read_f32()
        reader.read_i32()
        reader.read_u32()
        reader.read_u32()
        time_on_target = reader.read_u32()
        mission_over_time = reader.read_u32()
        reader.read_i16()

        if container_version < 24:
            return None

        loadouts = reader.read_u8()
        per_loadout_size = (16 * (2 if container_version >= 73 else 1)) + 16
        reader.read_bytes(loadouts * per_loadout_size)

        mission = reader.read_u8()
        if container_version > 65:
            old_mission = reader.read_u8()
        else:
            old_mission = mission

        reader.read_u8()
        reader.read_u8()
        mission_id = reader.read_u8()

        if container_version < 14:
            reader.read_u8()
        eval_flags = reader.read_u8()

        if container_version > 65:
            mission_context = reader.read_u8()
        else:
            mission_context = 0
    except ParseError:
        return None

    slots: list[int] | None = None
    pilots: list[int] | None = None
    plane_stats: list[int] | None = None
    player_slots: list[int] | None = None
    callsign_id: int | None = None
    callsign_num: int | None = None
    try:
        # Optional tail block with per-element occupancy. Keep this best-effort so
        # mission parsing still succeeds when offsets drift between versions.
        reader.read_u32()
        reader.read_u32()
        reader.read_u32()
        reader.read_u32()
        if container_version > 65:
            reader.read_u32()
            reader.read_u32()
        slots = [reader.read_u8() for _ in range(4)]
        pilots = [reader.read_u8() for _ in range(4)]
        plane_stats = [reader.read_u8() for _ in range(4)]
        player_slots = [reader.read_u8() for _ in range(4)]
        reader.read_u8()
        callsign_id = reader.read_u8()
        callsign_num = reader.read_u8()
        if container_version >= 72 and reader.remaining() >= 4:
            reader.read_u32()
    except ParseError:
        pass

    aircraft_count: int | None = None
    if slots is not None:
        slot_count = sum(1 for value in slots if value != 0xFF)
        if 0 <= slot_count <= 4:
            aircraft_count = slot_count
    if aircraft_count is None and pilots is not None:
        pilot_count = sum(1 for value in pilots if value != 0xFF)
        if 0 <= pilot_count <= 4:
            aircraft_count = pilot_count

    current_info = _mission_info_from_code(mission, strings_by_id)
    old_info = _mission_info_from_code(old_mission, strings_by_id)
    payload = {
        "mission_code": current_info["mission_code"],
        "mission_strings_id": current_info["mission_strings_id"],
        "mission_name": current_info["mission_name"],
        "old_mission_code": old_info["mission_code"],
        "old_mission_strings_id": old_info["mission_strings_id"],
        "old_mission_name": old_info["mission_name"],
        "mission_id": mission_id,
        "mission_context": mission_context,
        "eval_flags": eval_flags,
        "time_on_target_ms": time_on_target,
        "time_on_target_z": _format_campaign_time_z(time_on_target),
        "mission_over_time_ms": mission_over_time,
        "mission_over_time_z": _format_campaign_time_z(mission_over_time),
    }
    if isinstance(aircraft_count, int):
        payload["aircraft_count"] = aircraft_count
    if slots is not None:
        payload["slots"] = slots
    if pilots is not None:
        payload["pilots"] = pilots
    if plane_stats is not None:
        payload["plane_stats"] = plane_stats
    if player_slots is not None:
        payload["player_slots"] = player_slots
    if isinstance(callsign_id, int):
        payload["callsign_id"] = callsign_id
    if isinstance(callsign_num, int):
        payload["callsign_num"] = callsign_num
    return payload


def _extract_package_mission(
    record_blob: bytes,
    container_version: int | None,
    strings_by_id: dict[int, str],
) -> dict[str, Any] | None:
    """Decode mission block from a package record when layout is compatible."""

    if not isinstance(container_version, int):
        return None
    try:
        reader = BinaryReader(record_blob)
        unit_flags = _skip_campaign_and_unit_prefix(reader, container_version)
        final = bool(unit_flags & 0x00100000)

        elements = reader.read_u8()
        if elements > 32:
            return None
        reader.read_bytes(elements * 8)
        reader.read_bytes(8)
        if container_version >= 7:
            reader.read_bytes(32)

        wait_cycles = reader.read_u8()
        if final and wait_cycles == 0:
            reader.read_i16()
            if container_version < 35:
                reader.read_i16()
            reader.read_i16()
            mission = reader.read_i16() & 0xFF
            context = reader.read_i16() & 0xFF
            reader.read_bytes(16)
            if container_version >= 16:
                mission_tot = reader.read_u32()
            else:
                mission_tot = None
            if container_version >= 35:
                action_type = reader.read_u8()
            else:
                action_type = 0
            takeoff_time = None
            push_time = None
        else:
            reader.read_u8()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            takeoff_time = reader.read_u32()
            push_time = reader.read_u32()
            reader.read_u32()
            reader.read_i16()
            reader.read_i16()
            if container_version < 35:
                reader.read_i16()
            reader.read_i16()

            num_ingress = reader.read_u8()
            if num_ingress > 64:
                return None
            for _ in range(num_ingress):
                _skip_waypoint(reader, container_version)

            num_egress = reader.read_u8()
            if num_egress > 64:
                return None
            for _ in range(num_egress):
                _skip_waypoint(reader, container_version)

            reader.read_bytes(32)
            reader.read_u8()
            reader.read_u8()
            reader.read_bytes(2)
            mission_tot = reader.read_u32()
            reader.read_i16()
            reader.read_i16()
            reader.read_u32()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_i16()
            reader.read_u8()
            action_type = reader.read_u8()
            mission = reader.read_u8()
            reader.read_u8()
            reader.read_u8()
            context = reader.read_u8()
            reader.read_u8()

            if container_version >= 35:
                reader.read_u8()
                reader.read_u8()
                reader.read_u8()
                reader.read_bytes(4)
                reader.read_u8()
                reader.read_u8()
                reader.read_bytes(3)
    except ParseError:
        return None

    mission_info = _mission_info_from_code(mission, strings_by_id)
    return {
        "mission_code": mission_info["mission_code"],
        "mission_strings_id": mission_info["mission_strings_id"],
        "mission_name": mission_info["mission_name"],
        "action_type": action_type,
        "context": context,
        "takeoff_time_ms": takeoff_time,
        "takeoff_time_z": _format_campaign_time_z(takeoff_time),
        "push_time_ms": push_time,
        "push_time_z": _format_campaign_time_z(push_time),
        "time_on_target_ms": mission_tot,
        "time_on_target_z": _format_campaign_time_z(mission_tot),
    }


def _extract_package_team_id(record_blob: bytes) -> int | None:
    if len(record_blob) <= 28:
        return None
    value = record_blob[28]
    if 0 <= value <= 7:
        return value
    return None


def _callsign_profile_for_unit_ct(
    unit_ct_idx: int,
    ucd_records: dict[int, dict[str, Any]],
    vcd_records: dict[int, dict[str, int | str]],
) -> dict[str, Any] | None:
    """Resolve callsign-capable vehicle info for a unit class-table index."""

    ucd = ucd_records.get(unit_ct_idx)
    if ucd is None:
        return None

    vehicle_candidates = ucd.get("vehicle_ct_indices") or []
    vehicle_ct_idx = vehicle_candidates[0] if vehicle_candidates else None
    if vehicle_ct_idx is None:
        return {
            "ucd_name": ucd.get("name", ""),
            "vehicle_ct_index": None,
        }

    vcd = vcd_records.get(vehicle_ct_idx)
    if vcd is None:
        return {
            "ucd_name": ucd.get("name", ""),
            "vehicle_ct_index": vehicle_ct_idx,
        }

    return {
        "ucd_name": ucd.get("name", ""),
        "vehicle_ct_index": vehicle_ct_idx,
        "vehicle_name": vcd.get("name", ""),
        "callsign_idx": int(vcd.get("callsign_idx", 0)),
        "callsign_slots": int(vcd.get("callsign_slots", 0)),
    }


def _unit_kind_from_ct(domain: int, unit_type: int) -> str | None:
    """Classify CT domain/type pairs into campaign unit families."""

    mapping = {
        (2, 1): "squadron",
        (2, 2): "package",
        (2, 3): "flight",
        (3, 1): "brigade",
        (3, 2): "battalion",
        (4, 1): "taskforce",
    }
    return mapping.get((domain, unit_type))


def _extract_air_task_number(blob: bytes) -> int | None:
    """Extract duplicated 16-bit air-task number used by air package records."""

    if len(blob) < 70:
        return None
    number_a = struct.unpack_from("<H", blob, 29)[0]
    number_b = struct.unpack_from("<H", blob, 68)[0]
    if number_a != number_b:
        return None
    return number_a


def _extract_unit_roster_state(
    record_blob: bytes,
    container_version: int | None,
) -> dict[str, int] | None:
    """Decode common Unit fields useful for lightweight status inference."""

    if not isinstance(container_version, int):
        return None
    try:
        reader = BinaryReader(record_blob)
        reader.read_i16()
        reader.read_u32()
        reader.read_u32()
        reader.read_u16()
        reader.read_i16()
        reader.read_i16()
        if container_version >= 70:
            reader.read_f32()
        reader.read_u32()
        reader.read_i16()
        reader.read_i16()
        reader.read_u8()
        reader.read_i16()

        last_check = reader.read_u32()
        roster = reader.read_i32()
        unit_flags = reader.read_i32()
        reader.read_i16()
        reader.read_i16()
        reader.read_u32()
        reader.read_u32()
        if container_version > 1:
            reader.read_u32()
            reader.read_u32()
        moved = reader.read_u8()
        losses = reader.read_u8()
        tactic = reader.read_u8()
        return {
            "last_check": last_check,
            "roster": roster,
            "unit_flags": unit_flags,
            "moved": moved,
            "losses": losses,
            "tactic": tactic,
        }
    except ParseError:
        return None


def _extract_unit_campaign_id(record_blob: bytes) -> int | None:
    if len(record_blob) < 31:
        return None
    return struct.unpack_from("<H", record_blob, 29)[0]


def _extract_squadron_aircraft_count(record_blob: bytes) -> int | None:
    # In 4.38 squadron-kind package slot records, the tail keeps four per-aircraft
    # state bytes at [-43:-39]. Non-zero entries correspond to present airframes.
    if len(record_blob) < 43:
        return None
    plane_state = record_blob[-43:-39]
    count = sum(1 for value in plane_state if value != 0)
    if 1 <= count <= 4:
        return count
    return None


def _scan_unit_record_starts(
    data: bytes,
    ct_records: dict[int, dict[str, int]],
) -> list[int]:
    """Heuristically locate unit record boundaries inside decompressed `.uni`."""

    starts: list[int] = []
    max_start = max(0, len(data) - 12)
    for offset in range(max_start + 1):
        unit_type = struct.unpack_from("<h", data, offset)[0]
        ct_idx = unit_type - 100
        ct_info = ct_records.get(ct_idx)
        if ct_info is None:
            continue

        kind = _unit_kind_from_ct(ct_info["domain"], ct_info["type"])
        if kind is None:
            continue

        unit_num, unit_creator = struct.unpack_from("<II", data, offset + 2)
        if unit_num > 1_000_000 or unit_creator > 1_000_000:
            continue

        entity_type = struct.unpack_from("<H", data, offset + 10)[0]
        if entity_type != unit_type:
            continue

        # Offset is a plausible record start based on CT + structural checks.
        starts.append(offset)

    return starts


def _build_package_and_flight_indexes(
    records: list[dict[str, Any]],
    data: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build package/flight link structures from parsed unit records."""

    id_to_record: dict[tuple[int, int], dict[str, Any]] = {}
    for rec in records:
        rec_id = _to_vuid_tuple(rec.get("id"))
        if rec_id is not None:
            id_to_record[rec_id] = rec

    id_pattern = {
        struct.pack("<II", rec_id[0], rec_id[1]): rec_id for rec_id in id_to_record
    }

    packages: list[dict[str, Any]] = []
    flights: list[dict[str, Any]] = []
    package_flight_links: list[dict[str, Any]] = []
    flight_to_packages: dict[tuple[int, int], list[dict[str, int]]] = defaultdict(list)

    for rec in records:
        if rec.get("kind") != "package":
            continue

        package_id = _to_vuid_tuple(rec.get("id"))
        if package_id is None:
            continue

        start = int(rec.get("offset", 0))
        size = int(rec.get("size", 0))
        blob = data[start : start + size]

        first_position_by_id: dict[tuple[int, int], int] = {}
        for pos in range(max(0, len(blob) - 7)):
            # Each VU_ID is an 8-byte `<II>` pattern; first hit is retained.
            ref_id = id_pattern.get(blob[pos : pos + 8])
            if ref_id is None or ref_id == package_id:
                continue
            if ref_id not in first_position_by_id:
                first_position_by_id[ref_id] = pos

        unit_refs: list[dict[str, Any]] = []
        flight_refs: list[dict[str, Any]] = []
        for ref_id, pos in sorted(first_position_by_id.items(), key=lambda item: item[1]):
            target = id_to_record.get(ref_id)
            if target is None:
                continue
            ref_entry = {
                "offset": pos,
                "id": {"num": ref_id[0], "creator": ref_id[1]},
                "kind": target.get("kind"),
            }
            unit_refs.append(ref_entry)
            if target.get("kind") == "flight":
                flight_refs.append(ref_entry)
                package_flight_links.append(
                    {
                        "package_id": {"num": package_id[0], "creator": package_id[1]},
                        "flight_id": {"num": ref_id[0], "creator": ref_id[1]},
                        "source_offset": pos,
                    }
                )
                flight_to_packages[ref_id].append(
                    {"num": package_id[0], "creator": package_id[1]}
                )

        primary_flight_candidate: dict[str, int] | None = None
        if len(blob) >= 136:
            primary_flight_num = struct.unpack_from("<I", blob, 132)[0]
            primary_flight_id = (primary_flight_num, 0)
            target = id_to_record.get(primary_flight_id)
            if target is not None and target.get("kind") == "flight":
                primary_flight_candidate = {
                    "num": primary_flight_id[0],
                    "creator": primary_flight_id[1],
                    "source_offset": 132,
                }

        package_number = _extract_air_task_number(blob)
        package_number_copy: int | None = None
        if len(blob) >= 70:
            package_number_copy = struct.unpack_from("<H", blob, 68)[0]

        planned_flight_count: int | None = None
        planned_flight_slots: list[dict[str, Any]] = []
        if len(blob) >= 75:
            planned_flight_count = blob[74]
            max_slots_by_size = max(0, (len(blob) - 75) // 8)
            slot_count = min(planned_flight_count, max_slots_by_size)
            for slot_index in range(slot_count):
                slot_offset = 75 + slot_index * 8
                slot_num, slot_creator = struct.unpack_from("<II", blob, slot_offset)
                if slot_num == 0 and slot_creator == 0:
                    continue
                target = id_to_record.get((slot_num, slot_creator))
                slot_item: dict[str, Any] = {
                    "slot_index": slot_index,
                    "offset": slot_offset,
                    "id": {"num": slot_num, "creator": slot_creator},
                }
                if target is not None:
                    slot_item["kind"] = target.get("kind")
                    if isinstance(target.get("air_task_number"), int):
                        slot_item["air_task_number"] = target.get("air_task_number")
                    if isinstance(target.get("flight_mission"), dict):
                        slot_item["flight_mission"] = target.get("flight_mission")
                    if isinstance(target.get("squadron_mission"), dict):
                        slot_item["squadron_mission"] = target.get("squadron_mission")
                    if isinstance(target.get("timing"), dict):
                        slot_item["timing"] = target.get("timing")
                else:
                    slot_item["kind"] = "unknown"
                planned_flight_slots.append(slot_item)

        packages.append(
            {
                "id": rec.get("id"),
                "package_number": package_number,
                "package_number_copy": package_number_copy,
                "planned_flight_count": planned_flight_count,
                "planned_flight_slots": planned_flight_slots,
                "offset": rec.get("offset"),
                "size": rec.get("size"),
                "x": rec.get("x"),
                "y": rec.get("y"),
                "ct_index": rec.get("ct_index"),
                "package_mission": rec.get("package_mission"),
                "package_team_id": rec.get("package_team_id"),
                "timing": rec.get("timing"),
                "waypoints": rec.get("waypoints"),
                "primary_flight_candidate": primary_flight_candidate,
                "flight_refs": flight_refs,
                "unit_refs": unit_refs,
            }
        )

    for rec in records:
        if rec.get("kind") != "flight":
            continue
        flight_id = _to_vuid_tuple(rec.get("id"))
        if flight_id is None:
            continue
        flights.append(
            {
                "id": rec.get("id"),
                "offset": rec.get("offset"),
                "size": rec.get("size"),
                "x": rec.get("x"),
                "y": rec.get("y"),
                "unit_type": rec.get("unit_type"),
                "ct_index": rec.get("ct_index"),
                "air_task_number": rec.get("air_task_number"),
                "flight_mission": rec.get("flight_mission"),
                "timing": rec.get("timing"),
                "callsign_profile": rec.get("callsign_profile"),
                "packages": flight_to_packages.get(flight_id, []),
            }
        )

    return packages, flights, package_flight_links


def _parse_uni(data: bytes, ctx: ParseContext) -> dict[str, Any]:
    """Parse `.uni` unit stream into package/flight-centric structures."""

    ct_path = _find_support_file("FALCON4_CT.XML", support_base_dir=ctx.support_base_dir)
    if ct_path is None:
        return {
            "file_type": "uni",
            "size": len(data),
            "note": (
                "CT-backed parsing needs objects_cf/FALCON4_CT.XML. "
                "Copy BMS objects XML files into objects_cf/."
            ),
            "head_hex": data[:64].hex(),
        }

    ct_records = _load_ct_records(ct_path)
    ucd_records: dict[int, dict[str, Any]] = {}
    vcd_records: dict[int, dict[str, int | str]] = {}

    ucd_path = _find_support_file("FALCON4_UCD.XML", support_base_dir=ctx.support_base_dir)
    if ucd_path is not None:
        ucd_records = _load_ucd_records(ucd_path)

    vcd_path = _find_support_file("FALCON4_VCD.XML", support_base_dir=ctx.support_base_dir)
    if vcd_path is not None:
        vcd_records = _load_vcd_records(vcd_path)

    strings_path = _find_support_file("Strings.txt", support_base_dir=ctx.support_base_dir)
    if strings_path is None:
        strings_path = _find_support_file("strings.txt", support_base_dir=ctx.support_base_dir)
    strings_by_id: dict[int, str] = {}
    if strings_path is not None:
        strings_by_id = _load_strings_by_id(strings_path)

    starts = _scan_unit_record_starts(data, ct_records)
    if not starts:
        return {
            "file_type": "uni",
            "size": len(data),
            "ct_path": str(ct_path),
            "note": "No valid unit records found with CT-backed scanner.",
            "head_hex": data[:64].hex(),
        }

    expected_records: int | None = None
    if isinstance(ctx.decode_metadata, dict):
        value = ctx.decode_metadata.get("record_count")
        if isinstance(value, int):
            expected_records = value

    records: list[dict[str, Any]] = []

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(data)
        size = end - start
        if size < 18:
            continue

        unit_type = struct.unpack_from("<h", data, start)[0]
        ct_idx = unit_type - 100
        ct_info = ct_records.get(ct_idx)
        if ct_info is None:
            continue

        kind = _unit_kind_from_ct(ct_info["domain"], ct_info["type"])
        if kind is None:
            continue

        unit_num, unit_creator = struct.unpack_from("<II", data, start + 2)
        entity_type = struct.unpack_from("<H", data, start + 10)[0]
        x = struct.unpack_from("<h", data, start + 12)[0]
        y = struct.unpack_from("<h", data, start + 14)[0]
        state16_at_16 = struct.unpack_from("<h", data, start + 16)[0]

        record_blob = data[start:end]

        record: dict[str, Any] = {
            "index": index,
            "kind": kind,
            "offset": start,
            "size": size,
            "unit_type": unit_type,
            "entity_type": entity_type,
            "id": {"num": unit_num, "creator": unit_creator},
            "x": x,
            "y": y,
            "state16_at_16": state16_at_16,
            "ct_index": ct_idx,
            "ct": {
                "domain": ct_info["domain"],
                "class": ct_info["class"],
                "type": ct_info["type"],
                "subtype": ct_info["subtype"],
                "specific": ct_info["specific"],
                "entity_idx": ct_info["entity_idx"],
            },
            "head_hex": record_blob[: min(64, size)].hex(),
        }

        air_task_number = _extract_air_task_number(record_blob)
        if air_task_number is not None:
            record["air_task_number"] = air_task_number

        unit_state = _extract_unit_roster_state(record_blob, ctx.container_version)
        if unit_state is not None:
            record["unit_roster"] = unit_state.get("roster")
            record["unit_flags"] = unit_state.get("unit_flags")
            record["unit_losses"] = unit_state.get("losses")
            record["unit_moved"] = unit_state.get("moved")
            record["unit_tactic"] = unit_state.get("tactic")
        campaign_id = _extract_unit_campaign_id(record_blob)
        if isinstance(campaign_id, int):
            record["campaign_id"] = campaign_id

        if ucd_records and vcd_records:
            callsign_profile = _callsign_profile_for_unit_ct(
                ct_idx,
                ucd_records,
                vcd_records,
            )
            if callsign_profile is not None:
                record["callsign_profile"] = callsign_profile

        waypoints = _extract_unit_waypoints(record_blob, ctx.container_version)
        waypoint_candidates: list[list[dict[str, Any]]] = []
        if kind == "squadron":
            primary_score = _waypoint_array_quality(waypoints)
            if primary_score < 20:
                scanned_waypoints = _extract_unit_waypoints_scan(
                    record_blob, ctx.container_version
                )
                scanned_score = _waypoint_array_quality(scanned_waypoints)
                if scanned_score > primary_score:
                    waypoints = scanned_waypoints
                    record["waypoint_parse_mode"] = "scan_fallback"
            waypoint_candidates = _scan_waypoint_candidates(record_blob, ctx.container_version)
        if waypoints is not None:
            if waypoint_candidates:
                waypoints = _merge_waypoint_tail_candidates(waypoints, waypoint_candidates)
            else:
                waypoints = _sanitize_waypoint_sequence(waypoints)
            record["waypoints"] = waypoints
            timing = _extract_timing_from_waypoints(waypoints)
            if timing is not None:
                record["timing"] = timing

        if kind == "squadron":
            squadron_callsign = _extract_squadron_callsign(record_blob, strings_by_id)
            if squadron_callsign is not None:
                record["squadron_callsign"] = squadron_callsign
            squadron_mission = _extract_squadron_mission(record_blob, strings_by_id)
            if squadron_mission is not None:
                record["squadron_mission"] = squadron_mission
            count_value = _extract_squadron_aircraft_count(record_blob)
            if isinstance(count_value, int):
                record["aircraft_count"] = count_value
        elif kind == "flight":
            flight_mission = _extract_flight_mission(
                record_blob,
                ctx.container_version,
                strings_by_id,
            )
            if flight_mission is not None:
                record["flight_mission"] = flight_mission
                count_value = flight_mission.get("aircraft_count")
                if isinstance(count_value, int) and 1 <= count_value <= 4:
                    record["aircraft_count"] = count_value
        elif kind == "package":
            package_mission = _extract_package_mission(
                record_blob,
                ctx.container_version,
                strings_by_id,
            )
            if package_mission is not None:
                record["package_mission"] = package_mission
            package_team_id = _extract_package_team_id(record_blob)
            if isinstance(package_team_id, int):
                record["package_team_id"] = package_team_id

        records.append(record)

    # Aggregate inventory stats for diagnostics/validation.
    kind_counter = Counter(rec["kind"] for rec in records)

    size_stats: dict[str, dict[str, int]] = {}
    for kind in sorted(kind_counter):
        sizes = [rec["size"] for rec in records if rec["kind"] == kind]
        size_stats[kind] = {
            "count": len(sizes),
            "min": min(sizes),
            "max": max(sizes),
        }

    packages, flights, package_flight_links = _build_package_and_flight_indexes(records, data)

    warnings: list[str] = []
    if expected_records is not None and expected_records != len(records):
        warnings.append(
            "record_count metadata mismatch: "
            f"header={expected_records}, detected={len(records)}"
        )

    return {
        "file_type": "uni",
        "size": len(data),
        "container_version": ctx.container_version,
        "ct_path": str(ct_path),
        "ucd_path": str(ucd_path) if ucd_path is not None else None,
        "vcd_path": str(vcd_path) if vcd_path is not None else None,
        "strings_path": str(strings_path) if strings_path is not None else None,
        "expected_record_count": expected_records,
        "detected_record_count": len(records),
        "kind_counts": dict(sorted(kind_counter.items())),
        "record_size_stats_by_kind": size_stats,
        "package_count": len(packages),
        "flight_count": len(flights),
        "package_flight_links": package_flight_links,
        "packages": packages,
        "flights": flights,
        "records": records,
        "warnings": warnings,
    }


def _record_index_by_id(uni_parsed: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    """Index parsed `.uni` records by `(num, creator)` for fast joins."""

    record_by_id: dict[tuple[int, int], dict[str, Any]] = {}
    records = uni_parsed.get("records")
    if not isinstance(records, list):
        return record_by_id
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = _to_vuid_tuple(record.get("id"))
        if record_id is None:
            continue
        record_by_id[record_id] = record
    return record_by_id


def _flight_index_by_id(uni_parsed: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    """Index parsed `.uni` flights by `(num, creator)` for fallback joins."""

    flight_by_id: dict[tuple[int, int], dict[str, Any]] = {}
    flights = uni_parsed.get("flights")
    if not isinstance(flights, list):
        return flight_by_id
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        flight_id = _to_vuid_tuple(flight.get("id"))
        if flight_id is None:
            continue
        flight_by_id[flight_id] = flight
    return flight_by_id


def _timing_from_package_tasking(package_tasking: Any) -> dict[str, Any] | None:
    """Build timing object from package tasking when direct timing is missing."""

    if not isinstance(package_tasking, dict):
        return None
    return {
        "takeoff_time_ms": package_tasking.get("takeoff_time_ms"),
        "takeoff_time_z": package_tasking.get("takeoff_time_z"),
        "push_time_ms": package_tasking.get("push_time_ms"),
        "push_time_z": package_tasking.get("push_time_z"),
        "time_on_target_ms": package_tasking.get("time_on_target_ms"),
        "time_on_target_z": package_tasking.get("time_on_target_z"),
    }


def _collect_package_numbers(uni_parsed: dict[str, Any]) -> list[int]:
    """Collect available package numbers from parsed `.uni` package list."""

    if isinstance(uni_parsed, ParsedUniData):
        return uni_parsed.package_numbers()

    packages = uni_parsed.get("packages")
    if not isinstance(packages, list):
        return []
    numbers: set[int] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        package_number = package.get("package_number")
        if isinstance(package_number, int):
            numbers.add(package_number)
    return sorted(numbers)


def list_package_generated_flights(
    uni_parsed: ParsedUniData | dict[str, Any],
    package_numbers: list[int],
) -> list[dict[str, Any]]:
    """Return package->flight mappings with high-confidence best-effort logic."""

    if isinstance(uni_parsed, ParsedUniData):
        uni_parsed = {
            "packages": list(uni_parsed.packages),
            "flights": list(uni_parsed.flights),
            "records": list(uni_parsed.records),
            "warnings": list(uni_parsed.warnings),
        }

    packages = uni_parsed.get("packages")
    if not isinstance(packages, list):
        raise ParseError("uni_parsed does not contain expected packages list")

    record_by_id = _record_index_by_id(uni_parsed)
    flight_by_id = _flight_index_by_id(uni_parsed)

    out: list[dict[str, Any]] = []
    for package_number in package_numbers:
        matches = [
            package
            for package in packages
            if isinstance(package, dict) and package.get("package_number") == package_number
        ]
        if not matches:
            matches = [
                package
                for package in packages
                if isinstance(package, dict)
                and isinstance(package.get("id"), dict)
                and package["id"].get("num") == package_number
            ]

        package_items: list[dict[str, Any]] = []
        for package in matches:
            package_item: dict[str, Any] = {
                "package_id": package.get("id"),
                "package_number": package.get("package_number"),
                "package_mission": package.get("package_mission"),
                "package_team_id": package.get("package_team_id"),
                "timing": package.get("timing")
                if isinstance(package.get("timing"), dict)
                else _timing_from_package_tasking(package.get("package_mission")),
                "offset": package.get("offset"),
                "size": package.get("size"),
                "x": package.get("x"),
                "y": package.get("y"),
                "generated_flights": [],
                "notes": [],
            }

            squadron_callsign_idxs: set[int] = set()
            unit_refs = package.get("unit_refs")
            if isinstance(unit_refs, list):
                for ref in unit_refs:
                    if not isinstance(ref, dict) or ref.get("kind") != "squadron":
                        continue
                    ref_id = _to_vuid_tuple(ref.get("id"))
                    if ref_id is None:
                        continue
                    rec = record_by_id.get(ref_id)
                    if rec is None:
                        continue
                    profile = rec.get("callsign_profile")
                    if not isinstance(profile, dict):
                        continue
                    callsign_idx = profile.get("callsign_idx")
                    if isinstance(callsign_idx, int):
                        squadron_callsign_idxs.add(callsign_idx)

            planned_slots = package.get("planned_flight_slots")
            if isinstance(planned_slots, list) and planned_slots:
                # Highest-confidence source: package-owned flight slot table.
                for slot in planned_slots:
                    if not isinstance(slot, dict):
                        continue
                    slot_id = _to_vuid_tuple(slot.get("id"))
                    if slot_id is None:
                        continue

                    target = record_by_id.get(slot_id)
                    unit_kind = slot.get("kind")
                    if target is not None and isinstance(target.get("kind"), str):
                        unit_kind = target.get("kind")

                    callsign_profile: dict[str, Any] | None = None
                    squadron_callsign: dict[str, Any] | None = None
                    flight_mission: dict[str, Any] | None = None
                    squadron_mission: dict[str, Any] | None = None
                    timing: dict[str, Any] | None = None
                    aircraft_count: int | None = None

                    if target is not None:
                        if isinstance(target.get("callsign_profile"), dict):
                            callsign_profile = target.get("callsign_profile")
                        if isinstance(target.get("squadron_callsign"), dict):
                            squadron_callsign = target.get("squadron_callsign")
                        if isinstance(target.get("flight_mission"), dict):
                            flight_mission = target.get("flight_mission")
                        if isinstance(target.get("squadron_mission"), dict):
                            squadron_mission = target.get("squadron_mission")
                        if isinstance(target.get("timing"), dict):
                            timing = target.get("timing")
                        count_value = target.get("aircraft_count")
                        if isinstance(count_value, int):
                            aircraft_count = count_value

                    if flight_mission is None and squadron_mission is not None:
                        flight_mission = squadron_mission

                    air_task_number = slot.get("air_task_number")
                    if target is not None and isinstance(target.get("air_task_number"), int):
                        air_task_number = target.get("air_task_number")

                    package_item["generated_flights"].append(
                        {
                            "slot_index": slot.get("slot_index"),
                            "unit_id": {"num": slot_id[0], "creator": slot_id[1]},
                            "unit_kind": unit_kind,
                            "air_task_number": air_task_number,
                            "x": target.get("x") if isinstance(target, dict) else None,
                            "y": target.get("y") if isinstance(target, dict) else None,
                            "unit_type": target.get("unit_type")
                            if isinstance(target, dict)
                            else None,
                            "ct_index": target.get("ct_index")
                            if isinstance(target, dict)
                            else None,
                            "callsign_profile": callsign_profile,
                            "flight_mission": flight_mission,
                            "squadron_mission": squadron_mission,
                            "timing": timing,
                            "aircraft_count": aircraft_count,
                            "squadron_callsign": squadron_callsign,
                            "confidence": "high",
                            "inference_source": "package_flight_slot_table",
                            "source_offset": slot.get("offset"),
                        }
                    )

            if not package_item["generated_flights"]:
                refs: list[dict[str, Any]] = []
                flight_refs = package.get("flight_refs")
                if isinstance(flight_refs, list):
                    refs.extend(flight_refs)

                if not refs:
                    primary = package.get("primary_flight_candidate")
                    if isinstance(primary, dict):
                        refs.append(
                            {
                                "id": {
                                    "num": primary.get("num"),
                                    "creator": primary.get("creator", 0),
                                },
                                "offset": primary.get("source_offset"),
                                "kind": "flight",
                            }
                        )

                seen: set[tuple[int, int]] = set()
                for ref in refs:
                    ref_id = _to_vuid_tuple(ref.get("id"))
                    if ref_id is None or ref_id in seen:
                        continue
                    seen.add(ref_id)

                    target = record_by_id.get(ref_id)
                    if target is None:
                        target = flight_by_id.get(ref_id)
                    if target is None:
                        continue

                    package_item["generated_flights"].append(
                        {
                            "unit_id": {"num": ref_id[0], "creator": ref_id[1]},
                            "unit_kind": target.get("kind", "flight"),
                            "air_task_number": target.get("air_task_number"),
                            "x": target.get("x"),
                            "y": target.get("y"),
                            "unit_type": target.get("unit_type"),
                            "ct_index": target.get("ct_index"),
                            "callsign_profile": target.get("callsign_profile"),
                            "flight_mission": target.get("flight_mission"),
                            "squadron_mission": target.get("squadron_mission"),
                            "timing": target.get("timing"),
                            "aircraft_count": target.get("aircraft_count"),
                            "squadron_callsign": target.get("squadron_callsign"),
                            "confidence": "high",
                            "inference_source": "explicit_reference",
                            "source_offset": ref.get("offset"),
                        }
                    )

            if not package_item["generated_flights"]:
                package_item["notes"].append(
                    "no explicit flight records linked to this package"
                )
            else:
                package_item["notes"].append("using explicit package link data")

            package_item["package_squadron_callsign_idxs"] = sorted(squadron_callsign_idxs)
            package_items.append(package_item)

        out.append(
            {
                "query_package_number": package_number,
                "matches": package_items,
            }
        )

    return out


def list_package_flight_callsigns(
    uni_parsed: dict[str, Any],
    package_numbers: list[int],
) -> list[dict[str, Any]]:
    """Compatibility shape focused on callsign-related flight listing."""

    generated = list_package_generated_flights(uni_parsed, package_numbers)
    out: list[dict[str, Any]] = []

    for query in generated:
        query_number = query.get("query_package_number")
        matches = query.get("matches")
        if not isinstance(matches, list):
            continue

        package_items: list[dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            flights_raw = match.get("generated_flights")
            flights: list[dict[str, Any]] = []
            if isinstance(flights_raw, list):
                for flight in flights_raw:
                    if not isinstance(flight, dict):
                        continue
                    unit_id = _to_vuid_tuple(flight.get("unit_id"))
                    if unit_id is None:
                        continue
                    flights.append(
                        {
                            "flight_id": {"num": unit_id[0], "creator": unit_id[1]},
                            "source_offset": flight.get("source_offset"),
                            "unit_type": flight.get("unit_type"),
                            "ct_index": flight.get("ct_index"),
                            "x": flight.get("x"),
                            "y": flight.get("y"),
                            "callsign_profile": flight.get("callsign_profile"),
                            "flight_mission": flight.get("flight_mission"),
                            "timing": flight.get("timing"),
                            "aircraft_count": flight.get("aircraft_count"),
                        }
                    )

            package_items.append(
                {
                    "package_id": match.get("package_id"),
                    "package_number": match.get("package_number"),
                    "package_mission": match.get("package_mission"),
                    "package_team_id": match.get("package_team_id"),
                    "timing": match.get("timing")
                    if isinstance(match.get("timing"), dict)
                    else _timing_from_package_tasking(match.get("package_mission")),
                    "offset": match.get("offset"),
                    "size": match.get("size"),
                    "x": match.get("x"),
                    "y": match.get("y"),
                    "flights": flights,
                    "notes": match.get("notes", []),
                }
            )

        out.append(
            {
                "query_package_number": query_number,
                "matches": package_items,
            }
        )

    return out


def parse_uni(
    data: bytes,
    *,
    container_version: int | None = None,
    support_base_dir: str | Path | None = None,
    decode_metadata: dict[str, int] | None = None,
) -> ParsedUniData:
    ctx = ParseContext(
        container_version=container_version,
        decode_metadata=decode_metadata,
        support_base_dir=Path(support_base_dir).resolve() if support_base_dir is not None else None,
    )
    return ParsedUniData.from_dict(_parse_uni(data, ctx))


__all__ = [
    "_parse_uni",
    "parse_uni",
    "_collect_package_numbers",
    "list_package_generated_flights",
    "list_package_flight_callsigns",
]
