#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import struct
from typing import Any

from lib.campaign_paths import infer_bms_base_dir_from_campaign_dir, infer_support_base_dir
from lib.cam import parse_cam_file
from lib.cam_integration import extract_cam_brief_data
from lib.cam_writeback import (
    CamWritebackError,
    EntryFieldWriteValue,
    EntryWritePatch,
    FlightWritePatch,
    RecordFieldWriteValue,
    RecordWritePatch,
    WaypointWriteValue,
    build_verified_patched_cam_blob,
)
from lib.parsers.parse_cmp import parse_cmp
from lib.parsers.parse_uni import parse_uni


def _infer_campaign_dir(save_path: Path, explicit_campaign_dir: str | Path | None) -> Path | None:
    if explicit_campaign_dir:
        return Path(explicit_campaign_dir).expanduser().resolve()
    if save_path.parent.name.lower() == "campaign" and save_path.parent.parent.name.lower() == "data":
        return save_path.parent.resolve()
    if (
        save_path.parent.name.lower() == "campaign"
        and save_path.parent.parent.parent.name.lower() == "data"
    ):
        return save_path.parent.resolve()
    return None


def _flight_id_tuple(flight: Any) -> tuple[int, int] | None:
    if not isinstance(flight, dict):
        return None
    unit_id = flight.get("unit_id")
    if not isinstance(unit_id, dict):
        return None
    num = unit_id.get("num")
    creator = unit_id.get("creator")
    if isinstance(num, int) and isinstance(creator, int):
        return (num, creator)
    return None


def _build_all_waypoint_zero_patch(flight: dict[str, Any]) -> FlightWritePatch | None:
    flight_id = _flight_id_tuple(flight)
    if flight_id is None:
        return None

    steerpoints = flight.get("steerpoints")
    if not isinstance(steerpoints, list):
        return None

    waypoint_overrides: list[WaypointWriteValue] = []
    for waypoint in steerpoints:
        if not isinstance(waypoint, dict):
            continue
        waypoint_index = waypoint.get("index")
        if not isinstance(waypoint_index, int) or waypoint_index < 0:
            continue
        has_arrive = isinstance(waypoint.get("arrive_ms"), int)
        has_depart = isinstance(waypoint.get("depart_ms"), int)
        if not has_arrive and not has_depart:
            continue
        waypoint_overrides.append(
            WaypointWriteValue(
                waypoint_index=waypoint_index,
                arrive_ms=0 if has_arrive else None,
                depart_ms=0 if has_depart else None,
            )
        )
    if not waypoint_overrides:
        return None

    callsign = flight.get("callsign")
    flight_label = callsign.strip() if isinstance(callsign, str) and callsign.strip() else f"Flight {flight_id[0]}"
    return FlightWritePatch(
        flight_id=flight_id,
        flight_label=flight_label,
        waypoints=tuple(waypoint_overrides),
    )


def _collect_flight_patches(summary: dict[str, Any]) -> list[FlightWritePatch]:
    packages = summary.get("packages")
    if not isinstance(packages, list):
        return []

    patches: list[FlightWritePatch] = []
    seen_flight_ids: set[tuple[int, int]] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        flights = package.get("flights")
        if not isinstance(flights, list):
            continue
        for flight in flights:
            if not isinstance(flight, dict):
                continue
            patch = _build_all_waypoint_zero_patch(flight)
            if patch is None or patch.flight_id in seen_flight_ids:
                continue
            seen_flight_ids.add(patch.flight_id)
            patches.append(patch)
    return patches


def _build_output_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}_wiped.cam")


def _human_control_record_field(record: dict[str, Any]) -> RecordFieldWriteValue | None:
    current_flags = record.get("unit_flags")
    if not isinstance(current_flags, int):
        return None
    new_flags = current_flags | 0x80
    if new_flags == current_flags:
        return None
    return RecordFieldWriteValue(
        offset=39,
        value=struct.pack("<I", new_flags),
        field_label="unit_flags",
        expected_before=struct.pack("<I", current_flags),
    )


def _hq_control_record_field(record: dict[str, Any]) -> RecordFieldWriteValue | None:
    current_flags = record.get("unit_flags")
    if not isinstance(current_flags, int):
        return None
    new_flags = current_flags & ~0x80
    if new_flags == current_flags:
        return None
    return RecordFieldWriteValue(
        offset=39,
        value=struct.pack("<I", new_flags),
        field_label="unit_flags",
        expected_before=struct.pack("<I", current_flags),
    )


def _parse_except_camp_ids(raw_value: str | None) -> set[int]:
    if raw_value is None or not raw_value.strip():
        return set()
    out: set[int] = set()
    for item in raw_value.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            value = int(token, 10)
        except ValueError as exc:
            raise CamWritebackError(f"Invalid camp id in --except: {token!r}.") from exc
        if value < 0 or value > 0xFFFF:
            raise CamWritebackError(f"Camp id out of range in --except: {value}.")
        out.add(value)
    return out


def _build_control_state_patches(
    save_path: Path,
    *,
    bms_base_dir: Path | None,
    campaign_dir: Path | None,
    human_controlled: bool,
    except_camp_ids: set[int],
) -> tuple[list[RecordWritePatch], list[EntryWritePatch]]:
    support_base_dir = infer_support_base_dir(bms_base_dir, campaign_dir)
    parsed_cam = parse_cam_file(
        save_path,
        bms_base_dir=support_base_dir,
        parse_entries=False,
        best_effort=True,
    )
    cmp_entry = parsed_cam.get_entry_by_ext(".cmp")
    uni_entry = parsed_cam.get_entry_by_ext(".uni")
    if cmp_entry is None or uni_entry is None:
        raise CamWritebackError("The save must contain both .cmp and .uni entries.")

    parsed_cmp = parse_cmp(
        cmp_entry.data,
        container_version=parsed_cam.container_version,
        support_base_dir=support_base_dir,
        decode_metadata=cmp_entry.decode_metadata,
    )
    parsed_uni = parse_uni(
        uni_entry.data,
        container_version=parsed_cam.container_version,
        support_base_dir=support_base_dir,
        decode_metadata=uni_entry.decode_metadata,
    )
    record_by_id = parsed_uni.record_by_id()

    record_patches: list[RecordWritePatch] = []
    cmp_fields: list[EntryFieldWriteValue] = []
    seen_flights: set[tuple[int, int]] = set()
    for row in parsed_cmp.air_unit_rows:
        flight_id_raw = row.get("flight_id")
        if not isinstance(flight_id_raw, dict):
            continue
        num = flight_id_raw.get("num")
        creator = flight_id_raw.get("creator")
        if not isinstance(num, int) or not isinstance(creator, int):
            continue
        flight_id = (num, creator)
        if flight_id in seen_flights:
            continue
        seen_flights.add(flight_id)

        record = record_by_id.get(flight_id)
        if not isinstance(record, dict):
            continue
        label = row.get("display_name")
        if not isinstance(label, str) or not label.strip():
            label = row.get("base_name")
        if not isinstance(label, str) or not label.strip():
            label = f"Flight {num}"
        record_label = label.strip()
        record_campaign_id = record.get("campaign_id")
        row_campaign_id = row.get("camp_id")
        if isinstance(row_campaign_id, int) and row_campaign_id in except_camp_ids:
            continue
        if isinstance(record_campaign_id, int) and isinstance(row_campaign_id, int):
            if record_campaign_id != row_campaign_id:
                raise CamWritebackError(
                    f"{record_label}: .uni campaign id {record_campaign_id} does not match .cmp campaign id {row_campaign_id}."
                )

        record_field: RecordFieldWriteValue | None = (
            _human_control_record_field(record)
            if human_controlled
            else _hq_control_record_field(record)
        )

        if record_field is not None:
            record_patches.append(
                RecordWritePatch(
                    record_id=flight_id,
                    record_label=record_label,
                    expected_kind="flight",
                    fields=(record_field,),
                )
            )

        row_offset = row.get("offset")
        row_flags = row.get("row_flags")
        camp_id = row.get("camp_id")
        control_slot_raw = row.get("control_slot_raw")
        if (
            isinstance(row_offset, int)
            and isinstance(row_flags, int)
            and isinstance(camp_id, int)
            and isinstance(control_slot_raw, int)
        ):
            new_row_flags = row_flags | 0x80 if human_controlled else (row_flags & ~0x80)
            new_raw_camp_id = (camp_id & 0xFFFF) if human_controlled else ((camp_id & 0xFFFF) | 0xFFFF0000)
            if new_row_flags != row_flags:
                cmp_fields.append(
                    EntryFieldWriteValue(
                        offset=row_offset + 100,
                        value=struct.pack("<I", new_row_flags),
                        field_label=f"{record_label} row_flags",
                        expected_before=struct.pack("<I", row_flags),
                    )
                )
            raw_camp_id = camp_id | (control_slot_raw << 16)
            if new_raw_camp_id != raw_camp_id:
                cmp_fields.append(
                    EntryFieldWriteValue(
                        offset=row_offset + 104,
                        value=struct.pack("<I", new_raw_camp_id),
                        field_label=f"{record_label} control_slot",
                        expected_before=struct.pack("<I", raw_camp_id),
                    )
                )

    entry_patches: list[EntryWritePatch] = []
    if cmp_fields:
        entry_patches.append(
            EntryWritePatch(
                entry_label=(
                    "campaign air-unit HQ table (set human)"
                    if human_controlled
                    else "campaign air-unit HQ table (set HQ)"
                ),
                entry_ext=".cmp",
                fields=tuple(cmp_fields),
            )
        )
    return record_patches, entry_patches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a copy of a .cam save with all waypoint timings set to 0."
    )
    parser.add_argument("save_file", help="Path to the source .cam save file")
    parser.add_argument(
        "--campaign-dir",
        help="Optional Falcon BMS campaign dir. If omitted, the script tries to infer it from the save path.",
    )
    parser.add_argument(
        "--skip-takeoff-wipe",
        action="store_true",
        help="Do not zero waypoint timings.",
    )
    control_group = parser.add_mutually_exclusive_group()
    control_group.add_argument(
        "--set-all-squadrons-human",
        dest="set_all_squadrons_human",
        action="store_true",
        help="Also set all campaign air-unit HQ rows and matching flight records to human-controlled.",
    )
    control_group.add_argument(
        "--set-all-squadrons-hq",
        dest="set_all_squadrons_hq",
        action="store_true",
        help="Also set all campaign air-unit HQ rows to HQ-controlled.",
    )
    parser.add_argument(
        "--except",
        dest="except_camp_ids",
        help="Comma-separated campaign IDs to skip for squadron-control operations.",
    )
    args = parser.parse_args()

    source_path = Path(args.save_file).expanduser().resolve()
    if not source_path.is_file():
        parser.error(f"Save file not found: {source_path}")

    campaign_dir = _infer_campaign_dir(source_path, args.campaign_dir)
    if args.campaign_dir and (campaign_dir is None or not campaign_dir.is_dir()):
        parser.error(f"Campaign dir not found: {Path(args.campaign_dir).expanduser().resolve()}")
    except_camp_ids = _parse_except_camp_ids(args.except_camp_ids)

    bms_base_dir = infer_bms_base_dir_from_campaign_dir(campaign_dir)
    summary = extract_cam_brief_data(
        source_path,
        bms_base_dir=bms_base_dir,
        theater_target_folder=campaign_dir,
        save_stem=source_path.stem,
    )
    patches = [] if args.skip_takeoff_wipe else _collect_flight_patches(summary)
    record_patches: list[RecordWritePatch] = []
    entry_patches: list[EntryWritePatch] = []
    if args.set_all_squadrons_human:
        record_patches, entry_patches = _build_control_state_patches(
            source_path,
            bms_base_dir=bms_base_dir,
            campaign_dir=campaign_dir,
            human_controlled=True,
            except_camp_ids=except_camp_ids,
        )
    elif args.set_all_squadrons_hq:
        record_patches, entry_patches = _build_control_state_patches(
            source_path,
            bms_base_dir=bms_base_dir,
            campaign_dir=campaign_dir,
            human_controlled=False,
            except_camp_ids=except_camp_ids,
        )
    if not patches and not record_patches and not entry_patches:
        raise SystemExit(
            "No writable changes were found. "
            "If the save failed to parse fully, try again with --campaign-dir."
        )

    cam_blob, result = build_verified_patched_cam_blob(
        source_path,
        bms_base_dir=bms_base_dir,
        theater_target_folder=campaign_dir,
        flight_patches=patches,
        record_patches=record_patches,
        entry_patches=entry_patches,
    )

    output_path = _build_output_path(source_path)
    output_path.write_bytes(cam_blob)

    print(f"Source:  {source_path}")
    print(f"Output:  {output_path}")
    if campaign_dir is not None:
        print(f"Campaign: {campaign_dir}")
    if bms_base_dir is not None:
        print(f"BMS dir: {bms_base_dir}")
    print(f"Flights changed:   {result.changed_flights}")
    print(f"Waypoints changed: {result.changed_waypoints}")
    if args.set_all_squadrons_human or args.set_all_squadrons_hq:
        if except_camp_ids:
            print(f"Except campids:   {','.join(str(value) for value in sorted(except_camp_ids))}")
        print(f"Records changed:   {result.changed_records}")
        print(f"Record fields:     {result.changed_record_fields}")
        print(f"Entries changed:   {result.changed_entries}")
        print(f"Entry fields:      {result.changed_entry_fields}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CamWritebackError as exc:
        raise SystemExit(f"Writeback failed: {exc}")
