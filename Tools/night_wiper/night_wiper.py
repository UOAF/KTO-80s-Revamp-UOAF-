#!/usr/bin/env python3
"""Night Wiper: remove day packages and toggle squadron human control."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import sys

from lib.cam_container import CamContainer
from lib.support_files import detect_container_version, load_support_data, resolve_support_paths
from lib.uni_parser import UnitRecord, VuId, encode_uni_records, parse_uni_records
from lib.uni_wrappers import FlightUnit, PackageUnit, SquadronUnit, wrap_unit


CONFIG_FILE = Path(__file__).with_name("night_wiper.ini")
NIGHT_AIRFRAMES = "night airframes"
PROTECTED_SQUADRONS = "protected squadrons"


class NightWiperError(RuntimeError):
    """Raised when Night Wiper cannot safely apply the requested operation."""


@dataclass(frozen=True)
class SquadronSelector:
    kind: str
    value: str

    def matches(self, squadron: SquadronUnit) -> bool:
        if self.kind == "unit_id":
            return _format_vuid(squadron.unit_id) == self.value
        if self.kind == "camp_id":
            return str(squadron.get("camp_id")) == self.value
        if self.kind == "name_id":
            return str(squadron.get("name_id")) == self.value
        if self.kind == "aircraft":
            aircraft = squadron.aircraft_name or ""
            return self.value.casefold() in aircraft.casefold()
        raise NightWiperError(f"unsupported selector kind {self.kind!r}")


@dataclass(frozen=True)
class NightWiperConfig:
    night_airframes: tuple[str, ...]
    protected_squadrons: tuple[SquadronSelector, ...]


@dataclass(frozen=True)
class RunSummary:
    save_path: Path
    backup_path: Path | None
    packages_deleted: int = 0
    flights_deleted: int = 0
    squadrons_set_human: int = 0
    squadrons_set_hq: int = 0
    records_before: int = 0
    records_after: int = 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        config = load_config(CONFIG_FILE)
        summary = run(
            save_path=args.save,
            theater_dir=args.theater,
            config=config,
            lights_off=args.lights_off,
        )
    except NightWiperError as exc:
        print(f"night_wiper: {exc}", file=sys.stderr)
        return 2

    _print_summary(summary)
    return 0


def run(
    *,
    save_path: Path,
    theater_dir: Path,
    config: NightWiperConfig,
    lights_off: bool,
) -> RunSummary:
    save_path = save_path.expanduser().resolve()
    if not save_path.is_file():
        raise NightWiperError(f"save file does not exist: {save_path}")

    support = load_support_data(resolve_support_paths(theater_dir))
    container = CamContainer.from_path(save_path)
    uni_entry = _single_uni_entry(container)
    records = list(
        parse_uni_records(
            uni_entry,
            container_version=detect_container_version(container),
            support=support,
        )
    )

    if lights_off:
        packages_deleted, flights_deleted = _apply_lights_off(records, support, config)
        squadrons_set_human = _set_non_night_squadrons_human(records, support, config)
        squadrons_set_hq = 0
    else:
        packages_deleted = 0
        flights_deleted = 0
        squadrons_set_human = 0
        squadrons_set_hq = _clear_non_exempt_squadrons_human(records, support, config)

    changed = packages_deleted or flights_deleted or squadrons_set_human or squadrons_set_hq
    if not changed:
        return RunSummary(
            save_path=save_path,
            backup_path=None,
            records_before=len(records),
            records_after=len(records),
        )

    uni_entry.metadata["record_count"] = len(records)
    container.set_entry_decoded(uni_entry.name, encode_uni_records(records))
    backup_path = _backup_save(save_path)
    save_path.write_bytes(container.rebuild_bytes())

    return RunSummary(
        save_path=save_path,
        backup_path=backup_path,
        packages_deleted=packages_deleted,
        flights_deleted=flights_deleted,
        squadrons_set_human=squadrons_set_human,
        squadrons_set_hq=squadrons_set_hq,
        records_before=int(uni_entry.metadata["record_count"]) + packages_deleted + flights_deleted,
        records_after=len(records),
    )


def load_config(path: Path) -> NightWiperConfig:
    sections = _read_line_sections(path)
    night_airframes = tuple(sections.get(NIGHT_AIRFRAMES, ()))
    if not night_airframes:
        raise NightWiperError(f"{path}: [{NIGHT_AIRFRAMES}] must contain at least one value")

    return NightWiperConfig(
        night_airframes=night_airframes,
        protected_squadrons=tuple(
            _parse_squadron_selector(value)
            for value in sections.get(PROTECTED_SQUADRONS, ())
        ),
    )


def _apply_lights_off(
    records: list[UnitRecord],
    support,
    config: NightWiperConfig,
) -> tuple[int, int]:
    records_by_id = {record.unit_id: record for record in records}
    delete_ids: set[VuId] = set()

    for record in records:
        if record.kind != "package":
            continue
        package = wrap_unit(record, support)
        if not isinstance(package, PackageUnit):
            continue
        element_records = [records_by_id.get(unit_id) for unit_id in package.element_ids]
        if not element_records or any(item is None for item in element_records):
            continue
        if any(item.kind != "flight" for item in element_records if item is not None):
            continue
        protected_flights = [
            _is_protected_flight(item, records_by_id, support, config)
            for item in element_records
        ]
        if any(protected_flights):
            continue

        delete_ids.add(record.unit_id)
        delete_ids.update(item.unit_id for item in element_records if item is not None)

    if not delete_ids:
        return 0, 0

    packages_deleted = sum(
        1 for record in records if record.unit_id in delete_ids and record.kind == "package"
    )
    flights_deleted = sum(
        1 for record in records if record.unit_id in delete_ids and record.kind == "flight"
    )
    records[:] = [record for record in records if record.unit_id not in delete_ids]
    return packages_deleted, flights_deleted


def _set_non_night_squadrons_human(
    records: list[UnitRecord],
    support,
    config: NightWiperConfig,
) -> int:
    changed = 0
    for squadron in _squadrons(records, support):
        if _is_exempt_squadron(squadron, config):
            continue
        if _is_night_aircraft(squadron.aircraft_name, config):
            continue
        if not squadron.human_controlled:
            squadron.human_controlled = True
            changed += 1
    return changed


def _clear_non_exempt_squadrons_human(
    records: list[UnitRecord],
    support,
    config: NightWiperConfig,
) -> int:
    changed = 0
    for squadron in _squadrons(records, support):
        if _is_exempt_squadron(squadron, config):
            continue
        if squadron.human_controlled:
            squadron.human_controlled = False
            changed += 1
    return changed


def _is_protected_flight(
    flight_record: UnitRecord,
    records_by_id: dict[VuId, UnitRecord],
    support,
    config: NightWiperConfig,
) -> bool:
    flight = wrap_unit(flight_record, support)
    if not isinstance(flight, FlightUnit):
        return True
##    if _is_night_aircraft(flight.aircraft_name, config):
##        return True
    squadron_record = records_by_id.get(flight.get("squadron_id"))
    if squadron_record is None or squadron_record.kind != "squadron":
        return True
    squadron = wrap_unit(squadron_record, support)
    if not isinstance(squadron, SquadronUnit):
        return True
    return _is_exempt_squadron(squadron, config)


def _squadrons(records: list[UnitRecord], support) -> list[SquadronUnit]:
    squadrons: list[SquadronUnit] = []
    for record in records:
        if record.kind != "squadron":
            continue
        unit = wrap_unit(record, support)
        if isinstance(unit, SquadronUnit):
            squadrons.append(unit)
    return squadrons


def _is_night_aircraft(aircraft_name: str | None, config: NightWiperConfig) -> bool:
    if not aircraft_name:
        return False
    name = aircraft_name.casefold()
    return any(token.casefold() in name for token in config.night_airframes)


def _is_exempt_squadron(squadron: SquadronUnit, config: NightWiperConfig) -> bool:
    return any(selector.matches(squadron) for selector in config.protected_squadrons)


def _read_line_sections(path: Path) -> dict[str, tuple[str, ...]]:
    if not path.is_file():
        raise NightWiperError(f"config file does not exist: {path}")

    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = _normalize_section(line[1:-1])
            sections.setdefault(current_section, [])
            continue
        if current_section is None:
            raise NightWiperError(f"{path}:{line_number}: value appears before a section")
        sections[current_section].append(line)

    return {section: tuple(values) for section, values in sections.items()}


def _parse_squadron_selector(value: str) -> SquadronSelector:
    if ":" not in value:
        raise NightWiperError(
            "protected squadron selectors must use kind:value, for example camp_id:1715"
        )
    kind, text = value.split(":", 1)
    kind = kind.strip().casefold().replace("-", "_")
    text = text.strip()
    if kind not in {"unit_id", "camp_id", "name_id", "aircraft"}:
        raise NightWiperError(f"unsupported human squadron selector: {value!r}")
    if not text:
        raise NightWiperError(f"empty human squadron selector: {value!r}")
    if kind == "unit_id":
        text = _format_vuid(_parse_vuid(text))
    elif kind in {"camp_id", "name_id"} and not text.isdigit():
        raise NightWiperError(f"{kind} selector must be numeric: {value!r}")
    return SquadronSelector(kind=kind, value=text)


def _parse_vuid(value: str) -> VuId:
    parts = [part.strip() for part in value.replace("/", ",").split(",")]
    if len(parts) == 1:
        parts.append("0")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise NightWiperError(f"unit_id selector must be num or num,creator: {value!r}")
    return int(parts[0]), int(parts[1])


def _format_vuid(value: VuId) -> str:
    return f"{int(value[0])},{int(value[1])}"


def _normalize_section(value: str) -> str:
    return " ".join(value.strip().casefold().replace("_", " ").replace("-", " ").split())


def _single_uni_entry(container: CamContainer):
    entries = [entry for entry in container.entries if entry.name.lower().endswith(".uni")]
    if len(entries) != 1:
        raise NightWiperError(f"expected one .uni entry, found {len(entries)}")
    return entries[0]


def _backup_save(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete non-night packages and toggle squadron human control.",
        epilog=(
            "night_wiper.ini uses [night airframes] and [protected squadrons] "
            "sections. Protected squadron selectors are unit_id:8690, "
            "camp_id:6951, name_id:13, or aircraft:F-16CM-52."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--lights-off", action="store_true", help="delete day packages")
    mode.add_argument("--lights-on", action="store_true", help="clear human control")
    parser.add_argument(
        "--theater",
        type=Path,
        required=True,
        help="theater Data directory, for example /path/to/Falcon BMS 4.38/Data",
    )
    parser.add_argument("save", type=Path, help=".cam save to edit in-place")
    return parser.parse_args(argv)


def _print_summary(summary: RunSummary) -> None:
    if summary.backup_path is None:
        print(f"No changes needed: {summary.save_path}")
        return
    print(f"Edited: {summary.save_path}")
    print(f"Backup: {summary.backup_path}")
    print(f"Records: {summary.records_before} -> {summary.records_after}")
    if summary.packages_deleted or summary.flights_deleted:
        print(
            f"Deleted: {summary.packages_deleted} packages, "
            f"{summary.flights_deleted} flights"
        )
    if summary.squadrons_set_human:
        print(f"Human controlled: set {summary.squadrons_set_human} squadrons")
    if summary.squadrons_set_hq:
        print(f"Human controlled: cleared {summary.squadrons_set_hq} squadrons")


if __name__ == "__main__":
    raise SystemExit(main())
