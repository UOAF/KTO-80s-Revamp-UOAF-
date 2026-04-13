from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger("html_brief_log")


def campaign_dirs(
    *,
    bms_base_dir: str | Path | None,
    theater_target_folder: str | Path | None = None,
) -> list[Path]:
    dirs: list[Path] = []
    if not bms_base_dir:
        return dirs
    base = Path(bms_base_dir).expanduser()
    data_dir = base / "Data"
    default_campaign_dir = data_dir / "Campaign"

    if theater_target_folder:
        target = Path(theater_target_folder).expanduser()
        if not target.is_absolute():
            target = (base / target).resolve()
        try:
            resolved = target.resolve()
        except Exception:
            resolved = target
        for idx, part in enumerate(resolved.parts):
            if part.lower().startswith("add-on"):
                addon_root = Path(*resolved.parts[: idx + 1])
                dirs.append(addon_root / "Campaign")
                break
    if not dirs:
        dirs.append(default_campaign_dir)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for item in dirs:
        try:
            resolved = item.resolve()
        except Exception:
            resolved = item
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            deduped.append(resolved)
    return deduped


def infer_bms_base_dir_from_campaign_dir(campaign_dir: str | Path | None) -> Path | None:
    if not campaign_dir:
        return None

    try:
        resolved = Path(campaign_dir).expanduser().resolve()
    except Exception:
        resolved = Path(campaign_dir).expanduser()

    current: Path | None = resolved
    while current is not None:
        if current.name.lower() == "data":
            base_dir = current.parent
            try:
                return base_dir.resolve()
            except Exception:
                return base_dir
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def infer_support_base_dir(
    bms_base_dir: str | Path | None,
    theater_target_folder: str | Path | None,
    theater_name: str | None = None,
) -> Path | None:
    def normalize_token(text: str) -> str:
        return "".join(ch for ch in text.lower() if ch.isalnum())

    def has_ct(objects_dir: Path) -> bool:
        if not objects_dir.is_dir():
            return False
        try:
            for child in objects_dir.iterdir():
                if child.is_file() and child.name.lower() == "falcon4_ct.xml":
                    return True
        except Exception:
            return False
        return False

    def infer_objects_from_target(target_path: Path) -> Path | None:
        resolved = target_path.resolve()
        if resolved.name.lower() == "koreaobj":
            return resolved.parent
        parts_l = [part.lower() for part in resolved.parts]
        if "koreaobj" in parts_l:
            idx = parts_l.index("koreaobj")
            if idx > 0:
                return Path(*resolved.parts[:idx])
        if resolved.name.lower() == "objects":
            return resolved
        return None

    def infer_addon_token_from_target(target_path: Path) -> str | None:
        resolved = target_path.resolve()
        for part in resolved.parts:
            part_l = part.lower()
            if part_l.startswith("add-on"):
                token = normalize_token(part_l.replace("add-on", "", 1))
                if token:
                    return token
        return None

    if theater_target_folder:
        target_path = Path(theater_target_folder).expanduser()
        objects_from_target = infer_objects_from_target(target_path)
        if objects_from_target is not None and has_ct(objects_from_target):
            logger.debug("Support base dir resolved from theater target folder: %s", objects_from_target)
            return objects_from_target

    if bms_base_dir:
        base = Path(bms_base_dir).expanduser()
        data_dir = base / "Data"

        add_on_objects: list[tuple[Path, str, str]] = []
        if data_dir.is_dir():
            try:
                for add_on_dir in data_dir.iterdir():
                    if not add_on_dir.is_dir():
                        continue
                    add_on_name = add_on_dir.name
                    if not add_on_name.lower().startswith("add-on"):
                        continue
                    objects_dir = None
                    for candidate in (
                        add_on_dir / "TerrData" / "Objects",
                        add_on_dir / "Terrdata" / "Objects",
                        add_on_dir / "Terrdata" / "objects",
                    ):
                        if has_ct(candidate):
                            objects_dir = candidate
                            break
                    if objects_dir is None:
                        continue
                    add_on_objects.append(
                        (
                            objects_dir,
                            add_on_name.lower(),
                            normalize_token(add_on_name.replace("add-on", "", 1)),
                        )
                    )
            except Exception:
                pass

        if theater_target_folder and add_on_objects:
            target_path = Path(theater_target_folder).expanduser()
            target_addon_token = infer_addon_token_from_target(target_path)
            if target_addon_token:
                for objects_dir, add_on_name, add_on_token in add_on_objects:
                    if (
                        target_addon_token == add_on_token
                        or target_addon_token in add_on_token
                        or add_on_token in target_addon_token
                        or add_on_name in str(target_path).lower()
                    ):
                        logger.debug("Support base dir resolved from add-on target match: %s", objects_dir)
                        return objects_dir

        if add_on_objects and theater_name:
            theater_l = theater_name.lower()
            theater_token = normalize_token(theater_name)
            for objects_dir, add_on_name, add_on_token in add_on_objects:
                if (
                    theater_l in str(objects_dir).lower()
                    or theater_l in add_on_name
                    or (theater_token and (theater_token in add_on_token or add_on_token in theater_token))
                ):
                    logger.debug("Support base dir resolved from theater name match: %s", objects_dir)
                    return objects_dir

        for objects_dir in (
            base / "Data" / "TerrData" / "Objects",
            base / "Data" / "Terrdata" / "Objects",
        ):
            if has_ct(objects_dir):
                logger.debug("Support base dir resolved to default KTO objects: %s", objects_dir)
                return objects_dir

        if add_on_objects:
            logger.debug("Support base dir falling back to first add-on objects: %s", add_on_objects[0][0])
            return add_on_objects[0][0]

    if theater_target_folder:
        target_path = Path(theater_target_folder).expanduser()
        objects_from_target = infer_objects_from_target(target_path)
        if objects_from_target is not None and objects_from_target.is_dir():
            logger.debug(
                "Support base dir fallback to inferred objects dir without CT validation: %s",
                objects_from_target,
            )
            return objects_from_target

    if bms_base_dir:
        fallback = Path(bms_base_dir).expanduser().resolve()
        logger.debug("Support base dir fallback to BMS base dir: %s", fallback)
        return fallback

    logger.debug("Support base dir could not be inferred.")
    return None


__all__ = [
    "campaign_dirs",
    "infer_bms_base_dir_from_campaign_dir",
    "infer_support_base_dir",
]
