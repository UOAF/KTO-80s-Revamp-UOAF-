from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re


logger = logging.getLogger("html_brief_log")


@dataclass(frozen=True)
class BriefingSupportEntry:
    callsign: str
    support_type: str
    comment: str
    aircraft_count: int | None = None
    aircraft_type: str = ""

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "callsign": self.callsign,
            "type": self.support_type,
            "comment": self.comment,
            "aircraft_count": self.aircraft_count,
            "aircraft_type": self.aircraft_type,
        }


@dataclass(frozen=True)
class BriefingCommEntry:
    agency: str
    callsign: str
    uhf: str
    vhf: str
    notes: str

    def to_dict(self) -> dict[str, str]:
        return {
            "agency": self.agency,
            "callsign": self.callsign,
            "uhf": self.uhf,
            "vhf": self.vhf,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class BriefingWeatherData:
    sit: tuple[str, ...] = ()
    wind: tuple[str, ...] = ()
    vis: tuple[str, ...] = ()
    temp: tuple[str, ...] = ()
    cloud: tuple[str, ...] = ()
    con: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "sit": list(self.sit),
            "wind": list(self.wind),
            "vis": list(self.vis),
            "temp": list(self.temp),
            "cloud": list(self.cloud),
            "con": list(self.con),
        }


@dataclass(frozen=True)
class ParsedBriefingTxtData:
    c2_callsign: str = ""
    c2_aircraft: str = ""
    c2_ship_count: int | None = None
    tod: str = ""
    sunrise: str = ""
    sunset: str = ""
    weather: BriefingWeatherData = BriefingWeatherData()
    comm: tuple[BriefingCommEntry, ...] = ()
    support: tuple[BriefingSupportEntry, ...] = ()
    source_path: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "c2_callsign": self.c2_callsign,
            "c2_aircraft": self.c2_aircraft,
            "c2_ship_count": self.c2_ship_count,
            "tod": self.tod,
            "sunrise": self.sunrise,
            "sunset": self.sunset,
            "weather": self.weather.to_dict(),
            "comm": [entry.to_dict() for entry in self.comm],
            "support": [entry.to_dict() for entry in self.support],
            "source_path": str(self.source_path) if self.source_path is not None else "",
            "warnings": list(self.warnings),
        }


def parse_briefing_txt_lines(lines: list[str]) -> ParsedBriefingTxtData:
    warnings: list[str] = []
    comm_entries: list[BriefingCommEntry] = []
    support_entries: list[BriefingSupportEntry] = []
    weather = BriefingWeatherData()
    sunrise = ""
    sunset = ""
    tod = ""

    try:
        start = next(
            index for index, line in enumerate(lines) if line.strip("\t \n) ").startswith("Comm Ladder")
        )
        end = next(
            index for index, line in enumerate(lines) if line.strip("\t \n) ").startswith("Iff")
        )
        filtered_entries = _briefing_section_entries(lines, start, end)
        for line in filtered_entries:
            parts = line.strip("\t").split("\t")
            comm_entries.append(
                BriefingCommEntry(
                    agency=parts[0].strip().strip(":") if len(parts) >= 1 else "",
                    callsign=parts[1].strip() if len(parts) >= 2 else "",
                    uhf=parts[2].strip() if len(parts) >= 3 else "",
                    vhf=parts[3].strip() if len(parts) >= 4 else "",
                    notes=parts[4].strip() if len(parts) >= 5 else "",
                )
            )
    except StopIteration:
        pass
    except Exception as exc:
        warnings.append(f"Error reading briefing comm ladder: {exc}")

    try:
        start = next(
            index for index, line in enumerate(lines) if line.strip("\t \n) ").startswith("Support")
        )
        end = next(
            index
            for index, line in enumerate(lines)
            if line.strip("\t \n) ").startswith("Rules of Engagement")
        )
        filtered_entries = _briefing_section_entries(lines, start, end)
        for line in filtered_entries:
            support_entries.append(_parse_support_entry(line))
    except StopIteration:
        pass
    except Exception as exc:
        warnings.append(f"Error reading briefing support section: {exc}")

    try:
        weather = _parse_weather(lines)
    except Exception as exc:
        warnings.append(f"Error reading briefing weather section: {exc}")

    try:
        sunrise_line = _find_briefing_value(lines, "Sunrise")
        sunrise = _format_briefing_event_time(sunrise_line)
    except Exception:
        sunrise = ""

    try:
        sunset_line = _find_briefing_value(lines, "Sunset")
        sunset = _format_briefing_event_time(sunset_line)
    except Exception:
        sunset = ""

    try:
        tod_line = (
            _find_briefing_value(lines, "Time of Day")
            or _find_briefing_value(lines, "Time on Target")
            or _find_briefing_value(lines, "Time on Station")
        )
        tod = _format_briefing_event_time(tod_line, fallback_local_offset_minutes=_infer_local_offset_minutes(lines))
    except Exception:
        tod = ""

    c2_callsign = ""
    for entry in comm_entries:
        if _normalize_text(entry.agency) == "TACTICAL" and entry.callsign.strip():
            c2_callsign = entry.callsign.strip()
            break

    if not c2_callsign:
        for entry in support_entries:
            support_tokens = _normalize_text(" ".join((entry.support_type, entry.comment)))
            if "AIRC2" in support_tokens:
                c2_callsign = entry.callsign
                break

    c2_aircraft = ""
    c2_ship_count: int | None = None
    if c2_callsign:
        wanted_callsign = _normalize_text(c2_callsign)
        for entry in support_entries:
            if _normalize_text(entry.callsign) != wanted_callsign:
                continue
            c2_aircraft = entry.aircraft_type
            c2_ship_count = entry.aircraft_count
            break

    return ParsedBriefingTxtData(
        c2_callsign=c2_callsign,
        c2_aircraft=c2_aircraft,
        c2_ship_count=c2_ship_count,
        tod=tod,
        sunrise=sunrise,
        sunset=sunset,
        weather=weather,
        comm=tuple(comm_entries),
        support=tuple(support_entries),
        warnings=tuple(warnings),
    )


def load_parsed_briefing_for_base_dir(
    bms_base_dir: str | Path | None,
) -> ParsedBriefingTxtData:
    if not bms_base_dir:
        return ParsedBriefingTxtData()

    briefing_path = Path(bms_base_dir).expanduser() / "User" / "Briefings" / "briefing.txt"
    if not briefing_path.is_file():
        return ParsedBriefingTxtData(
            source_path=briefing_path,
            warnings=(f"Briefing file not found: {briefing_path}",),
        )

    try:
        lines = briefing_path.read_text(encoding="latin1").splitlines()
    except Exception as exc:
        logger.warning("Failed to read briefing file %s: %s", briefing_path, exc)
        return ParsedBriefingTxtData(
            source_path=briefing_path,
            warnings=(f"Failed to read briefing file {briefing_path}: {exc}",),
        )

    parsed = parse_briefing_txt_lines(lines)
    return ParsedBriefingTxtData(
        c2_callsign=parsed.c2_callsign,
        c2_aircraft=parsed.c2_aircraft,
        c2_ship_count=parsed.c2_ship_count,
        tod=parsed.tod,
        sunrise=parsed.sunrise,
        sunset=parsed.sunset,
        weather=parsed.weather,
        comm=parsed.comm,
        support=parsed.support,
        source_path=briefing_path,
        warnings=parsed.warnings,
    )


def _normalize_text(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _briefing_section_entries(lines: list[str], start: int, end: int) -> list[str]:
    raw_entries = [line.strip(" \n") for line in lines[start + 4 : end]]
    return [line for line in raw_entries if line != ""]


_SUPPORT_LINE_RE = re.compile(
    r"^\s*(?P<callsign>[^(\t]+?)\s*\((?P<support_type>[^)]+)\)\s*:\s*"
    r"(?P<aircraft_count>\d+)\s+(?P<aircraft_type>[A-Za-z0-9+./_-]+)\s*(?P<comment>.*)$"
)


def _parse_support_entry(line: str) -> BriefingSupportEntry:
    match = _SUPPORT_LINE_RE.match(line)
    if match is not None:
        aircraft_count_text = match.group("aircraft_count").strip()
        return BriefingSupportEntry(
            callsign=match.group("callsign").strip(),
            support_type=match.group("support_type").strip(),
            comment=match.group("comment").strip(),
            aircraft_count=int(aircraft_count_text) if aircraft_count_text.isdigit() else None,
            aircraft_type=match.group("aircraft_type").strip(),
        )

    parts = line.strip("\t").split("\t")
    return BriefingSupportEntry(
        callsign=parts[0].strip() if len(parts) >= 1 else "",
        support_type=parts[1].strip() if len(parts) >= 2 else "",
        comment=parts[2].strip() if len(parts) >= 3 else "",
    )


def _parse_weather(lines: list[str]) -> BriefingWeatherData:
    start = next(index for index, line in enumerate(lines) if line.strip("\t \n) ").startswith("Weather"))
    return BriefingWeatherData(
        sit=_weather_triplet(lines, start + 4),
        wind=_weather_triplet(lines, start + 5),
        vis=_weather_triplet(lines, start + 6),
        temp=_weather_triplet(lines, start + 7),
        cloud=_weather_triplet(lines, start + 8),
        con=_weather_triplet(lines, start + 9),
    )


def _weather_triplet(lines: list[str], index: int) -> tuple[str, str, str]:
    parts = lines[index].strip("\t \n").split("\t")[1:4]
    values = [part.strip() for part in parts]
    while len(values) < 3:
        values.append("")
    return tuple(values[:3])


def _find_briefing_value(lines: list[str], prefix: str) -> str:
    line = next(
        (line for line in lines if line.strip("\t \n) ").startswith(prefix)),
        "",
    )
    if not line:
        return ""
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


def _infer_local_offset_minutes(lines: list[str]) -> int | None:
    for key in ("Sunrise", "Sunset"):
        formatted = _find_briefing_value(lines, key)
        offset = _event_local_offset_minutes(formatted)
        if offset is not None:
            return offset
    return None


def _event_local_offset_minutes(value: str) -> int | None:
    match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?z\s*\((\d{1,2}):(\d{2})(?::\d{2})?l\)", value, re.IGNORECASE)
    if match is None:
        return None
    z_minutes = int(match.group(1)) * 60 + int(match.group(2))
    local_minutes = int(match.group(3)) * 60 + int(match.group(4))
    diff = local_minutes - z_minutes
    while diff <= -720:
        diff += 1440
    while diff > 720:
        diff -= 1440
    return diff


def _format_briefing_event_time(value: str, fallback_local_offset_minutes: int | None = None) -> str:
    if not value:
        return ""
    pair = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?z\s*\((\d{1,2}):(\d{2})(?::\d{2})?l\)", value, re.IGNORECASE)
    if pair is not None:
        return (
            f"{int(pair.group(3)):02d}{int(pair.group(4)):02d}L / "
            f"{int(pair.group(1)):02d}{int(pair.group(2)):02d}Z"
        )
    z_only = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?z", value, re.IGNORECASE)
    if z_only is not None:
        hour = int(z_only.group(1))
        minute = int(z_only.group(2))
        z_text = f"{hour:02d}{minute:02d}Z"
        if fallback_local_offset_minutes is None:
            return z_text
        local_total = (hour * 60 + minute + fallback_local_offset_minutes) % 1440
        return f"{local_total // 60:02d}{local_total % 60:02d}L / {z_text}"
    return value.strip()
