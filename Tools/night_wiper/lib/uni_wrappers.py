"""Typed convenience wrappers for parsed `.uni` unit records."""

from __future__ import annotations

from dataclasses import dataclass

from .support_files import SupportData, VehicleClassEntry, format_campaign_time_z
from .uni_parser import FieldMap, UnitRecord, Waypoint


UNIT_FLAG_HUMAN_CONTROLLED = 0x80


class UnitWrapperError(RuntimeError):
    """Raised when a human-readable unit edit cannot be resolved deterministically."""


@dataclass
class Unit:
    """Base wrapper around a parsed unit record."""

    record: UnitRecord
    support: SupportData

    @property
    def fields(self) -> FieldMap:
        return self.record.fields

    def get(self, name: str) -> object:
        return self.record.get(name)

    def set(self, name: str, value: object) -> None:
        self.record.set(name, value)

    @property
    def unit_id(self) -> tuple[int, int]:
        return self.record.unit_id

    @property
    def kind(self) -> str:
        return self.record.kind

    @property
    def aircraft_name(self) -> str | None:
        vehicle = _vehicle_for_unit_record(self.record, self.support)
        return None if vehicle is None else vehicle.name

    def to_view(self) -> dict[str, object]:
        return {
            "unit_id": _vuid_dict(self.unit_id),
            "unit_flags": self.record.unit_flags,
            "steerpoints": [
                _waypoint_view(index, waypoint)
                for index, waypoint in enumerate(self.record.waypoints)
            ],
            "aircraft": self.aircraft_view,
        }

    @property
    def aircraft_view(self) -> dict[str, object] | None:
        ct_entry = self.support.ct_by_number.get(self.record.ct_index)
        if ct_entry is None:
            return None
        unit_class = self.support.ucd_by_number.get(ct_entry.entity_idx)
        vehicle = _vehicle_for_unit_record(self.record, self.support)
        if unit_class is None or vehicle is None:
            return None
        return {
            "unit_class_index": unit_class.number,
            "unit_class_name": unit_class.name,
            "vehicle_ct_index": vehicle.ct_idx,
            "vehicle_name": vehicle.name,
            "callsign_idx": vehicle.callsign_idx,
            "callsign_slots": vehicle.callsign_slots,
        }


@dataclass
class FlightUnit(Unit):
    """Convenience API for flight unit records."""

    @property
    def mission_code(self) -> int:
        return int(self.get("mission"))

    @mission_code.setter
    def mission_code(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError(f"mission_code must fit in uint8, got {value}")
        self.set("mission", value)

    @property
    def mission_name(self) -> str | None:
        return self.support.strings_by_id.get(300 + self.mission_code)

    def set_mission_by_string(self, name: str) -> None:
        self.mission_code = _mission_code_by_string(self.support, name)

    @property
    def old_mission_code(self) -> int:
        return int(self.get("old_mission"))

    @old_mission_code.setter
    def old_mission_code(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError(f"old_mission_code must fit in uint8, got {value}")
        self.set("old_mission", value)

    @property
    def callsign_id(self) -> int:
        return int(self.get("callsign_id"))

    @callsign_id.setter
    def callsign_id(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError(f"callsign_id must fit in uint8, got {value}")
        self.set("callsign_id", value)

    @property
    def callsign_num(self) -> int:
        return int(self.get("callsign_num"))

    @callsign_num.setter
    def callsign_num(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError(f"callsign_num must fit in uint8, got {value}")
        self.set("callsign_num", value)

    @property
    def callsign(self) -> str | None:
        root = self.support.strings_by_id.get(2000 + self.callsign_id)
        if root is None:
            return None
        return f"{root}{self.callsign_num}"

    def set_callsign_by_string(self, callsign: str) -> None:
        callsign_id, callsign_num = _callsign_parts_by_string(self.support, callsign)
        self.callsign_id = callsign_id
        self.callsign_num = callsign_num

    @property
    def time_on_target_ms(self) -> int:
        return int(self.get("time_on_target"))

    def to_view(self) -> dict[str, object]:
        view = super().to_view()
        view["callsign"] = self.callsign_view
        view["flight"] = {
            "time_on_target_ms": self.time_on_target_ms,
            "time_on_target_z": format_campaign_time_z(self.time_on_target_ms),
            "mission_over_time_ms": int(self.get("mission_over_time")),
            "mission_over_time_z": format_campaign_time_z(int(self.get("mission_over_time"))),
            "mission_code": self.mission_code,
            "mission_name": self.mission_name,
            "old_mission_code": self.old_mission_code,
            "old_mission_name": self.support.strings_by_id.get(300 + self.old_mission_code),
            "last_direction": self.get("last_direction"),
            "priority": self.get("priority"),
            "mission_id": self.get("mission_id"),
            "eval_flags": self.get("eval_flags"),
            "mission_context": self.get("mission_context"),
            "package_id": _vuid_dict(self.get("package_id")),
            "squadron_id": _vuid_dict(self.get("squadron_id")),
            "requester_id": _vuid_dict(self.get("requester_id")),
            "slots": list(self.get("slots")),
            "pilots": list(self.get("pilots")),
            "plane_stats": list(self.get("plane_stats")),
            "player_slots": list(self.get("player_slots")),
            "last_player_slot": self.get("last_player_slot"),
            "aircraft_count": _aircraft_count(
                tuple(int(value) for value in self.get("plane_stats"))
            ),
            "callsign": self.callsign_view,
        }
        return view

    @property
    def callsign_view(self) -> dict[str, object] | None:
        root = self.support.strings_by_id.get(2000 + self.callsign_id)
        if root is None or not 1 <= self.callsign_num <= 9:
            return None
        return {
            "strings_id": 2000 + self.callsign_id,
            "callsign_id": self.callsign_id,
            "callsign_num": self.callsign_num,
            "root": root,
            "name": f"{root}{self.callsign_num}",
        }


@dataclass
class SquadronUnit(Unit):
    """Convenience API for squadron unit records."""

    @property
    def human_controlled(self) -> bool:
        return bool(self.record.unit_flags & UNIT_FLAG_HUMAN_CONTROLLED)

    @human_controlled.setter
    def human_controlled(self, value: bool) -> None:
        flags = self.record.unit_flags
        if value:
            flags |= UNIT_FLAG_HUMAN_CONTROLLED
        else:
            flags &= ~UNIT_FLAG_HUMAN_CONTROLLED
        self.set("unit_flags", flags)

    @property
    def specialty_code(self) -> int:
        return int(self.get("specialty"))

    @specialty_code.setter
    def specialty_code(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError(f"specialty_code must fit in uint8, got {value}")
        self.set("specialty", value)

    @property
    def specialty_string(self) -> str | None:
        if self.specialty_code == 0:
            return "Set by HQ"
        return self.support.strings_by_id.get(300 + self.specialty_code)

    def set_specialty_by_string(self, name: str) -> None:
        if _normalized(name) == _normalized("Set by HQ"):
            self.specialty_code = 0
            return
        self.specialty_code = _mission_code_by_string(self.support, name)

    def to_view(self) -> dict[str, object]:
        view = super().to_view()
        view["squadron"] = {
            "human_controlled": self.human_controlled,
            "specialty": {
                "code": self.specialty_code,
                "name": self.specialty_string,
                "set_by_hq": self.specialty_code == 0,
            }
        }
        return view


@dataclass
class PackageUnit(Unit):
    """Convenience API for package unit records."""

    @property
    def element_ids(self) -> tuple[tuple[int, int], ...]:
        if "element_ids" not in self.fields:
            return ()
        return tuple(self.get("element_ids"))

    @property
    def support_ids(self) -> tuple[tuple[int, int], ...]:
        if "support_ids" not in self.fields:
            return ()
        return tuple(self.get("support_ids"))


def wrap_unit(record: UnitRecord, support: SupportData) -> Unit:
    if record.kind == "flight":
        return FlightUnit(record, support)
    if record.kind == "squadron":
        return SquadronUnit(record, support)
    if record.kind == "package":
        return PackageUnit(record, support)
    return Unit(record, support)


def _vehicle_for_unit_record(
    record: UnitRecord,
    support: SupportData,
) -> VehicleClassEntry | None:
    ct_entry = support.ct_by_number.get(record.ct_index)
    if ct_entry is None:
        return None
    unit_class = support.ucd_by_number.get(ct_entry.entity_idx)
    if unit_class is None or not unit_class.vehicle_ct_indices:
        return None
    return support.vcd_by_ct_idx.get(unit_class.vehicle_ct_indices[0])


def _waypoint_view(index: int, waypoint: Waypoint) -> dict[str, int | str | None]:
    return {
        "index": index,
        "haves": waypoint.haves,
        "x": waypoint.x,
        "y": waypoint.y,
        "z": waypoint.z,
        "arrive_ms": waypoint.arrive_ms,
        "arrive_z": format_campaign_time_z(waypoint.arrive_ms),
        "depart_ms": waypoint.depart_ms,
        "depart_z": format_campaign_time_z(waypoint.depart_ms),
        "action": waypoint.action,
        "route_action": waypoint.route_action,
        "formation": waypoint.formation,
        "flags": waypoint.flags,
    }


def _aircraft_count(plane_stats: tuple[int, ...]) -> int:
    for index in range(min(len(plane_stats), 4) - 1, 0, -1):
        if plane_stats[index] != 0:
            return index + 1
    return 1


def _vuid_dict(value) -> dict[str, int]:
    return {"num": int(value[0]), "creator": int(value[1])}


def _mission_code_by_string(support: SupportData, name: str) -> int:
    wanted = _normalized(name)
    matches = [
        string_id - 300
        for string_id, value in support.strings_by_id.items()
        if 300 <= string_id <= 341 and _normalized(value) == wanted
    ]
    if len(matches) != 1:
        raise UnitWrapperError(f"mission string {name!r} resolved to {len(matches)} matches")
    return matches[0]


def _callsign_parts_by_string(support: SupportData, callsign: str) -> tuple[int, int]:
    text = callsign.strip()
    if len(text) < 2 or not text[-1].isdigit():
        raise UnitWrapperError(f"callsign must end with a numeric flight number: {callsign!r}")
    callsign_num = int(text[-1])
    root = text[:-1]
    matches = [
        string_id - 2000
        for string_id, value in support.strings_by_id.items()
        if 2000 <= string_id <= 2255 and _normalized(value) == _normalized(root)
    ]
    if len(matches) != 1:
        raise UnitWrapperError(f"callsign root {root!r} resolved to {len(matches)} matches")
    return matches[0], callsign_num


def _normalized(value: str) -> str:
    return " ".join(value.strip().casefold().split())
