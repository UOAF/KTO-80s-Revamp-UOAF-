from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
from typing import Any

from lib.campaign_paths import infer_support_base_dir
from lib.cam.cam_content import (
    BinaryReader,
    CamFormatError,
    DecodedEntry,
    _decode_compressed_entry,
    _detect_container_version,
    parse_container,
)
from lib.cam.lzss import LzssError, lzss_compress
from lib.cam.types import ParsedUniData
from lib.parsers.parse_uni import _waypoint_array_quality, parse_uni


class CamWritebackError(RuntimeError):
    """Raised when a save writeback cannot be completed safely."""


@dataclass(frozen=True)
class WaypointWriteValue:
    waypoint_index: int
    arrive_ms: int | None = None
    depart_ms: int | None = None
    altitude_ft: int | None = None


@dataclass(frozen=True)
class FlightWritePatch:
    flight_id: tuple[int, int]
    flight_label: str
    waypoints: tuple[WaypointWriteValue, ...]


@dataclass(frozen=True)
class RecordFieldWriteValue:
    offset: int
    value: bytes
    field_label: str = ""
    expected_before: bytes | None = None


@dataclass(frozen=True)
class RecordWritePatch:
    record_id: tuple[int, int]
    record_label: str
    fields: tuple[RecordFieldWriteValue, ...]
    expected_kind: str | None = None


@dataclass(frozen=True)
class EntryFieldWriteValue:
    offset: int
    value: bytes
    field_label: str = ""
    expected_before: bytes | None = None


@dataclass(frozen=True)
class EntryWritePatch:
    entry_label: str
    fields: tuple[EntryFieldWriteValue, ...]
    entry_name: str | None = None
    entry_ext: str | None = None


@dataclass(frozen=True)
class SaveWriteResult:
    changed_flights: int
    changed_waypoints: int
    written_path: Path
    source_sha256: str
    changed_records: int = 0
    changed_record_fields: int = 0
    changed_entries: int = 0
    changed_entry_fields: int = 0


def build_verified_patched_cam_blob(
    save_path: str | Path,
    *,
    bms_base_dir: str | Path | None,
    theater_target_folder: str | Path | None = None,
    flight_patches: list[FlightWritePatch] | None = None,
    record_patches: list[RecordWritePatch] | None = None,
    entry_patches: list[EntryWritePatch] | None = None,
) -> tuple[bytes, SaveWriteResult]:
    flight_patches = list(flight_patches or [])
    record_patches = list(record_patches or [])
    entry_patches = list(entry_patches or [])
    if not flight_patches and not record_patches and not entry_patches:
        raise CamWritebackError("There are no save overrides to write.")

    source_path = Path(save_path).resolve()
    source_blob = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_blob).hexdigest()
    container_entries = parse_container(source_blob)
    decoded_entries = _decode_entries(source_blob, container_entries)
    container_version = _detect_container_version(decoded_entries)

    uni_item = next((item for item in decoded_entries if Path(item.entry.name).suffix.lower() == ".uni"), None)
    if uni_item is None:
        raise CamWritebackError("The save does not contain a .uni entry.")

    support_base_dir = infer_support_base_dir(bms_base_dir, theater_target_folder)
    parsed_uni = parse_uni(
        uni_item.decoded.data,
        container_version=container_version,
        support_base_dir=support_base_dir,
        decode_metadata=uni_item.decoded.metadata,
    )
    modified_uni = uni_item.decoded.data
    if flight_patches:
        modified_uni = _apply_flight_patches_to_uni(
            modified_uni,
            parsed_uni,
            container_version,
            flight_patches,
        )
    if record_patches:
        modified_uni = _apply_record_patches_to_uni(
            modified_uni,
            parsed_uni,
            record_patches,
        )

    reparsed_uni = parse_uni(
        modified_uni,
        container_version=container_version,
        support_base_dir=support_base_dir,
        decode_metadata={"record_count": parsed_uni.detected_record_count or 0},
    )
    if flight_patches:
        _verify_flight_patches(
            modified_uni,
            uni_item.decoded.data,
            parsed_uni,
            reparsed_uni,
            container_version,
            flight_patches,
        )
    if record_patches:
        _verify_record_patches(
            modified_uni,
            uni_item.decoded.data,
            parsed_uni,
            reparsed_uni,
            record_patches,
        )

    modified_decoded_by_name: dict[str, bytes] = {
        item.entry.name: item.decoded.data
        for item in decoded_entries
    }
    modified_decoded_by_name[uni_item.entry.name] = modified_uni
    modified_entry_names: set[str] = {uni_item.entry.name}
    if entry_patches:
        _apply_entry_patches_to_entries(
            modified_decoded_by_name,
            decoded_entries,
            entry_patches,
        )
        modified_entry_names.update(
            _find_entry_patch_target_name(decoded_entries, patch)
            for patch in entry_patches
        )

    raw_entries: list[tuple[str, bytes]] = []
    for item in decoded_entries:
        if item.entry.name not in modified_entry_names:
            raw_entries.append((item.entry.name, item.raw))
            continue
        entry_data = modified_decoded_by_name.get(item.entry.name)
        if entry_data is None:
            raise CamWritebackError(f"Missing rebuilt decoded entry for {item.entry.name}.")
        raw_entries.append(
            (
                item.entry.name,
                _encode_decoded_entry(
                    item.entry.name,
                    entry_data,
                    metadata=item.decoded.metadata,
                ),
            )
        )

    rebuilt_blob = _build_container_blob(raw_entries)
    _verify_container_blob(
        rebuilt_blob,
        uni_item.entry.name,
        decoded_entries,
        entry_patches,
        flight_patches,
        record_patches,
        container_version,
        support_base_dir,
    )

    changed_waypoints = sum(len(patch.waypoints) for patch in flight_patches)
    return (
        rebuilt_blob,
        SaveWriteResult(
            changed_flights=len(flight_patches),
            changed_waypoints=changed_waypoints,
            written_path=source_path,
            source_sha256=source_sha256,
            changed_records=len(record_patches),
            changed_record_fields=sum(len(patch.fields) for patch in record_patches),
            changed_entries=len(entry_patches),
            changed_entry_fields=sum(len(patch.fields) for patch in entry_patches),
        ),
    )


def apply_patched_cam_blob_atomic(
    save_path: str | Path,
    *,
    cam_blob: bytes,
    expected_source_sha256: str | None = None,
) -> Path:
    target_path = Path(save_path).resolve()
    if expected_source_sha256 is not None:
        current_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
        if current_sha256 != expected_source_sha256:
            raise CamWritebackError("The save changed after preflight verification; aborting write.")
    temp_path = _temp_write_path_for(target_path)
    try:
        with temp_path.open("wb") as handle:
            handle.write(cam_blob)
            handle.flush()
            os.fsync(handle.fileno())
        if temp_path.read_bytes() != cam_blob:
            raise CamWritebackError("Temporary write verification failed.")
        os.replace(temp_path, target_path)
        return target_path
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        raise


def _decode_entries(source_blob: bytes, entries: list[Any]) -> list[DecodedEntry]:
    decoded: list[DecodedEntry] = []
    for entry in entries:
        raw = source_blob[entry.offset : entry.offset + entry.length]
        try:
            decode_result = _decode_compressed_entry(entry.name, raw)
        except (CamFormatError, LzssError) as exc:
            raise CamWritebackError(f"Failed to decode {entry.name}: {exc}") from exc
        decoded.append(DecodedEntry(entry=entry, raw=raw, decoded=decode_result))
    return decoded


def _apply_flight_patches_to_uni(
    uni_data: bytes,
    parsed_uni: ParsedUniData,
    container_version: int | None,
    flight_patches: list[FlightWritePatch],
) -> bytes:
    record_by_id = parsed_uni.record_by_id()
    mutable = bytearray(uni_data)
    for patch in flight_patches:
        record = record_by_id.get(patch.flight_id)
        if not isinstance(record, dict):
            raise CamWritebackError(f"{patch.flight_label}: flight record not found in .uni.")
        record_offset = record.get("offset")
        record_size = record.get("size")
        if not isinstance(record_offset, int) or not isinstance(record_size, int):
            raise CamWritebackError(f"{patch.flight_label}: invalid record bounds in .uni.")
        record_blob = bytes(uni_data[record_offset : record_offset + record_size])
        parsed_waypoints = record.get("waypoints")
        if not isinstance(parsed_waypoints, list):
            raise CamWritebackError(f"{patch.flight_label}: parsed waypoints are missing.")
        located_waypoints = _locate_displayed_waypoints(
            record_blob,
            container_version=container_version,
            kind=record.get("kind"),
            parsed_waypoints=parsed_waypoints,
            flight_label=patch.flight_label,
        )

        located_by_index = {
            waypoint.get("index"): waypoint
            for waypoint in located_waypoints
            if isinstance(waypoint.get("index"), int)
        }
        for waypoint_patch in patch.waypoints:
            located = located_by_index.get(waypoint_patch.waypoint_index)
            if not isinstance(located, dict):
                raise CamWritebackError(
                    f"{patch.flight_label}: waypoint {waypoint_patch.waypoint_index + 1} could not be located."
                )
            if waypoint_patch.altitude_ft is not None:
                _write_altitude_ft(
                    mutable,
                    base_offset=record_offset,
                    located_waypoint=located,
                    altitude_ft=waypoint_patch.altitude_ft,
                    flight_label=patch.flight_label,
                )
            if waypoint_patch.arrive_ms is not None:
                _write_u32_field(
                    mutable,
                    base_offset=record_offset,
                    field_offset=located.get("_arrive_offset"),
                    value=waypoint_patch.arrive_ms,
                    field_label="arrive",
                    flight_label=patch.flight_label,
                    waypoint_index=waypoint_patch.waypoint_index,
                )
            if waypoint_patch.depart_ms is not None:
                _write_u32_field(
                    mutable,
                    base_offset=record_offset,
                    field_offset=located.get("_depart_offset"),
                    value=waypoint_patch.depart_ms,
                    field_label="depart",
                    flight_label=patch.flight_label,
                    waypoint_index=waypoint_patch.waypoint_index,
                )
    return bytes(mutable)


def _apply_record_patches_to_uni(
    uni_data: bytes,
    parsed_uni: ParsedUniData,
    record_patches: list[RecordWritePatch],
) -> bytes:
    record_by_id = parsed_uni.record_by_id()
    mutable = bytearray(uni_data)
    for patch in record_patches:
        record = record_by_id.get(patch.record_id)
        if not isinstance(record, dict):
            raise CamWritebackError(f"{patch.record_label}: record not found in .uni.")
        record_kind = record.get("kind")
        if patch.expected_kind is not None and record_kind != patch.expected_kind:
            raise CamWritebackError(
                f"{patch.record_label}: expected kind {patch.expected_kind!r}, found {record_kind!r}."
            )
        record_offset, record_size = _record_bounds(record, patch.record_label)
        for field in patch.fields:
            _write_record_field(
                mutable,
                baseline_uni=uni_data,
                base_offset=record_offset,
                record_size=record_size,
                field=field,
                record_label=patch.record_label,
            )
    return bytes(mutable)


def _verify_flight_patches(
    modified_uni: bytes,
    baseline_uni_bytes: bytes,
    baseline_uni: ParsedUniData,
    reparsed_uni: ParsedUniData,
    container_version: int | None,
    flight_patches: list[FlightWritePatch],
) -> None:
    reparsed_by_id = reparsed_uni.record_by_id()
    baseline_by_id = baseline_uni.record_by_id()
    for patch in flight_patches:
        record = reparsed_by_id.get(patch.flight_id)
        if not isinstance(record, dict):
            raise CamWritebackError(f"{patch.flight_label}: modified save lost the flight record.")
        baseline_record = baseline_by_id.get(patch.flight_id)
        if not isinstance(baseline_record, dict):
            raise CamWritebackError(f"{patch.flight_label}: baseline save lost the flight record.")
        record_offset = baseline_record.get("offset")
        record_size = baseline_record.get("size")
        if not isinstance(record_offset, int) or not isinstance(record_size, int):
            raise CamWritebackError(f"{patch.flight_label}: invalid baseline record bounds in .uni.")
        baseline_record_blob = baseline_uni_bytes[record_offset : record_offset + record_size]
        located_waypoints = _locate_displayed_waypoints(
            baseline_record_blob,
            container_version=container_version,
            kind=baseline_record.get("kind"),
            parsed_waypoints=baseline_record.get("waypoints") if isinstance(baseline_record.get("waypoints"), list) else [],
            flight_label=patch.flight_label,
        )
        by_index = {
            waypoint.get("index"): waypoint
            for waypoint in located_waypoints
            if isinstance(waypoint, dict) and isinstance(waypoint.get("index"), int)
        }
        for waypoint_patch in patch.waypoints:
            waypoint = by_index.get(waypoint_patch.waypoint_index)
            if not isinstance(waypoint, dict):
                raise CamWritebackError(
                    f"{patch.flight_label}: modified save lost waypoint {waypoint_patch.waypoint_index + 1}."
                )
            if waypoint_patch.altitude_ft is not None:
                expected_z = _altitude_ft_to_raw_z(waypoint_patch.altitude_ft, patch.flight_label, waypoint_patch.waypoint_index)
                z_offset = waypoint.get("_z_offset")
                if not isinstance(z_offset, int):
                    raise CamWritebackError(
                        f"{patch.flight_label}: altitude offset missing for waypoint {waypoint_patch.waypoint_index + 1}."
                    )
                actual_z = struct.unpack_from("<h", modified_uni, record_offset + z_offset)[0]
                if actual_z != expected_z:
                    raise CamWritebackError(
                        f"{patch.flight_label}: altitude verification failed for waypoint {waypoint_patch.waypoint_index + 1}."
                    )
            if waypoint_patch.arrive_ms is not None:
                arrive_offset = waypoint.get("_arrive_offset")
                if not isinstance(arrive_offset, int):
                    raise CamWritebackError(
                        f"{patch.flight_label}: arrive offset missing for waypoint {waypoint_patch.waypoint_index + 1}."
                    )
                actual_arrive = struct.unpack_from("<I", modified_uni, record_offset + arrive_offset)[0]
                if actual_arrive != waypoint_patch.arrive_ms:
                    raise CamWritebackError(
                        f"{patch.flight_label}: arrive-time verification failed for waypoint {waypoint_patch.waypoint_index + 1}."
                    )
            if waypoint_patch.depart_ms is not None:
                depart_offset = waypoint.get("_depart_offset")
                if not isinstance(depart_offset, int):
                    raise CamWritebackError(
                        f"{patch.flight_label}: depart offset missing for waypoint {waypoint_patch.waypoint_index + 1}."
                    )
                actual_depart = struct.unpack_from("<I", modified_uni, record_offset + depart_offset)[0]
                if actual_depart != waypoint_patch.depart_ms:
                    raise CamWritebackError(
                        f"{patch.flight_label}: depart-time verification failed for waypoint {waypoint_patch.waypoint_index + 1}."
                    )


def _verify_record_patches(
    modified_uni: bytes,
    baseline_uni_bytes: bytes,
    baseline_uni: ParsedUniData,
    reparsed_uni: ParsedUniData,
    record_patches: list[RecordWritePatch],
) -> None:
    baseline_by_id = baseline_uni.record_by_id()
    reparsed_by_id = reparsed_uni.record_by_id()
    for patch in record_patches:
        baseline_record = baseline_by_id.get(patch.record_id)
        if not isinstance(baseline_record, dict):
            raise CamWritebackError(f"{patch.record_label}: baseline save lost the record.")
        reparsed_record = reparsed_by_id.get(patch.record_id)
        if not isinstance(reparsed_record, dict):
            raise CamWritebackError(f"{patch.record_label}: modified save lost the record.")
        baseline_kind = baseline_record.get("kind")
        reparsed_kind = reparsed_record.get("kind")
        if patch.expected_kind is not None and baseline_kind != patch.expected_kind:
            raise CamWritebackError(
                f"{patch.record_label}: baseline kind changed to {baseline_kind!r}."
            )
        if patch.expected_kind is not None and reparsed_kind != patch.expected_kind:
            raise CamWritebackError(
                f"{patch.record_label}: modified record kind changed to {reparsed_kind!r}."
            )
        record_offset, record_size = _record_bounds(baseline_record, patch.record_label)
        for field in patch.fields:
            _verify_record_field(
                modified_uni=modified_uni,
                baseline_uni=baseline_uni_bytes,
                base_offset=record_offset,
                record_size=record_size,
                field=field,
                record_label=patch.record_label,
            )


def _record_bounds(record: dict[str, Any], record_label: str) -> tuple[int, int]:
    record_offset = record.get("offset")
    record_size = record.get("size")
    if not isinstance(record_offset, int) or not isinstance(record_size, int):
        raise CamWritebackError(f"{record_label}: invalid record bounds in .uni.")
    return record_offset, record_size


def _write_record_field(
    mutable: bytearray,
    *,
    baseline_uni: bytes,
    base_offset: int,
    record_size: int,
    field: RecordFieldWriteValue,
    record_label: str,
) -> None:
    field_offset = field.offset
    field_value = field.value
    if not isinstance(field_offset, int) or field_offset < 0:
        raise CamWritebackError(f"{record_label}: invalid record field offset.")
    if not isinstance(field_value, bytes) or not field_value:
        raise CamWritebackError(f"{record_label}: record field patch must supply non-empty bytes.")
    field_end = field_offset + len(field_value)
    if field_end > record_size:
        raise CamWritebackError(f"{record_label}: record field patch extends past record bounds.")
    absolute_offset = base_offset + field_offset
    baseline_value = baseline_uni[absolute_offset:absolute_offset + len(field_value)]
    if len(baseline_value) != len(field_value):
        raise CamWritebackError(f"{record_label}: could not read baseline bytes for record field patch.")
    if field.expected_before is not None and baseline_value != field.expected_before:
        field_name = field.field_label or f"offset {field_offset}"
        raise CamWritebackError(
            f"{record_label}: baseline bytes did not match expected value for {field_name}."
        )
    mutable[absolute_offset:absolute_offset + len(field_value)] = field_value


def _verify_record_field(
    *,
    modified_uni: bytes,
    baseline_uni: bytes,
    base_offset: int,
    record_size: int,
    field: RecordFieldWriteValue,
    record_label: str,
) -> None:
    field_offset = field.offset
    field_value = field.value
    if not isinstance(field_offset, int) or field_offset < 0:
        raise CamWritebackError(f"{record_label}: invalid record field offset during verification.")
    if not isinstance(field_value, bytes) or not field_value:
        raise CamWritebackError(f"{record_label}: invalid record field value during verification.")
    field_end = field_offset + len(field_value)
    if field_end > record_size:
        raise CamWritebackError(f"{record_label}: record field patch exceeds record bounds during verification.")
    absolute_offset = base_offset + field_offset
    actual_value = modified_uni[absolute_offset:absolute_offset + len(field_value)]
    if actual_value != field_value:
        field_name = field.field_label or f"offset {field_offset}"
        raise CamWritebackError(f"{record_label}: verification failed for {field_name}.")
    if field.expected_before is not None:
        baseline_value = baseline_uni[absolute_offset:absolute_offset + len(field.expected_before)]
        if baseline_value != field.expected_before:
            field_name = field.field_label or f"offset {field_offset}"
            raise CamWritebackError(
                f"{record_label}: baseline bytes changed unexpectedly for {field_name} during verification."
            )


def _apply_entry_patches_to_entries(
    modified_decoded_by_name: dict[str, bytes],
    decoded_entries: list[DecodedEntry],
    entry_patches: list[EntryWritePatch],
) -> None:
    by_name = {item.entry.name: item for item in decoded_entries}
    for patch in entry_patches:
        target_name = _find_entry_patch_target_name(decoded_entries, patch)
        item = by_name.get(target_name)
        if item is None:
            raise CamWritebackError(f"{patch.entry_label}: target entry not found.")
        baseline_data = item.decoded.data
        mutable = bytearray(modified_decoded_by_name.get(target_name, baseline_data))
        for field in patch.fields:
            _write_entry_field(
                mutable,
                baseline_entry=baseline_data,
                field=field,
                entry_label=patch.entry_label,
                entry_name=target_name,
            )
        modified_decoded_by_name[target_name] = bytes(mutable)


def _find_entry_patch_target_name(decoded_entries: list[DecodedEntry], patch: EntryWritePatch) -> str:
    matches: list[str] = []
    for item in decoded_entries:
        entry_name = item.entry.name
        if patch.entry_name is not None and entry_name != patch.entry_name:
            continue
        if patch.entry_ext is not None and Path(entry_name).suffix.lower() != patch.entry_ext.lower():
            continue
        if patch.entry_name is None and patch.entry_ext is None:
            continue
        matches.append(entry_name)
    if not matches:
        raise CamWritebackError(f"{patch.entry_label}: no entry matched the requested patch target.")
    if len(matches) > 1:
        raise CamWritebackError(f"{patch.entry_label}: patch target matched multiple entries: {matches!r}.")
    return matches[0]


def _write_entry_field(
    mutable: bytearray,
    *,
    baseline_entry: bytes,
    field: EntryFieldWriteValue,
    entry_label: str,
    entry_name: str,
) -> None:
    field_offset = field.offset
    field_value = field.value
    if not isinstance(field_offset, int) or field_offset < 0:
        raise CamWritebackError(f"{entry_label}: invalid decoded-entry offset for {entry_name}.")
    if not isinstance(field_value, bytes) or not field_value:
        raise CamWritebackError(f"{entry_label}: entry field patch must supply non-empty bytes.")
    field_end = field_offset + len(field_value)
    if field_end > len(baseline_entry):
        raise CamWritebackError(f"{entry_label}: decoded-entry patch extends past {entry_name} bounds.")
    baseline_value = baseline_entry[field_offset:field_end]
    if field.expected_before is not None and baseline_value != field.expected_before:
        field_name = field.field_label or f"offset {field_offset}"
        raise CamWritebackError(
            f"{entry_label}: baseline bytes did not match expected value for {field_name} in {entry_name}."
        )
    mutable[field_offset:field_end] = field_value


def _verify_entry_patches(
    rebuilt_blob: bytes,
    decoded_entries: list[DecodedEntry],
    entry_patches: list[EntryWritePatch],
) -> None:
    if not entry_patches:
        return
    rebuilt_entries = parse_container(rebuilt_blob)
    raw_by_name = {
        entry.name: rebuilt_blob[entry.offset : entry.offset + entry.length]
        for entry in rebuilt_entries
    }
    baseline_entries_by_name = {item.entry.name: item for item in decoded_entries}
    for patch in entry_patches:
        target_name = _find_entry_patch_target_name(decoded_entries, patch)
        raw = raw_by_name.get(target_name)
        if raw is None:
            raise CamWritebackError(f"{patch.entry_label}: rebuilt container is missing {target_name}.")
        try:
            decoded = _decode_compressed_entry(target_name, raw)
        except (CamFormatError, LzssError) as exc:
            raise CamWritebackError(f"{patch.entry_label}: rebuilt {target_name} could not be decoded: {exc}") from exc
        baseline_item = baseline_entries_by_name.get(target_name)
        if baseline_item is None:
            raise CamWritebackError(f"{patch.entry_label}: baseline entry {target_name} is missing.")
        for field in patch.fields:
            field_offset = field.offset
            field_end = field_offset + len(field.value)
            if decoded.data[field_offset:field_end] != field.value:
                field_name = field.field_label or f"offset {field_offset}"
                raise CamWritebackError(f"{patch.entry_label}: verification failed for {field_name} in {target_name}.")
            if field.expected_before is not None:
                baseline_value = baseline_item.decoded.data[field_offset:field_end]
                if baseline_value != field.expected_before:
                    field_name = field.field_label or f"offset {field_offset}"
                    raise CamWritebackError(
                        f"{patch.entry_label}: baseline bytes changed unexpectedly for {field_name} in {target_name}."
                    )


def _encode_cmp_entry(cmp_data: bytes) -> bytes:
    payload = lzss_compress(cmp_data)
    compressed_size = len(payload) + 4
    return struct.pack("<II", compressed_size, len(cmp_data)) + payload


def _encode_decoded_entry(
    entry_name: str,
    entry_data: bytes,
    *,
    metadata: dict[str, int],
) -> bytes:
    ext = Path(entry_name).suffix.lower()
    if ext == ".uni":
        record_count = metadata.get("record_count")
        if not isinstance(record_count, int):
            raise CamWritebackError("The .uni entry is missing record-count metadata.")
        return _encode_uni_entry(entry_data, record_count)
    if ext == ".cmp":
        return _encode_cmp_entry(entry_data)
    raise CamWritebackError(f"Writeback does not support rebuilding patched {ext} entries yet.")


def _encode_uni_entry(uni_data: bytes, record_count: int) -> bytes:
    payload = lzss_compress(uni_data)
    compressed_size = len(payload) + 6
    return (
        struct.pack("<IHI", compressed_size, record_count, len(uni_data))
        + payload
    )


def _build_container_blob(raw_entries: list[tuple[str, bytes]]) -> bytes:
    if not raw_entries:
        raise CamWritebackError("Cannot build an empty CAM container.")
    out = bytearray(b"\x00\x00\x00\x00")
    offsets: list[tuple[str, int, int]] = []
    for name, raw in raw_entries:
        entry_offset = len(out)
        out.extend(raw)
        offsets.append((name, entry_offset, len(raw)))
    directory_offset = len(out)
    out.extend(struct.pack("<I", len(offsets)))
    for name, entry_offset, length in offsets:
        encoded_name = name.encode("ascii")
        if len(encoded_name) > 255:
            raise CamWritebackError(f"Entry name is too long: {name!r}")
        out.append(len(encoded_name))
        out.extend(encoded_name)
        out.extend(struct.pack("<II", entry_offset, length))
    struct.pack_into("<I", out, 0, directory_offset)
    return bytes(out)


def _verify_container_blob(
    blob: bytes,
    uni_entry_name: str,
    baseline_decoded_entries: list[DecodedEntry],
    entry_patches: list[EntryWritePatch],
    flight_patches: list[FlightWritePatch],
    record_patches: list[RecordWritePatch],
    container_version: int | None,
    support_base_dir: Path | None,
) -> None:
    _verify_entry_patches(blob, baseline_decoded_entries, entry_patches)
    entries = parse_container(blob)
    raw_by_name = {
        entry.name: blob[entry.offset : entry.offset + entry.length]
        for entry in entries
    }
    uni_raw = raw_by_name.get(uni_entry_name)
    if uni_raw is None:
        raise CamWritebackError("Rebuilt CAM container is missing the .uni entry.")
    try:
        decoded_uni = _decode_compressed_entry(uni_entry_name, uni_raw)
    except (CamFormatError, LzssError) as exc:
        raise CamWritebackError(f"Rebuilt .uni entry could not be decoded: {exc}") from exc
    reparsed_uni = parse_uni(
        decoded_uni.data,
        container_version=container_version,
        support_base_dir=support_base_dir,
        decode_metadata=decoded_uni.metadata,
    )
    reparsed_by_id = reparsed_uni.record_by_id()
    for patch in flight_patches:
        if patch.flight_id not in reparsed_by_id:
            raise CamWritebackError(f"{patch.flight_label}: rebuilt container lost the flight record.")
    for patch in record_patches:
        record = reparsed_by_id.get(patch.record_id)
        if not isinstance(record, dict):
            raise CamWritebackError(f"{patch.record_label}: rebuilt container lost the record.")
        if patch.expected_kind is not None and record.get("kind") != patch.expected_kind:
            raise CamWritebackError(
                f"{patch.record_label}: rebuilt container changed record kind to {record.get('kind')!r}."
            )


def _temp_write_path_for(target_path: Path) -> Path:
    for counter in range(100):
        suffix = "" if counter == 0 else f".{counter:02d}"
        candidate = target_path.with_name(f".{target_path.name}.frag_helper_write.tmp{suffix}")
        if not candidate.exists():
            return candidate
    raise CamWritebackError("Could not allocate a temporary save path.")


def _verify_record_waypoint_mapping(
    flight_label: str,
    located_waypoints: list[dict[str, Any]],
    parsed_waypoints: list[dict[str, Any]],
) -> None:
    located_keys = [_waypoint_merge_key(waypoint) for waypoint in located_waypoints]
    parsed_keys = [_waypoint_merge_key(waypoint) for waypoint in parsed_waypoints if isinstance(waypoint, dict)]
    if located_keys != parsed_keys:
        raise CamWritebackError(
            f"{flight_label}: could not match displayed steerpoints to raw .uni waypoint bytes safely."
        )


def _locate_displayed_waypoints(
    record_blob: bytes,
    *,
    container_version: int | None,
    kind: Any,
    parsed_waypoints: list[dict[str, Any]],
    flight_label: str,
) -> list[dict[str, Any]]:
    located_waypoints = _locate_record_waypoints(
        record_blob,
        container_version=container_version,
        kind=kind,
    )
    try:
        _verify_record_waypoint_mapping(flight_label, located_waypoints, parsed_waypoints)
        return located_waypoints
    except CamWritebackError:
        pass

    candidate_arrays = _located_waypoint_candidate_arrays(
        record_blob,
        container_version=container_version,
        kind=kind,
    )
    resolved = _resolve_waypoints_by_key(parsed_waypoints, candidate_arrays)
    _verify_record_waypoint_mapping(flight_label, resolved, parsed_waypoints)
    return resolved


def _locate_record_waypoints(
    record_blob: bytes,
    *,
    container_version: int | None,
    kind: Any,
) -> list[dict[str, Any]]:
    primary = _extract_located_unit_waypoints(record_blob, container_version)
    primary_score = _waypoint_array_quality(primary)
    if kind == "squadron":
        scanned = _extract_located_unit_waypoints_scan(record_blob, container_version)
        scanned_score = _waypoint_array_quality(scanned)
        if scanned_score > primary_score:
            primary = scanned
    if kind == "squadron":
        candidates = _scan_located_waypoint_candidates(record_blob, container_version)
        if candidates:
            return _merge_located_waypoint_tail_candidates(primary, candidates)
    return _sanitize_located_waypoint_sequence(primary)


def _located_waypoint_candidate_arrays(
    record_blob: bytes,
    *,
    container_version: int | None,
    kind: Any,
) -> list[list[dict[str, Any]]]:
    arrays: list[list[dict[str, Any]]] = []
    primary = _extract_located_unit_waypoints(record_blob, container_version)
    sanitized_primary = _sanitize_located_waypoint_sequence(primary)
    if sanitized_primary:
        arrays.append(sanitized_primary)
    if kind == "squadron":
        scanned_primary = _extract_located_unit_waypoints_scan(record_blob, container_version)
        sanitized_scanned = _sanitize_located_waypoint_sequence(scanned_primary)
        if sanitized_scanned:
            arrays.append(sanitized_scanned)
        scan_candidates = _scan_located_waypoint_candidates(record_blob, container_version)
        arrays.extend(
            sanitized
            for candidate in scan_candidates
            if (sanitized := _sanitize_located_waypoint_sequence(candidate))
        )
        merged_primary = _merge_located_waypoint_tail_candidates(primary, scan_candidates)
        if merged_primary:
            arrays.append(merged_primary)
        if scanned_primary is not None:
            merged_scanned = _merge_located_waypoint_tail_candidates(scanned_primary, scan_candidates)
            if merged_scanned:
                arrays.append(merged_scanned)
    return arrays


def _resolve_waypoints_by_key(
    parsed_waypoints: list[dict[str, Any]],
    candidate_arrays: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for index, parsed_waypoint in enumerate(parsed_waypoints):
        key = _waypoint_merge_key(parsed_waypoint)
        matches: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidate_arrays:
            for waypoint in candidate:
                if _waypoint_merge_key(waypoint) != key:
                    continue
                identity = (
                    waypoint.get("_z_offset"),
                    waypoint.get("_arrive_offset"),
                    waypoint.get("_depart_offset"),
                )
                matches[identity] = waypoint
        if len(matches) != 1:
            raise CamWritebackError(
                f"Could not resolve raw byte offsets uniquely for waypoint {index + 1}."
            )
        located = dict(next(iter(matches.values())))
        located["index"] = parsed_waypoint.get("index", index)
        resolved.append(located)
    return resolved


def _extract_located_unit_waypoints(
    record_blob: bytes,
    container_version: int | None,
) -> list[dict[str, Any]] | None:
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
        return _read_located_waypoint_array(reader, num_waypoints, container_version)
    except Exception:
        return None


def _extract_located_unit_waypoints_scan(
    record_blob: bytes,
    container_version: int | None,
) -> list[dict[str, Any]] | None:
    if not isinstance(container_version, int):
        return None
    best_score = -1
    best: list[dict[str, Any]] | None = None
    for offset in range(0, max(0, len(record_blob) - 4)):
        try:
            reader = BinaryReader(record_blob[offset:])
            if container_version >= 71:
                num_waypoints = reader.read_u16()
            else:
                num_waypoints = reader.read_u8()
            if num_waypoints <= 1 or num_waypoints > 20:
                continue
            located = _read_located_waypoint_array(reader, num_waypoints, container_version, base_offset=offset)
        except Exception:
            continue
        score = _waypoint_array_quality(located)
        if score > best_score:
            best_score = score
            best = located
    return best


def _scan_located_waypoint_candidates(
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
            located = _read_located_waypoint_array(reader, num_waypoints, container_version, base_offset=offset)
        except Exception:
            continue
        score = _waypoint_array_quality(located)
        if score < 0:
            continue
        candidates.append((score, located))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [waypoints for _score, waypoints in candidates]


def _read_located_waypoint_array(
    reader: BinaryReader,
    num_waypoints: int,
    container_version: int,
    *,
    base_offset: int = 0,
) -> list[dict[str, Any]]:
    waypoints: list[dict[str, Any]] = []
    for index in range(num_waypoints):
        haves = reader.read_u8()
        x = reader.read_i16()
        y = reader.read_i16()
        z_offset = base_offset + reader.offset
        z = reader.read_i16()
        arrive_offset = base_offset + reader.offset
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
        depart_offset: int | None = None
        if haves & 0x01:
            depart_offset = base_offset + reader.offset
            depart = reader.read_u32()

        waypoints.append(
            {
                "index": index,
                "haves": haves,
                "x": x,
                "y": y,
                "z": z,
                "arrive_ms": arrive,
                "action": action,
                "route_action": route_action,
                "formation": packed_formation & 0x0F,
                "formation_spacing": ((packed_formation >> 4) & 0x0F) - 8,
                "flags": flags,
                "target_id": target_id,
                "target_building": target_building,
                "depart_ms": depart,
                "_z_offset": z_offset,
                "_arrive_offset": arrive_offset,
                "_depart_offset": depart_offset,
            }
        )
    return waypoints


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


def _sanitize_located_waypoint_sequence(waypoints: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
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


def _merge_located_waypoint_tail_candidates(
    primary_waypoints: list[dict[str, Any]] | None,
    candidate_waypoints: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    merged = _sanitize_located_waypoint_sequence(primary_waypoints)
    if not merged:
        return []
    seen = {_waypoint_merge_key(waypoint) for waypoint in merged}
    for candidate in candidate_waypoints:
        sanitized_candidate = _sanitize_located_waypoint_sequence(candidate)
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
        if not appended:
            continue
    for index, waypoint in enumerate(merged):
        waypoint["index"] = index
    return merged


def _write_altitude_ft(
    mutable: bytearray,
    *,
    base_offset: int,
    located_waypoint: dict[str, Any],
    altitude_ft: int,
    flight_label: str,
) -> None:
    raw_z = _altitude_ft_to_raw_z(altitude_ft, flight_label, located_waypoint.get("index", -1))
    field_offset = located_waypoint.get("_z_offset")
    if not isinstance(field_offset, int):
        raise CamWritebackError(
            f"{flight_label}: altitude field offset is missing for waypoint {located_waypoint.get('index', 0) + 1}."
        )
    struct.pack_into("<h", mutable, base_offset + field_offset, raw_z)


def _write_u32_field(
    mutable: bytearray,
    *,
    base_offset: int,
    field_offset: Any,
    value: int,
    field_label: str,
    flight_label: str,
    waypoint_index: int,
) -> None:
    if not isinstance(value, int) or value < 0 or value > 0xFFFFFFFF:
        raise CamWritebackError(
            f"{flight_label}: invalid {field_label} value for waypoint {waypoint_index + 1}."
        )
    if not isinstance(field_offset, int):
        raise CamWritebackError(
            f"{flight_label}: waypoint {waypoint_index + 1} has no writable {field_label} field in the save."
        )
    struct.pack_into("<I", mutable, base_offset + field_offset, value)


def _altitude_ft_to_raw_z(altitude_ft: int, flight_label: str, waypoint_index: int) -> int:
    if altitude_ft % 10 != 0:
        raise CamWritebackError(
            f"{flight_label}: waypoint {waypoint_index + 1} altitude must be in 10 ft increments for save writeback."
        )
    raw_z = altitude_ft // 10
    if raw_z < -32768 or raw_z > 32767:
        raise CamWritebackError(
            f"{flight_label}: waypoint {waypoint_index + 1} altitude is out of range for save writeback."
        )
    return raw_z


__all__ = [
    "CamWritebackError",
    "EntryFieldWriteValue",
    "EntryWritePatch",
    "FlightWritePatch",
    "RecordFieldWriteValue",
    "RecordWritePatch",
    "SaveWriteResult",
    "WaypointWriteValue",
    "apply_patched_cam_blob_atomic",
    "build_verified_patched_cam_blob",
]
