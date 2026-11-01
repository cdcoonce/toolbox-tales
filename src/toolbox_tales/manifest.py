"""Episode manifest and cast sheet validation."""

import math
from pathlib import Path
from typing import Any

import yaml

MIN_PANELS = 4
MAX_PANELS = 6
OVERLAY_MAX = 60
PANEL_PADDING_S = 0.5
SHOT_ALLOWANCE_S = 5.0
RUNTIME_MIN_S = 45.0
RUNTIME_MAX_S = 60.0


def _cast_names(cast_sheet: Any) -> set[str]:
    chars = cast_sheet.get("characters") if isinstance(cast_sheet, dict) else None
    if not isinstance(chars, list):
        return set()
    return {c["name"] for c in chars if isinstance(c, dict) and isinstance(c.get("name"), str)}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_arrival_shot(shot: Any, panel_count: int, names: set[str]) -> list[str]:
    if shot is None:
        return ["arrival-shot: arrival_shot is missing"]
    if not isinstance(shot, dict):
        return ["arrival-shot: arrival_shot must be a single mapping, not a list or scalar"]
    errors = []
    after = shot.get("after_panel")
    if not isinstance(after, int) or isinstance(after, bool) or not 1 <= after <= panel_count:
        errors.append(f"arrival-shot: after_panel {after!r} must be an int from 1 to {panel_count}")
    ref = shot.get("reference")
    if not isinstance(ref, str) or ref not in names:
        errors.append(f"unknown-cast: arrival_shot reference {ref!r} is not in the cast sheet")
    return errors


def _check_runtime(panels: list[Any], panel_count: int) -> list[str]:
    durations = []
    for panel in panels:
        lines = panel.get("lines") if isinstance(panel, dict) else None
        if not isinstance(lines, list):
            return []
        for line in lines:
            if not isinstance(line, dict) or "duration" not in line:
                return []
            durations.append(line["duration"])
    if not durations:
        return []
    bad = [d for d in durations if not _is_number(d) or not math.isfinite(d)]
    if bad:
        return [f"runtime: duration {bad[0]!r} is not a finite number"]
    total = sum(durations) + PANEL_PADDING_S * panel_count + SHOT_ALLOWANCE_S
    if not RUNTIME_MIN_S <= total <= RUNTIME_MAX_S:
        return [f"runtime: {total:.1f}s is outside {RUNTIME_MIN_S}..{RUNTIME_MAX_S}s"]
    return []


def validate(manifest: dict, cast_sheet: dict) -> list[str]:
    """Return human-readable errors for a manifest; an empty list means valid."""
    if not isinstance(manifest, dict):
        return ["load: manifest must be a mapping"]
    names = _cast_names(cast_sheet)
    errors: list[str] = []

    panels = manifest.get("panels")
    if not isinstance(panels, list):
        panels = []
    count = len(panels)
    if not MIN_PANELS <= count <= MAX_PANELS:
        errors.append(f"panel-count: {count} panels, expected {MIN_PANELS} to {MAX_PANELS}")

    for i, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            continue
        lines = panel.get("lines")
        for line in lines if isinstance(lines, list) else []:
            speaker = line.get("speaker") if isinstance(line, dict) else None
            if not isinstance(speaker, str) or speaker not in names:
                errors.append(
                    f"unknown-speaker: panel {i} speaker {speaker!r} is not in the cast sheet"
                )

    cast = manifest.get("cast")
    for entry in cast if isinstance(cast, list) else []:
        if not isinstance(entry, str) or entry not in names:
            errors.append(f"unknown-cast: cast entry {entry!r} is not in the cast sheet")

    overlays = []
    for i, panel in enumerate(panels, start=1):
        overlay = panel.get("overlay") if isinstance(panel, dict) else None
        if isinstance(overlay, str):
            overlays.append(overlay)
            if len(overlay) > OVERLAY_MAX:
                errors.append(
                    f"overlay-length: panel {i} overlay is {len(overlay)} characters, max {OVERLAY_MAX}"
                )

    tool = manifest.get("tool")
    if not isinstance(tool, str) or not any(tool.lower() in o.lower() for o in overlays):
        errors.append(f"tool-overlay: no overlay names the tool {tool!r}")

    errors.extend(_check_arrival_shot(manifest.get("arrival_shot"), count, names))
    errors.extend(_check_runtime(panels, count))
    return errors


def load_and_validate(path: str | Path) -> list[str]:
    """Load a manifest and its cast sheet from disk, then validate them."""
    path = Path(path)
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return [f"load: cannot read manifest {path}: {exc}"]
    if not isinstance(manifest, dict):
        return [f"load: manifest {path} must be a YAML mapping"]
    sheet_ref = manifest.get("cast_sheet")
    if not isinstance(sheet_ref, str):
        return ["load: manifest cast_sheet must be a path string"]
    sheet_path = path.parent / sheet_ref
    try:
        cast_sheet = yaml.safe_load(sheet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return [f"load: cannot read cast sheet {sheet_path}: {exc}"]
    if not isinstance(cast_sheet, dict):
        return [f"load: cast sheet {sheet_path} must be a YAML mapping"]
    return validate(manifest, cast_sheet)
