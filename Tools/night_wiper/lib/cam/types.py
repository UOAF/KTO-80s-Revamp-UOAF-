from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VUIdTuple = tuple[int, int]


def _to_vuid_tuple(value: Any) -> VUIdTuple | None:
    if not isinstance(value, dict):
        return None
    num = value.get("num")
    creator = value.get("creator")
    if isinstance(num, int) and isinstance(creator, int):
        return (num, creator)
    return None


def _to_warning_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ParsedCmpData:
    file_type: str
    size: int
    current_time_ms: int | None
    current_time_z: str
    bullseye_name_id: int | None
    bullseye_x: int | None
    bullseye_y: int | None
    player_squadron_id: VUIdTuple | None
    theater_name: str
    scenario: str
    save_file: str
    ui_name: str
    air_unit_rows: tuple[dict[str, Any], ...] = ()
    undocumented_tail_size: int | None = None
    undocumented_tail_head_hex: str = ""
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ParsedCmpData":
        data = raw if isinstance(raw, dict) else {}
        current_time_z = data.get("current_time_z")
        theater_name = data.get("theater_name")
        scenario = data.get("scenario")
        save_file = data.get("save_file")
        ui_name = data.get("ui_name")
        return cls(
            file_type="cmp",
            size=data.get("size") if isinstance(data.get("size"), int) else 0,
            current_time_ms=data.get("current_time") if isinstance(data.get("current_time"), int) else None,
            current_time_z=current_time_z if isinstance(current_time_z, str) else "",
            bullseye_name_id=(
                data.get("bullseye_name_id") if isinstance(data.get("bullseye_name_id"), int) else None
            ),
            bullseye_x=data.get("bullseye_x") if isinstance(data.get("bullseye_x"), int) else None,
            bullseye_y=data.get("bullseye_y") if isinstance(data.get("bullseye_y"), int) else None,
            player_squadron_id=_to_vuid_tuple(data.get("player_squadron_id")),
            theater_name=theater_name.strip() if isinstance(theater_name, str) else "",
            scenario=scenario.strip() if isinstance(scenario, str) else "",
            save_file=save_file.strip() if isinstance(save_file, str) else "",
            ui_name=ui_name.strip() if isinstance(ui_name, str) else "",
            air_unit_rows=tuple(item for item in data.get("air_unit_rows", []) if isinstance(item, dict)),
            undocumented_tail_size=(
                data.get("undocumented_tail_size")
                if isinstance(data.get("undocumented_tail_size"), int)
                else None
            ),
            undocumented_tail_head_hex=(
                data.get("undocumented_tail_head_hex")
                if isinstance(data.get("undocumented_tail_head_hex"), str)
                else ""
            ),
            warnings=_to_warning_tuple(data.get("warnings")),
        )

    def air_unit_row_by_flight_id(self) -> dict[VUIdTuple, dict[str, Any]]:
        out: dict[VUIdTuple, dict[str, Any]] = {}
        for row in self.air_unit_rows:
            flight_id = _to_vuid_tuple(row.get("flight_id"))
            if flight_id is None:
                continue
            out[flight_id] = row
        return out


@dataclass(frozen=True)
class ParsedUniData:
    file_type: str
    size: int
    container_version: int | None
    ct_path: str
    ucd_path: str
    vcd_path: str
    strings_path: str
    expected_record_count: int | None
    detected_record_count: int | None
    kind_counts: dict[str, int]
    record_size_stats_by_kind: dict[str, dict[str, int]]
    package_count: int
    flight_count: int
    package_flight_links: tuple[dict[str, Any], ...]
    packages: tuple[dict[str, Any], ...]
    flights: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    note: str = ""
    head_hex: str = ""
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ParsedUniData":
        data = raw if isinstance(raw, dict) else {}
        kind_counts = data.get("kind_counts")
        record_size_stats_by_kind = data.get("record_size_stats_by_kind")
        return cls(
            file_type="uni",
            size=data.get("size") if isinstance(data.get("size"), int) else 0,
            container_version=(
                data.get("container_version") if isinstance(data.get("container_version"), int) else None
            ),
            ct_path=data.get("ct_path") if isinstance(data.get("ct_path"), str) else "",
            ucd_path=data.get("ucd_path") if isinstance(data.get("ucd_path"), str) else "",
            vcd_path=data.get("vcd_path") if isinstance(data.get("vcd_path"), str) else "",
            strings_path=data.get("strings_path") if isinstance(data.get("strings_path"), str) else "",
            expected_record_count=(
                data.get("expected_record_count")
                if isinstance(data.get("expected_record_count"), int)
                else None
            ),
            detected_record_count=(
                data.get("detected_record_count")
                if isinstance(data.get("detected_record_count"), int)
                else None
            ),
            kind_counts=dict(kind_counts) if isinstance(kind_counts, dict) else {},
            record_size_stats_by_kind=(
                dict(record_size_stats_by_kind) if isinstance(record_size_stats_by_kind, dict) else {}
            ),
            package_count=data.get("package_count") if isinstance(data.get("package_count"), int) else 0,
            flight_count=data.get("flight_count") if isinstance(data.get("flight_count"), int) else 0,
            package_flight_links=tuple(
                item for item in data.get("package_flight_links", []) if isinstance(item, dict)
            ),
            packages=tuple(item for item in data.get("packages", []) if isinstance(item, dict)),
            flights=tuple(item for item in data.get("flights", []) if isinstance(item, dict)),
            records=tuple(item for item in data.get("records", []) if isinstance(item, dict)),
            note=data.get("note") if isinstance(data.get("note"), str) else "",
            head_hex=data.get("head_hex") if isinstance(data.get("head_hex"), str) else "",
            warnings=_to_warning_tuple(data.get("warnings")),
        )

    def record_by_id(self) -> dict[VUIdTuple, dict[str, Any]]:
        out: dict[VUIdTuple, dict[str, Any]] = {}
        for record in self.records:
            record_id = _to_vuid_tuple(record.get("id"))
            if record_id is None:
                continue
            out[record_id] = record
        return out

    def records_by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.records
            if isinstance(record.get("kind"), str) and record.get("kind") == kind
        ]

    def flight_by_id(self) -> dict[VUIdTuple, dict[str, Any]]:
        out: dict[VUIdTuple, dict[str, Any]] = {}
        for flight in self.flights:
            flight_id = _to_vuid_tuple(flight.get("id"))
            if flight_id is None:
                continue
            out[flight_id] = flight
        return out

    def package_by_id(self) -> dict[VUIdTuple, dict[str, Any]]:
        out: dict[VUIdTuple, dict[str, Any]] = {}
        for package in self.packages:
            package_id = _to_vuid_tuple(package.get("id"))
            if package_id is None:
                continue
            out[package_id] = package
        return out

    def squadron_by_id(self) -> dict[VUIdTuple, dict[str, Any]]:
        out: dict[VUIdTuple, dict[str, Any]] = {}
        for squadron in self.records_by_kind("squadron"):
            squadron_id = _to_vuid_tuple(squadron.get("id"))
            if squadron_id is None:
                continue
            out[squadron_id] = squadron
        return out

    def squadron_records(self) -> list[dict[str, Any]]:
        return self.records_by_kind("squadron")

    def package_numbers(self) -> list[int]:
        numbers: set[int] = set()
        for package in self.packages:
            package_number = package.get("package_number")
            if isinstance(package_number, int):
                numbers.add(package_number)
        return sorted(numbers)

    def generated_packages(self, package_numbers: list[int]) -> list[dict[str, Any]]:
        from lib.parsers.parse_uni import list_package_generated_flights

        return list_package_generated_flights(self, package_numbers)


@dataclass(frozen=True)
class ParsedTwxData:
    current_date: str = ""
    source_path: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedL16Data:
    by_flight: dict[int, dict[str, int]] = field(default_factory=dict)
    source_path: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SummaryInput:
    source_path: Path
    support_base_dir: Path | None
    container_version: int | None
    cmp: ParsedCmpData
    uni: ParsedUniData
    twx: ParsedTwxData = field(default_factory=ParsedTwxData)
    l16: ParsedL16Data = field(default_factory=ParsedL16Data)
    theater_name: str = ""


@dataclass(frozen=True)
class SummaryOutput:
    source_path: str
    support_base_dir: str
    l16_source_path: str
    current_date: str
    current_time_ms: int | None
    player: dict[str, Any]
    bullseye: dict[str, Any]
    package_count: int
    packages: list[dict[str, Any]]
    warnings: list[str]
    container_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "support_base_dir": self.support_base_dir,
            "l16_source_path": self.l16_source_path,
            "current_date": self.current_date,
            "current_time_ms": self.current_time_ms,
            "player": self.player,
            "bullseye": self.bullseye,
            "package_count": self.package_count,
            "packages": self.packages,
            "warnings": self.warnings,
        }
