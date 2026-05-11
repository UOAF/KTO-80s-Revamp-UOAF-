"""Falcon BMS support-file resolution and lightweight indexes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

from .cam_container import CamContainer


class BmsSupportError(RuntimeError):
    """Raised when the live BMS support file layout cannot be resolved."""


@dataclass(frozen=True)
class BmsSupportPaths:
    theater_dir: Path
    campaign_dir: Path
    objects_dir: Path
    strings_path: Path
    ct_path: Path
    ucd_path: Path
    vcd_path: Path


@dataclass(frozen=True)
class ClassTableEntry:
    number: int
    domain: int
    class_: int
    type_: int
    subtype: int
    specific: int
    entity_type: int
    entity_idx: int


@dataclass(frozen=True)
class UnitClassEntry:
    number: int
    ct_idx: int
    name: str
    vehicle_ct_indices: tuple[int, ...]


@dataclass(frozen=True)
class VehicleClassEntry:
    number: int
    ct_idx: int
    name: str
    callsign_idx: int
    callsign_slots: int


@dataclass(frozen=True)
class SupportData:
    paths: BmsSupportPaths
    strings_by_id: dict[int, str]
    ct_by_number: dict[int, ClassTableEntry]
    ucd_by_number: dict[int, UnitClassEntry]
    ucd_by_ct_idx: dict[int, UnitClassEntry]
    vcd_by_number: dict[int, VehicleClassEntry]
    vcd_by_ct_idx: dict[int, VehicleClassEntry]


def resolve_support_paths(theater_dir: str | Path) -> BmsSupportPaths:
    theater_path = Path(theater_dir).expanduser().resolve()
    if not theater_path.is_dir():
        raise BmsSupportError(f"theater directory does not exist: {theater_path}")

    campaign_dir = theater_path / "Campaign"
    objects_dir = theater_path / "TerrData" / "Objects"
    paths = BmsSupportPaths(
        theater_dir=theater_path,
        campaign_dir=campaign_dir,
        objects_dir=objects_dir,
        strings_path=campaign_dir / "Strings.txt",
        ct_path=objects_dir / "Falcon4_CT.xml",
        ucd_path=objects_dir / "Falcon4_UCD.xml",
        vcd_path=objects_dir / "Falcon4_VCD.xml",
    )
    _require_files(paths.strings_path, paths.ct_path, paths.ucd_path, paths.vcd_path)
    return paths


def detect_container_version(container: CamContainer) -> int | None:
    for entry in container.entries:
        if not entry.name.lower().endswith(".ver"):
            continue
        text = entry.decoded.decode("ascii", errors="replace").strip("\x00\r\n\t ")
        if text.isdigit():
            return int(text)
    return None


def load_support_data(paths: BmsSupportPaths) -> SupportData:
    ct_by_number = load_class_table(paths.ct_path)
    ucd_by_number = load_unit_classes(paths.ucd_path)
    vcd_by_number = load_vehicle_classes(paths.vcd_path)
    return SupportData(
        paths=paths,
        strings_by_id=load_strings_by_id(paths.strings_path),
        ct_by_number=ct_by_number,
        ucd_by_number=ucd_by_number,
        ucd_by_ct_idx={entry.ct_idx: entry for entry in ucd_by_number.values()},
        vcd_by_number=vcd_by_number,
        vcd_by_ct_idx={entry.ct_idx: entry for entry in vcd_by_number.values()},
    )


def load_class_table(path: str | Path) -> dict[int, ClassTableEntry]:
    root = _xml_root(path)
    entries: dict[int, ClassTableEntry] = {}
    for node in root.findall("CT"):
        number_text = node.attrib.get("Num")
        if number_text is None:
            continue
        number = _safe_int(number_text, default=-1)
        if number < 0:
            continue
        entries[number] = ClassTableEntry(
            number=number,
            domain=_safe_int(node.findtext("Domain")),
            class_=_safe_int(node.findtext("Class")),
            type_=_safe_int(node.findtext("Type")),
            subtype=_safe_int(node.findtext("SubType")),
            specific=_safe_int(node.findtext("Specific")),
            entity_type=_safe_int(node.findtext("EntityType")),
            entity_idx=_safe_int(node.findtext("EntityIdx"), default=-1),
        )
    return entries


def load_unit_classes(path: str | Path) -> dict[int, UnitClassEntry]:
    root = _xml_root(path)
    entries: dict[int, UnitClassEntry] = {}
    for node in root.findall("UCD"):
        number_text = node.attrib.get("Num")
        if number_text is None:
            continue
        number = _safe_int(number_text, default=-1)
        if number < 0:
            continue
        vehicle_ct_indices = tuple(
            value
            for index in range(16)
            if (value := _safe_int(node.findtext(f"VehicleCtIdx_{index}"), default=-1)) >= 0
        )
        entries[number] = UnitClassEntry(
            number=number,
            ct_idx=_safe_int(node.findtext("CtIdx"), default=-1),
            name=(node.findtext("Name") or "").strip(),
            vehicle_ct_indices=vehicle_ct_indices,
        )
    return entries


def load_vehicle_classes(path: str | Path) -> dict[int, VehicleClassEntry]:
    root = _xml_root(path)
    entries: dict[int, VehicleClassEntry] = {}
    for node in root.findall("VCD"):
        number_text = node.attrib.get("Num")
        if number_text is None:
            continue
        number = _safe_int(number_text, default=-1)
        if number < 0:
            continue
        entries[number] = VehicleClassEntry(
            number=number,
            ct_idx=_safe_int(node.findtext("CtIdx"), default=-1),
            name=(node.findtext("Name") or "").strip(),
            callsign_idx=_safe_int(node.findtext("CallsignIdx")),
            callsign_slots=_safe_int(node.findtext("CallsignSlots")),
        )
    return entries


def load_strings_by_id(path: str | Path) -> dict[int, str]:
    strings: dict[int, str] = {}
    for line in Path(path).read_text(encoding="latin-1", errors="replace").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            strings[int(parts[0])] = parts[1].strip()
    return strings


def format_campaign_time_z(value_ms: int | None) -> str | None:
    if not isinstance(value_ms, int) or value_ms < 0:
        return None
    total_seconds, milliseconds = divmod(value_ms, 1000)
    hours = (total_seconds // 3600) % 24
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if milliseconds:
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}z"
    return f"{hours:02}:{minutes:02}:{seconds:02}z"


def _require_files(*paths: Path) -> None:
    missing = [path for path in paths if not path.is_file()]
    if missing:
        message = ", ".join(str(path) for path in missing)
        raise BmsSupportError(f"missing required support files: {message}")


def _xml_root(path: str | Path) -> ET.Element:
    return ET.parse(Path(path)).getroot()


def _safe_int(text: str | None, *, default: int = 0) -> int:
    if text is None:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default
