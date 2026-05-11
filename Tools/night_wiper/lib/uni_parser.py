"""Deterministic `.uni` unit-record splitting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import struct

from .binary import BinaryParseError, BinaryReader
from .cam_container import DecodedEntry
from .support_files import ClassTableEntry, SupportData


MAX_WAYPOINTS = 500
SUPPORT_SLOT_COUNT = 4
FLIGHT_SLOT_COUNT = 4
LOADOUT_ENTRY_SIZE = 48
WAYPOINT_TARGET_DATA_SIZE = 8 + 1 + ((8 + 1) * 4)

VuId = tuple[int, int]
FieldMap = dict[str, "Field"]

AIR_UNIT_KINDS = {1: "flight", 2: "package", 3: "squadron"}
GROUND_UNIT_KINDS = {1: "battalion", 2: "brigade"}


class UniRecordError(RuntimeError):
    """Raised when a `.uni` unit record cannot be split deterministically."""


@dataclass(frozen=True)
class Waypoint:
    haves: int
    x: int
    y: int
    z: int
    arrive_ms: int
    depart_ms: int
    action: int
    route_action: int
    formation: int
    flags: int
    target_data: bytes = b""


@dataclass
class Field:
    """One parsed binary field value."""

    name: str
    value: object
    codec: "Codec"


class Codec:
    """Binary encoder for one parsed field value."""

    def encode(self, value: object) -> bytes:
        raise NotImplementedError


@dataclass(frozen=True)
class StructCodec(Codec):
    fmt: str

    def encode(self, value: object) -> bytes:
        return struct.pack(self.fmt, value)


@dataclass(frozen=True)
class BytesCodec(Codec):
    size: int | None = None

    def encode(self, value: object) -> bytes:
        if not isinstance(value, bytes):
            raise TypeError("bytes field value must be bytes")
        if self.size is not None and len(value) != self.size:
            raise ValueError(f"bytes field expected {self.size} bytes, got {len(value)}")
        return value


@dataclass(frozen=True)
class VuIdCodec(Codec):
    def encode(self, value: object) -> bytes:
        num, creator = _as_pair(value)
        return struct.pack("<II", num, creator)


@dataclass(frozen=True)
class VuIdListCodec(Codec):
    def encode(self, value: object) -> bytes:
        return b"".join(VUID.encode(item) for item in _as_tuple(value))


@dataclass(frozen=True)
class U8TupleCodec(Codec):
    size: int

    def encode(self, value: object) -> bytes:
        items = _as_tuple(value)
        if len(items) != self.size:
            raise ValueError(f"expected {self.size} u8 values, got {len(items)}")
        return bytes(int(item) for item in items)


@dataclass(frozen=True)
class WaypointListCodec(Codec):
    include_count: bool

    def encode(self, value: object) -> bytes:
        waypoints = _as_tuple(value)
        payload = bytearray()
        if self.include_count:
            payload.extend(U16.encode(len(waypoints)))
        for waypoint in waypoints:
            if not isinstance(waypoint, Waypoint):
                raise TypeError("waypoint list values must be Waypoint instances")
            payload.extend(_encode_waypoint(waypoint))
        return bytes(payload)


I16 = StructCodec("<h")
U16 = StructCodec("<H")
I32 = StructCodec("<i")
U32 = StructCodec("<I")
U8 = StructCodec("<B")
VUID = VuIdCodec()
VUID_LIST = VuIdListCodec()
U8_FLIGHT_SLOTS = U8TupleCodec(FLIGHT_SLOT_COUNT)
WAYPOINTS_WITH_COUNT = WaypointListCodec(include_count=True)
WAYPOINTS_NO_COUNT = WaypointListCodec(include_count=False)


@dataclass(frozen=True)
class UnitRecord:
    index: int
    start_offset: int
    end_offset: int
    kind: str
    unit_type: int
    ct_index: int
    fields: FieldMap
    raw: bytes

    @property
    def size(self) -> int:
        return self.end_offset - self.start_offset

    @property
    def unit_id(self) -> VuId:
        value = self.get("unit_id")
        if not isinstance(value, tuple) or len(value) != 2:
            raise UniRecordError("unit_id value is malformed")
        return int(value[0]), int(value[1])

    @property
    def unit_flags(self) -> int:
        return int(self.get("unit_flags"))

    @property
    def waypoints(self) -> tuple[Waypoint, ...]:
        value = self.get("waypoints")
        if not isinstance(value, tuple):
            raise UniRecordError("waypoints value is malformed")
        return value

    def get(self, name: str) -> object:
        return self.fields[name].value

    def set(self, name: str, value: object) -> None:
        self.fields[name].value = value

    def to_bytes(self) -> bytes:
        return b"".join(field.codec.encode(field.value) for field in self.fields.values())


def parse_uni_records(
    entry: DecodedEntry,
    *,
    container_version: int,
    support: SupportData,
) -> tuple[UnitRecord, ...]:
    """Parse one decoded `.uni` entry into top-level unit records."""

    if not entry.name.lower().endswith(".uni"):
        raise ValueError(f"expected a .uni entry, got {entry.name!r}")

    record_count = entry.metadata.get("record_count")
    if not isinstance(record_count, int):
        raise UniRecordError(f"{entry.name}: missing .uni record_count metadata")

    return split_unit_records(
        entry.decoded,
        record_count=record_count,
        class_table=support.ct_by_number,
        version=container_version,
    )


def encode_uni_records(records: Sequence[UnitRecord]) -> bytes:
    """Encode parsed records back into a decoded `.uni` byte payload."""

    return b"".join(record.to_bytes() for record in records)


def split_unit_records(
    data: bytes,
    *,
    record_count: int,
    class_table: dict[int, ClassTableEntry],
    version: int,
) -> tuple[UnitRecord, ...]:
    """Split decoded `.uni` bytes into top-level unit records.

    The version is accepted for caller diagnostics; compatibility is determined
    by structural parsing checks instead of a hard-coded allow-list.
    """

    reader = BinaryReader(data)
    records: list[UnitRecord] = []
    for index in range(record_count):
        start = reader.tell()
        unit_type = reader.i16()
        ct_index = unit_type - 100
        class_entry = class_table.get(ct_index)
        if class_entry is None:
            raise UniRecordError(f"unknown unit type {unit_type} at offset {start}")
        kind = unit_kind(class_entry)
        if kind is None:
            raise UniRecordError(f"unsupported unit kind for CT {ct_index} at offset {start}")

        try:
            fields: FieldMap = {}
            _set(fields, "unit_type", unit_type, I16)
            _read_unit_record_by_kind(fields, reader, kind, unit_type)
        except BinaryParseError as exc:
            raise UniRecordError(f"truncated {kind} record at offset {start}: {exc}") from exc

        end = reader.tell()
        records.append(
            UnitRecord(
                index=index,
                start_offset=start,
                end_offset=end,
                kind=kind,
                unit_type=unit_type,
                ct_index=ct_index,
                fields=fields,
                raw=data[start:end],
            )
        )

    if reader.tell() != len(data):
        raise UniRecordError(
            f"record walk ended at {reader.tell()}, decoded payload has {len(data)} bytes"
        )
    return tuple(records)


def unit_kind(entry: ClassTableEntry) -> str | None:
    if entry.domain == 2:
        return AIR_UNIT_KINDS.get(entry.type_)
    if entry.domain == 3:
        return GROUND_UNIT_KINDS.get(entry.type_)
    if entry.domain == 4 and entry.type_ == 1:
        return "task_force"
    return None


def _read_unit_record_by_kind(
    fields: FieldMap,
    reader: BinaryReader,
    kind: str,
    unit_type: int,
) -> None:
    if kind == "flight":
        _read_flight_record(fields, reader, unit_type)
        return
    if kind == "package":
        _read_package_record(fields, reader, unit_type)
        return
    if kind == "squadron":
        _read_squadron_record(fields, reader, unit_type)
        return
    if kind == "battalion":
        _read_battalion_record(fields, reader, unit_type)
        return
    if kind == "brigade":
        _read_brigade_record(fields, reader, unit_type)
        return
    if kind == "task_force":
        _read_task_force_record(fields, reader, unit_type)
        return
    raise UniRecordError(f"unsupported unit kind {kind!r}")


def _read_unit_prefix_after_type(
    fields: FieldMap,
    reader: BinaryReader,
    unit_type: int,
) -> None:
    _set(fields, "unit_id", reader.vu_id(), VUID)
    _set(fields, "entity_type_copy", reader.i16(), I16)
    if fields["entity_type_copy"].value != unit_type:
        raise UniRecordError(
            f"unit type copy mismatch: {fields['entity_type_copy'].value} != {unit_type}"
        )

    _set(fields, "x", reader.i16(), I16)
    _set(fields, "y", reader.i16(), I16)
    _opaque(fields, reader, "z_raw", 4)
    _set(fields, "spot_time", reader.u32(), U32)
    _set(fields, "spotted", reader.i16(), I16)
    _set(fields, "base_flags", reader.i16(), I16)
    _set(fields, "owner", reader.u8(), U8)
    _set(fields, "camp_id", reader.i16(), I16)
    _set(fields, "last_check", reader.u32(), U32)
    _set(fields, "roster", reader.i32(), I32)
    _set(fields, "unit_flags", reader.i32(), I32)
    _set(fields, "dest_x", reader.i16(), I16)
    _set(fields, "dest_y", reader.i16(), I16)
    _set(fields, "target_id", reader.vu_id(), VUID)
    _set(fields, "cargo_id", reader.vu_id(), VUID)
    _set(fields, "moved", reader.u8(), U8)
    _set(fields, "losses", reader.u8(), U8)
    _set(fields, "tactic", reader.u8(), U8)
    _set(fields, "current_wp", reader.u16(), U16)
    _set(fields, "name_id", reader.i16(), I16)
    _set(fields, "reinforcement", reader.i16(), I16)
    _set(fields, "waypoints", tuple(_read_waypoints(reader)), WAYPOINTS_WITH_COUNT)


def _read_waypoints(reader: BinaryReader) -> list[Waypoint]:
    count = reader.u16()
    if count > MAX_WAYPOINTS:
        raise UniRecordError(f"suspicious waypoint count {count}")
    return [_read_waypoint(reader) for _ in range(count)]


def _read_waypoint(reader: BinaryReader) -> Waypoint:
    haves = reader.u8()
    x = reader.i16()
    y = reader.i16()
    z = reader.i16()
    arrive = reader.u32()
    action = reader.u8()
    route_action = reader.u8()
    formation = reader.u8()
    flags = reader.u32()
    target_data = b""
    if haves & 0x02:
        target_data = reader.read_bytes(WAYPOINT_TARGET_DATA_SIZE)
    depart = arrive
    if haves & 0x01:
        depart = reader.u32()
    return Waypoint(
        haves=haves,
        x=x,
        y=y,
        z=z,
        arrive_ms=arrive,
        depart_ms=depart,
        action=action,
        route_action=route_action,
        formation=formation,
        flags=flags,
        target_data=target_data,
    )


def _read_flight_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _opaque(fields, reader, "flight_z_fuel_burnt_raw", 4 + 4)
    _opaque(fields, reader, "fuel_initial_raw", 4 * 4)
    _opaque(fields, reader, "laser_code_raw", 2 * 4)
    _opaque(fields, reader, "last_move_last_combat_raw", 4 + 4)
    _set(fields, "time_on_target", reader.u32(), U32)
    _set(fields, "mission_over_time", reader.u32(), U32)
    _set(fields, "mission_target", reader.i16(), I16)
    _set(fields, "loadouts", reader.u8(), U8)
    _opaque(fields, reader, "loadout_raw", int(fields["loadouts"].value) * LOADOUT_ENTRY_SIZE)
    _set(fields, "mission", reader.u8(), U8)
    _set(fields, "old_mission", reader.u8(), U8)
    _set(fields, "last_direction", reader.u8(), U8)
    _set(fields, "priority", reader.u8(), U8)
    _set(fields, "mission_id", reader.u8(), U8)
    _set(fields, "eval_flags", reader.u8(), U8)
    _set(fields, "mission_context", reader.u8(), U8)
    _set(fields, "package_id", reader.vu_id(), VUID)
    _set(fields, "squadron_id", reader.vu_id(), VUID)
    _set(fields, "requester_id", reader.vu_id(), VUID)
    _set(fields, "slots", _read_u8_tuple(reader, FLIGHT_SLOT_COUNT), U8_FLIGHT_SLOTS)
    _set(fields, "pilots", _read_u8_tuple(reader, FLIGHT_SLOT_COUNT), U8_FLIGHT_SLOTS)
    _set(
        fields,
        "plane_stats",
        _read_u8_tuple(reader, FLIGHT_SLOT_COUNT),
        U8_FLIGHT_SLOTS,
    )
    _set(
        fields,
        "player_slots",
        _read_u8_tuple(reader, FLIGHT_SLOT_COUNT),
        U8_FLIGHT_SLOTS,
    )
    _set(fields, "last_player_slot", reader.u8(), U8)
    _set(fields, "callsign_id", reader.u8(), U8)
    _set(fields, "callsign_num", reader.u8(), U8)
    _opaque(fields, reader, "refuel_quantity_raw", 4)
    _opaque(fields, reader, "tex_set_raw", 4 * 4)
    _opaque(fields, reader, "tacan_raw", 4 + 4)
    _opaque(fields, reader, "loaded_cft_raw", 4)


def _read_package_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _set(fields, "elements", reader.u8(), U8)
    _set(
        fields,
        "element_ids",
        tuple(reader.vu_id() for _ in range(int(fields["elements"].value))),
        VUID_LIST,
    )
    _set(fields, "interceptor_id", reader.vu_id(), VUID)
    _set(
        fields,
        "support_ids",
        _read_vuid_tuple(reader, SUPPORT_SLOT_COUNT),
        VUID_LIST,
    )
    _set(fields, "wait_cycles", reader.u8(), U8)
    is_final = bool(int(fields["unit_flags"].value) & 0x00100000)
    if is_final and fields["wait_cycles"].value == 0:
        _opaque(fields, reader, "package_final_requests_responses_raw", 2 + 2)
        _opaque(fields, reader, "package_final_mis_request_header_raw", 1 + 1 + 1 + 1)
        _opaque(fields, reader, "package_final_mis_request_ids_raw", 8 + 8)
        _opaque(fields, reader, "package_final_mis_request_tail_raw", 4 + 1 + 2)
        return

    _opaque(fields, reader, "package_flights_wait_for_raw", 1 + 2)
    _opaque(fields, reader, "package_coordinates_raw", 2 * 8)
    _opaque(fields, reader, "package_takeoff_tp_flags_raw", 4 + 4 + 4)
    _opaque(fields, reader, "package_caps_requests_responses_raw", 2 + 2 + 2)
    _set(fields, "ingress_count", reader.u8(), U8)
    _set(
        fields,
        "ingress_waypoints",
        tuple(_read_waypoint(reader) for _ in range(int(fields["ingress_count"].value))),
        WAYPOINTS_NO_COUNT,
    )
    _set(fields, "egress_count", reader.u8(), U8)
    _set(
        fields,
        "egress_waypoints",
        tuple(_read_waypoint(reader) for _ in range(int(fields["egress_count"].value))),
        WAYPOINTS_NO_COUNT,
    )
    _opaque(fields, reader, "package_mis_request_ids_raw", 8 + 8 + 8 + 8)
    _opaque(fields, reader, "package_mis_request_who_vs_raw", 1 + 1 + 2)
    _opaque(fields, reader, "package_mis_request_tot_xy_flags_raw", 4 + 2 + 2 + 4)
    _opaque(fields, reader, "package_mis_request_caps_target_raw", 2 + 2 + 2 + 2 + 2)
    _opaque(fields, reader, "package_mis_request_type_mission_raw", 1 + 1 + 1 + 1 + 1 + 1)
    _opaque(fields, reader, "package_mis_request_delayed_blocks_raw", 1 + 1 + 1 + 4 + 1 + 1 + 3)


def _read_squadron_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _opaque(fields, reader, "squadron_fuel_raw", 4)
    _set(fields, "specialty", reader.u8(), U8)
    _opaque(fields, reader, "camp_specific_rating_raw", 16)
    _opaque(fields, reader, "stores_raw", 1000)
    _opaque(fields, reader, "pilots_raw", 48 * 10)
    _opaque(fields, reader, "schedule_raw", 16 * 4)
    _opaque(fields, reader, "airbase_hot_spot_raw", 8 + 8)
    _opaque(fields, reader, "rating_raw", 16)
    _opaque(fields, reader, "kills_missions_score_raw", 2 * 6)
    _opaque(fields, reader, "losses_raw", 1 + 1)
    _opaque(fields, reader, "squadron_patch_raw", 2)
    _opaque(fields, reader, "squadron_retask_relocate_tex_raw", 4 + 1 + 4)


def _read_battalion_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _opaque(fields, reader, "ground_unit_header_raw", 1 + 2 + 8)
    _opaque(fields, reader, "battalion_last_move_combat_raw", 4 + 4)
    _opaque(fields, reader, "battalion_parent_last_obj_raw", 8 + 8)
    _opaque(fields, reader, "battalion_status_raw", 1 + 1 + 1 + 1 + 1 + 1)


def _read_brigade_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _opaque(fields, reader, "ground_unit_header_raw", 1 + 2 + 8)
    _set(fields, "elements", reader.u8(), U8)
    _set(
        fields,
        "element_ids",
        tuple(reader.vu_id() for _ in range(int(fields["elements"].value))),
        VUID_LIST,
    )


def _read_task_force_record(fields: FieldMap, reader: BinaryReader, unit_type: int) -> None:
    _read_unit_prefix_after_type(fields, reader, unit_type)
    _set(fields, "orders", reader.u8(), U8)
    _set(fields, "supply", reader.u8(), U8)


def _set(fields: FieldMap, name: str, value: object, codec: Codec) -> None:
    fields[name] = Field(name=name, value=value, codec=codec)


def _opaque(fields: FieldMap, reader: BinaryReader, name: str, size: int) -> None:
    _set(fields, name, reader.read_bytes(size), BytesCodec(size))


def _read_u8_tuple(reader: BinaryReader, count: int) -> tuple[int, ...]:
    return tuple(reader.u8() for _ in range(count))


def _read_vuid_tuple(reader: BinaryReader, count: int) -> tuple[VuId, ...]:
    return tuple(reader.vu_id() for _ in range(count))


def _encode_waypoint(waypoint: Waypoint) -> bytes:
    payload = bytearray()
    payload.extend(U8.encode(waypoint.haves))
    payload.extend(I16.encode(waypoint.x))
    payload.extend(I16.encode(waypoint.y))
    payload.extend(I16.encode(waypoint.z))
    payload.extend(U32.encode(waypoint.arrive_ms))
    payload.extend(U8.encode(waypoint.action))
    payload.extend(U8.encode(waypoint.route_action))
    payload.extend(U8.encode(waypoint.formation))
    payload.extend(U32.encode(waypoint.flags))
    if waypoint.haves & 0x02:
        if len(waypoint.target_data) != WAYPOINT_TARGET_DATA_SIZE:
            raise ValueError("waypoint target_data has invalid length")
        payload.extend(waypoint.target_data)
    if waypoint.haves & 0x01:
        payload.extend(U32.encode(waypoint.depart_ms))
    return bytes(payload)


def _as_tuple(value: object) -> tuple:
    if not isinstance(value, tuple):
        raise TypeError("field value must be a tuple")
    return value


def _as_pair(value: object) -> tuple[int, int]:
    items = _as_tuple(value)
    if len(items) != 2:
        raise ValueError(f"expected VU_ID pair, got {len(items)} values")
    return int(items[0]), int(items[1])
