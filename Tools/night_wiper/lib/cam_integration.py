from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lib.campaign_paths import infer_support_base_dir
from lib.cam.types import ParsedCmpData, ParsedUniData, SummaryInput
from lib.parsers.parse_briefing_txt import load_parsed_briefing_for_base_dir
from lib.parsers.parse_cmp import parse_cmp
from lib.parsers.parse_l16 import load_parsed_l16_for_save
from lib.parsers.parse_summary import build_summary_error_output, build_summary_output
from lib.parsers.parse_twx import load_parsed_twx_for_cam_path, load_parsed_twx_for_save
from lib.parsers.parse_uni import parse_uni

logger = logging.getLogger("html_brief_log")


class CamIntegrationError(RuntimeError):
    """Raised when CAM integration cannot parse expected data."""


def _resolve_cam_modules() -> Any:
    try:
        from lib.cam.cam_content import parse_cam_file
    except Exception as exc:
        logger.exception("Failed to import CAM parser modules")
        raise CamIntegrationError(f"Failed to import CAM parser modules: {exc}") from exc

    logger.debug("Resolved CAM modules via static package imports")
    return parse_cam_file

def extract_cam_brief_data(
    cam_file_path: str | Path,
    *,
    bms_base_dir: str | Path | None = None,
    theater_target_folder: str | Path | None = None,
    theater_name: str | None = None,
    save_stem: str | None = None,
) -> dict[str, Any]:
    logger.debug(
        "Extract CAM brief data start: cam=%s base=%s theater_target=%s theater=%s save_stem=%s",
        cam_file_path,
        bms_base_dir,
        theater_target_folder,
        theater_name,
        save_stem,
    )
    parse_cam_file = _resolve_cam_modules()

    source_path = Path(cam_file_path).resolve()
    support_base_dir = infer_support_base_dir(
        bms_base_dir,
        theater_target_folder,
        theater_name=theater_name,
    )

    parsed_cam = parse_cam_file(
        source_path,
        bms_base_dir=support_base_dir,
        parse_entries=False,
        best_effort=True,
    )
    cmp_entry = parsed_cam.get_entry_by_ext(".cmp")
    uni_entry = parsed_cam.get_entry_by_ext(".uni")

    twx_data = load_parsed_twx_for_cam_path(source_path)
    if not twx_data.current_date:
        twx_data = load_parsed_twx_for_save(
            bms_base_dir=bms_base_dir,
            theater_target_folder=theater_target_folder,
            save_stem=save_stem or source_path.stem,
        )
    l16_data = load_parsed_l16_for_save(
        bms_base_dir=bms_base_dir,
        theater_target_folder=theater_target_folder,
        save_stem=save_stem or source_path.stem,
    )
    briefing_data = load_parsed_briefing_for_base_dir(bms_base_dir)

    summary_input = SummaryInput(
        source_path=source_path,
        support_base_dir=support_base_dir,
        container_version=parsed_cam.container_version,
        cmp=(
            parse_cmp(
                cmp_entry.data,
                container_version=parsed_cam.container_version,
                support_base_dir=support_base_dir,
                decode_metadata=cmp_entry.decode_metadata,
            )
            if cmp_entry is not None
            else ParsedCmpData.from_dict(None)
        ),
        uni=(
            parse_uni(
                uni_entry.data,
                container_version=parsed_cam.container_version,
                support_base_dir=support_base_dir,
                decode_metadata=uni_entry.decode_metadata,
            )
            if uni_entry is not None
            else ParsedUniData.from_dict(None)
        ),
        twx=twx_data,
        l16=l16_data,
        theater_name=theater_name or "",
    )

    try:
        summary_output = build_summary_output(summary_input)
        logger.debug(
            "Extract CAM brief data done: package_count=%d warnings=%d",
            summary_output.package_count,
            len(summary_output.warnings),
        )
        output = summary_output.to_dict()
        output["briefing"] = briefing_data.to_dict()
        output["warnings"].extend(list(briefing_data.warnings))
        return output
    except Exception as exc:
        logger.exception("CAM summary shaping failed for %s", source_path)
        fallback = build_summary_error_output(
            summary_input,
            warnings=[f"Summary shaping failed: {exc}"],
        )
        output = fallback.to_dict()
        output["briefing"] = briefing_data.to_dict()
        output["warnings"].extend(list(briefing_data.warnings))
        return output
