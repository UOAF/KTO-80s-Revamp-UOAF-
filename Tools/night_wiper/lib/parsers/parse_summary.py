from __future__ import annotations

from pathlib import Path
from typing import Any

from lib.cam.cam_content import _find_support_file, _load_strings_by_id, _to_vuid_tuple
from lib.cam.types import ParsedCmpData, ParsedUniData, SummaryInput, SummaryOutput


def _load_strings(support_base_dir: Path | None) -> dict[int, str]:
    strings_path = _find_support_file("Strings.txt", support_base_dir=support_base_dir)
    if strings_path is None:
        strings_path = _find_support_file("strings.txt", support_base_dir=support_base_dir)
    if strings_path is None:
        return {}
    return _load_strings_by_id(strings_path)


def _parse_radiomap_frequency(value: str) -> int | None:
    text = value.strip()
    if not text or text.upper() == "NONE":
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _format_radiomap_frequency(value: int | None) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""
    return f"{value / 1000.0:.3f} MHz"


def _load_radiomap(support_base_dir: Path | None) -> dict[str, dict[str, Any]]:
    radiomap_path = _find_support_file("RadioMap.dat", support_base_dir=support_base_dir)
    if radiomap_path is None:
        radiomap_path = _find_support_file("radiomap.dat", support_base_dir=support_base_dir)
    if radiomap_path is None:
        return {}

    entries: dict[str, dict[str, Any]] = {}
    with radiomap_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            callsign = parts[0]
            if not callsign:
                continue
            tactical_uhf = _parse_radiomap_frequency(parts[1])
            intra_vhf = _parse_radiomap_frequency(parts[2])
            backup_uhf = _parse_radiomap_frequency(parts[3])
            entries[callsign.lower()] = {
                "callsign": callsign,
                "tactical_uhf_hz": tactical_uhf,
                "tactical_uhf": _format_radiomap_frequency(tactical_uhf),
                "intra_vhf_hz": intra_vhf,
                "intra_vhf": _format_radiomap_frequency(intra_vhf),
                "backup_uhf_hz": backup_uhf,
                "backup_uhf": _format_radiomap_frequency(backup_uhf),
            }
    return entries


def _team_name_from_id(team_id: Any, strings_by_id: dict[int, str]) -> str:
    if not isinstance(team_id, int):
        return ""
    value = strings_by_id.get(40 + team_id)
    if isinstance(value, str):
        return value.strip()
    return ""


def _callsign_root_from_idx(
    callsign_idx: int | None,
    strings_by_id: dict[int, str],
) -> str | None:
    if not isinstance(callsign_idx, int):
        return None
    root = strings_by_id.get(2000 + callsign_idx)
    if isinstance(root, str) and root.strip():
        return root.strip()
    root = strings_by_id.get(callsign_idx)
    if isinstance(root, str) and root.strip():
        return root.strip()
    return None


def _resolve_callsign(
    generated_flight: dict[str, Any],
    *,
    strings_by_id: dict[int, str],
    callsign_by_idx: dict[int, str],
    callsign_num_by_idx: dict[int, int],
) -> str | None:
    squadron_callsign = generated_flight.get("squadron_callsign")
    if isinstance(squadron_callsign, dict):
        value = squadron_callsign.get("callsign")
        if isinstance(value, str) and value.strip():
            return value.strip()

    callsign_profile = generated_flight.get("callsign_profile")
    callsign_idx: int | None = None
    if isinstance(callsign_profile, dict):
        value = callsign_profile.get("callsign_idx")
        if isinstance(value, int):
            callsign_idx = value

    root: str | None = None
    if isinstance(squadron_callsign, dict):
        callsign_root = squadron_callsign.get("callsign_root")
        if isinstance(callsign_root, str) and callsign_root.strip():
            root = callsign_root.strip()
        strings_id = squadron_callsign.get("strings_id")
        if not root and isinstance(strings_id, int):
            mapped = strings_by_id.get(strings_id)
            if isinstance(mapped, str) and mapped.strip():
                root = mapped.strip()
    if not root and callsign_idx is not None:
        root = callsign_by_idx.get(callsign_idx)
    if not root and callsign_idx is not None:
        root = _callsign_root_from_idx(callsign_idx, strings_by_id)

    if not root:
        return None

    callsign_num: int | None = None
    if isinstance(squadron_callsign, dict):
        value = squadron_callsign.get("callsign_num")
        if isinstance(value, int):
            callsign_num = value

    if (callsign_num is None or not (1 <= callsign_num <= 9)) and callsign_idx is not None:
        idx_num = callsign_num_by_idx.get(callsign_idx)
        if isinstance(idx_num, int):
            callsign_num = idx_num

    if callsign_num is None or not (1 <= callsign_num <= 9):
        air_task_number = generated_flight.get("air_task_number")
        if isinstance(air_task_number, int):
            candidate = air_task_number % 10
            if 1 <= candidate <= 9:
                callsign_num = candidate

    if callsign_num is None or not (1 <= callsign_num <= 9):
        slot_index = generated_flight.get("slot_index")
        if isinstance(slot_index, int):
            candidate = slot_index + 1
            if 1 <= candidate <= 9:
                callsign_num = candidate

    if isinstance(callsign_num, int) and 1 <= callsign_num <= 9:
        return f"{root}{callsign_num}"
    return root


def _merge_flight_timing(
    *,
    base_timing: Any,
    flight_mission: Any,
    package_timing: Any,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(base_timing, dict):
        for key, value in base_timing.items():
            if isinstance(key, str) and key.endswith("_z"):
                continue
            merged[key] = value
    if not isinstance(flight_mission, dict):
        flight_mission = {}
    if not isinstance(package_timing, dict):
        package_timing = {}

    for stem in ("takeoff_time", "push_time", "time_on_target"):
        ms_key = f"{stem}_ms"
        if merged.get(ms_key) is None:
            mission_val = flight_mission.get(ms_key)
            merged[ms_key] = mission_val if isinstance(mission_val, int) else package_timing.get(ms_key)
    return merged


def _strip_timing_z_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if not (isinstance(key, str) and key.endswith("_z"))}


def _radio_for_callsign(callsign: Any, radiomap_by_callsign: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(callsign, str):
        return {}
    key = callsign.strip().lower()
    if not key:
        return {}
    value = radiomap_by_callsign.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _collect_player_package_ids(
    uni_parsed: ParsedUniData,
    player_squadron_id: tuple[int, int] | None,
) -> tuple[set[tuple[int, int]], str]:
    if player_squadron_id is None:
        return set(), "all_packages_fallback"

    packages = uni_parsed.packages
    if not packages:
        return set(), "all_packages_fallback"

    record_by_id = uni_parsed.record_by_id()
    player_kind: str | None = None
    player_record = record_by_id.get(player_squadron_id)
    if isinstance(player_record, dict) and isinstance(player_record.get("kind"), str):
        player_kind = player_record.get("kind")

    if player_kind == "flight":
        flight_linked: set[tuple[int, int]] = set()
        for package in packages:
            if not isinstance(package, dict):
                continue
            package_id = _to_vuid_tuple(package.get("id"))
            if package_id is None:
                continue
            planned = package.get("planned_flight_slots")
            if isinstance(planned, list):
                for slot in planned:
                    if isinstance(slot, dict) and _to_vuid_tuple(slot.get("id")) == player_squadron_id:
                        flight_linked.add(package_id)
                        break
            refs = package.get("flight_refs")
            if isinstance(refs, list):
                for ref in refs:
                    if isinstance(ref, dict) and _to_vuid_tuple(ref.get("id")) == player_squadron_id:
                        flight_linked.add(package_id)
                        break
            urefs = package.get("unit_refs")
            if isinstance(urefs, list):
                for ref in urefs:
                    if isinstance(ref, dict) and _to_vuid_tuple(ref.get("id")) == player_squadron_id:
                        flight_linked.add(package_id)
                        break
        if flight_linked:
            return flight_linked, "player_flight_ref"

    explicit: set[tuple[int, int]] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        package_id = _to_vuid_tuple(package.get("id"))
        if package_id is None:
            continue
        refs = package.get("unit_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if ref.get("kind") != "squadron":
                continue
            if _to_vuid_tuple(ref.get("id")) == player_squadron_id:
                explicit.add(package_id)
                break
    if explicit:
        return explicit, "player_squadron_ref"

    player_callsign_idx: int | None = None
    if isinstance(player_record, dict):
        profile = player_record.get("callsign_profile")
        if isinstance(profile, dict) and isinstance(profile.get("callsign_idx"), int):
            player_callsign_idx = profile.get("callsign_idx")

    if player_callsign_idx is None:
        return set(), "all_packages_fallback"

    by_callsign: set[tuple[int, int]] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        package_id = _to_vuid_tuple(package.get("id"))
        if package_id is None:
            continue
        refs = package.get("unit_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("kind") != "squadron":
                continue
            ref_id = _to_vuid_tuple(ref.get("id"))
            if ref_id is None:
                continue
            ref_record = record_by_id.get(ref_id)
            if not isinstance(ref_record, dict):
                continue
            profile = ref_record.get("callsign_profile")
            if not isinstance(profile, dict):
                continue
            ref_callsign_idx = profile.get("callsign_idx")
            if isinstance(ref_callsign_idx, int) and ref_callsign_idx == player_callsign_idx:
                by_callsign.add(package_id)
                break

    if by_callsign:
        return by_callsign, "callsign_idx_fallback"
    return set(), "all_packages_fallback"


def _build_package_rows(
    *,
    summary_input: SummaryInput,
    package_numbers: list[int] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    selected_numbers = package_numbers or summary_input.uni.package_numbers()
    generated = summary_input.uni.generated_packages(selected_numbers)

    strings_by_id = _load_strings(summary_input.support_base_dir)
    radiomap_by_callsign = _load_radiomap(summary_input.support_base_dir)
    record_by_id = summary_input.uni.record_by_id()
    package_by_id = summary_input.uni.package_by_id()

    packages: list[dict[str, Any]] = []
    for query in generated:
        matches = query.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue

            package_id = _to_vuid_tuple(match.get("package_id"))
            package_record = record_by_id.get(package_id) if package_id is not None else None
            package_link = package_by_id.get(package_id) if package_id is not None else None
            callsign_by_idx: dict[int, str] = {}
            callsign_num_by_idx: dict[int, int] = {}
            if isinstance(package_link, dict):
                unit_refs = package_link.get("unit_refs")
                if isinstance(unit_refs, list):
                    for ref in unit_refs:
                        if not isinstance(ref, dict) or ref.get("kind") != "squadron":
                            continue
                        ref_id = _to_vuid_tuple(ref.get("id"))
                        if ref_id is None:
                            continue
                        squadron_record = record_by_id.get(ref_id)
                        if not isinstance(squadron_record, dict):
                            continue
                        profile = squadron_record.get("callsign_profile")
                        if not isinstance(profile, dict):
                            continue
                        callsign_idx = profile.get("callsign_idx")
                        if not isinstance(callsign_idx, int):
                            continue
                        squad_callsign = squadron_record.get("squadron_callsign")
                        if isinstance(squad_callsign, dict):
                            root: str | None = None
                            callsign_root = squad_callsign.get("callsign_root")
                            if isinstance(callsign_root, str) and callsign_root.strip():
                                root = callsign_root.strip()
                            callsign = squad_callsign.get("callsign")
                            if not root and isinstance(callsign, str) and callsign.strip():
                                root = callsign.strip().rstrip("123456789")
                            strings_id = squad_callsign.get("strings_id")
                            if not root and isinstance(strings_id, int):
                                mapped = strings_by_id.get(strings_id)
                                if isinstance(mapped, str) and mapped.strip():
                                    root = mapped.strip()
                            if root:
                                callsign_by_idx[callsign_idx] = root
                            callsign_num = squad_callsign.get("callsign_num")
                            if isinstance(callsign_num, int):
                                callsign_num_by_idx[callsign_idx] = callsign_num

            flights: list[dict[str, Any]] = []
            generated_flights = match.get("generated_flights")
            if isinstance(generated_flights, list):
                for generated_flight in generated_flights:
                    if not isinstance(generated_flight, dict):
                        continue
                    unit_id = _to_vuid_tuple(generated_flight.get("unit_id"))
                    if unit_id is None:
                        continue
                    unit_record = record_by_id.get(unit_id)

                    callsign = _resolve_callsign(
                        generated_flight,
                        strings_by_id=strings_by_id,
                        callsign_by_idx=callsign_by_idx,
                        callsign_num_by_idx=callsign_num_by_idx,
                    )

                    callsign_profile = generated_flight.get("callsign_profile")
                    aircraft = ""
                    if isinstance(callsign_profile, dict):
                        value = callsign_profile.get("vehicle_name")
                        if isinstance(value, str):
                            aircraft = value

                    tasking = generated_flight.get("flight_mission")
                    if not isinstance(tasking, dict):
                        tasking = generated_flight.get("squadron_mission")
                    timing = _merge_flight_timing(
                        base_timing=generated_flight.get("timing"),
                        flight_mission=generated_flight.get("flight_mission"),
                        package_timing=match.get("timing"),
                    )

                    steerpoints: list[dict[str, Any]] = []
                    if isinstance(unit_record, dict):
                        waypoints = unit_record.get("waypoints")
                        if isinstance(waypoints, list):
                            steerpoints = waypoints

                    flight_number = generated_flight.get("air_task_number")
                    flight_radio = _radio_for_callsign(callsign, radiomap_by_callsign)
                    flight_row = {
                        "unit_id": {"num": unit_id[0], "creator": unit_id[1]},
                        "unit_kind": generated_flight.get("unit_kind"),
                        "flight_number": flight_number,
                        "callsign": callsign or "",
                        "aircraft": aircraft,
                        "aircraft_count": generated_flight.get("aircraft_count"),
                        "tasking": tasking if isinstance(tasking, dict) else {},
                        "timing": timing,
                        "steerpoints": steerpoints,
                        "radio": flight_radio,
                    }
                    flight_row["l16"] = summary_input.l16.by_flight.get(flight_number) if isinstance(flight_number, int) else {}
                    flights.append(flight_row)

            package_steerpoints: list[dict[str, Any]] = []
            if isinstance(package_record, dict):
                waypoints = package_record.get("waypoints")
                if isinstance(waypoints, list):
                    package_steerpoints = waypoints

            package_radio: dict[str, Any] = {}
            for flight in flights:
                flight_radio = flight.get("radio")
                if not isinstance(flight_radio, dict):
                    continue
                tactical_uhf = flight_radio.get("tactical_uhf")
                tactical_uhf_hz = flight_radio.get("tactical_uhf_hz")
                if not tactical_uhf and not isinstance(tactical_uhf_hz, int):
                    continue
                package_radio = {
                    "primary_callsign": flight.get("callsign", ""),
                    "tactical_uhf_hz": tactical_uhf_hz,
                    "tactical_uhf": tactical_uhf or "",
                }
                break

            packages.append(
                {
                    "package_id": match.get("package_id"),
                    "package_number": match.get("package_number"),
                    "team_id": match.get("package_team_id"),
                    "team_name": _team_name_from_id(match.get("package_team_id"), strings_by_id),
                    "tasking": match.get("package_mission") if isinstance(match.get("package_mission"), dict) else {},
                    "timing": _strip_timing_z_fields(match.get("timing")),
                    "radio": package_radio,
                    "steerpoints": package_steerpoints,
                    "flights": flights,
                    "notes": match.get("notes") if isinstance(match.get("notes"), list) else [],
                }
            )

    if packages:
        return packages, False

    fallback_packages: list[dict[str, Any]] = []
    for package in summary_input.uni.packages:
        fallback_packages.append(
            {
                "package_id": package.get("id"),
                "package_number": package.get("package_number"),
                "team_id": package.get("package_team_id"),
                "team_name": _team_name_from_id(package.get("package_team_id"), strings_by_id),
                "tasking": package.get("package_mission") if isinstance(package.get("package_mission"), dict) else {},
                "timing": _strip_timing_z_fields(package.get("timing")),
                "radio": {},
                "steerpoints": package.get("waypoints") if isinstance(package.get("waypoints"), list) else [],
                "flights": [],
                "notes": ["fallback_from_uni_packages"],
            }
        )
    return fallback_packages, bool(fallback_packages)


def _bullseye_grid_size_for_theater(theater: str | None) -> tuple[int, int]:
    _ = theater
    return (1024, 1024)


def _map_coord_from_cmp(value: int, axis: str, grid_size: int) -> float:
    if abs(value) <= grid_size * 2:
        map_scalar = (float(value) / float(grid_size)) * 4096.0
    else:
        map_scalar = (float(value) / 3359580.0) * 4096.0
    if axis == "lat":
        return map_scalar - 4096.0
    return map_scalar


def build_summary_output(
    summary_input: SummaryInput,
    *,
    package_numbers: list[int] | None = None,
) -> SummaryOutput:
    packages, used_uni_fallback = _build_package_rows(
        summary_input=summary_input,
        package_numbers=package_numbers,
    )
    player_squadron = summary_input.cmp.player_squadron_id
    _, match_method = _collect_player_package_ids(summary_input.uni, player_squadron)

    current_time_ms = summary_input.cmp.current_time_ms
    current_date = summary_input.twx.current_date or ""

    bullseye_name_id = summary_input.cmp.bullseye_name_id
    bullseye_x = summary_input.cmp.bullseye_x
    bullseye_y = summary_input.cmp.bullseye_y
    grid_x, grid_y = _bullseye_grid_size_for_theater(
        summary_input.theater_name or summary_input.cmp.theater_name
    )

    map_lat: float | None = None
    map_lng: float | None = None
    if isinstance(bullseye_x, int) and isinstance(bullseye_y, int):
        map_lng = _map_coord_from_cmp(bullseye_x, "lng", grid_x)
        map_lat = _map_coord_from_cmp(bullseye_y, "lat", grid_y)

    warnings: list[str] = list(summary_input.cmp.warnings)
    warnings.extend(summary_input.uni.warnings)
    warnings.extend(summary_input.twx.warnings)
    warnings.extend(summary_input.l16.warnings)
    if used_uni_fallback:
        warnings.append("Using UNI package fallback data shape.")

    player_dict: dict[str, Any] = {
        "squadron_id": {
            "num": player_squadron[0],
            "creator": player_squadron[1],
        }
        if player_squadron is not None
        else None,
        "package_match_method": match_method,
    }

    return SummaryOutput(
        source_path=str(summary_input.source_path),
        support_base_dir=str(summary_input.support_base_dir) if summary_input.support_base_dir is not None else "",
        l16_source_path=str(summary_input.l16.source_path) if summary_input.l16.source_path is not None else "",
        current_date=current_date,
        current_time_ms=current_time_ms,
        player=player_dict,
        bullseye={
            "x": bullseye_x,
            "y": bullseye_y,
            "name_id": bullseye_name_id,
            "name": "",
            "map_lat": map_lat,
            "map_lng": map_lng,
            "map_grid_size_x": grid_x,
            "map_grid_size_y": grid_y,
        },
        package_count=len(packages),
        packages=packages,
        warnings=warnings,
        container_version=summary_input.container_version,
    )


def build_summary_error_output(
    summary_input: SummaryInput,
    *,
    warnings: list[str] | tuple[str, ...] | None = None,
    package_match_method: str = "summary_failed",
) -> SummaryOutput:
    current_date = summary_input.twx.current_date or ""
    bullseye_x = summary_input.cmp.bullseye_x
    bullseye_y = summary_input.cmp.bullseye_y
    grid_x, grid_y = _bullseye_grid_size_for_theater(
        summary_input.theater_name or summary_input.cmp.theater_name
    )

    map_lat: float | None = None
    map_lng: float | None = None
    if isinstance(bullseye_x, int) and isinstance(bullseye_y, int):
        map_lng = _map_coord_from_cmp(bullseye_x, "lng", grid_x)
        map_lat = _map_coord_from_cmp(bullseye_y, "lat", grid_y)

    player_squadron = summary_input.cmp.player_squadron_id
    combined_warnings = list(summary_input.cmp.warnings)
    combined_warnings.extend(summary_input.uni.warnings)
    combined_warnings.extend(summary_input.twx.warnings)
    combined_warnings.extend(summary_input.l16.warnings)
    if warnings:
        combined_warnings.extend(str(item) for item in warnings if str(item).strip())

    return SummaryOutput(
        source_path=str(summary_input.source_path),
        support_base_dir=str(summary_input.support_base_dir) if summary_input.support_base_dir is not None else "",
        l16_source_path=str(summary_input.l16.source_path) if summary_input.l16.source_path is not None else "",
        current_date=current_date,
        current_time_ms=summary_input.cmp.current_time_ms,
        player={
            "squadron_id": {
                "num": player_squadron[0],
                "creator": player_squadron[1],
            }
            if player_squadron is not None
            else None,
            "package_match_method": package_match_method,
        },
        bullseye={
            "x": bullseye_x,
            "y": bullseye_y,
            "name_id": summary_input.cmp.bullseye_name_id,
            "name": "",
            "map_lat": map_lat,
            "map_lng": map_lng,
            "map_grid_size_x": grid_x,
            "map_grid_size_y": grid_y,
        },
        package_count=0,
        packages=[],
        warnings=combined_warnings,
        container_version=summary_input.container_version,
    )


def parse_summary(
    summary_input: SummaryInput,
    *,
    package_numbers: list[int] | None = None,
) -> SummaryOutput:
    return build_summary_output(summary_input, package_numbers=package_numbers)

__all__ = [
    "parse_summary",
    "build_summary_output",
    "build_summary_error_output",
]
