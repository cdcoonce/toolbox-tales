"""Plan an episode as ordered segments and render it to an MP4 with ffmpeg.

`build_plan` is pure: it turns a validated manifest into a `Plan` and does no subprocess
calls and no writes. `render` is the thin ffmpeg runner. All on-screen text (placeholder
prompts and overlays) is untrusted, so it only ever reaches ffmpeg through drawtext
`textfile=` temp files, never inline in a filter string.
"""

import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toolbox_tales.manifest import PANEL_PADDING_S, SHOT_ALLOWANCE_S

DEFAULT_LINE_S = 3.0
_BACKGROUND = "0x2b3a4a"
_STDERR_TAIL = 1500

Runner = Callable[..., Any]


class RenderError(Exception):
    """ffmpeg failed to produce the output."""


@dataclass(frozen=True)
class Segment:
    kind: str  # "panel" or "shot"
    source: Path | None  # None means a placeholder
    duration_s: float
    overlay: str | None
    placeholder_text: str | None
    audio: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Plan:
    segments: tuple[Segment, ...]
    audio: tuple[Path, ...]
    output: Path
    width: int = 1280
    height: int = 720
    fps: int = 24


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _existing(base: Path, name: Any) -> Path | None:
    if not isinstance(name, str):
        return None
    path = base / name
    return path if path.exists() else None


def _panel_audio(panel: dict, base: Path) -> tuple[Path, ...]:
    """Voice files in line order, or () unless every line has an existing audio file."""
    lines = panel.get("lines")
    if not isinstance(lines, list) or not lines:
        return ()
    files = []
    for line in lines:
        found = _existing(base, line.get("audio") if isinstance(line, dict) else None)
        if found is None:
            return ()
        files.append(found)
    return tuple(files)


def build_plan(manifest: dict, manifest_dir: Path) -> Plan:
    """Turn a validated manifest into an ordered render plan."""
    panels = [
        Segment(
            kind="panel",
            source=_existing(manifest_dir, panel.get("image")),
            duration_s=sum(
                _number(line.get("duration"), DEFAULT_LINE_S) for line in panel.get("lines", [])
            )
            + PANEL_PADDING_S,
            overlay=panel.get("overlay"),
            placeholder_text=panel.get("prompt"),
            audio=_panel_audio(panel, manifest_dir),
        )
        for panel in manifest["panels"]
    ]
    shot_data = manifest["arrival_shot"]
    shot_duration = _number(shot_data.get("duration"), SHOT_ALLOWANCE_S)
    if not math.isfinite(shot_duration) or shot_duration <= 0:
        raise ValueError("shot: duration must be a positive number of seconds")
    shot = Segment(
        kind="shot",
        source=_existing(manifest_dir, shot_data.get("video")),
        duration_s=shot_duration,
        overlay=None,
        placeholder_text=shot_data.get("prompt"),
    )
    after = int(shot_data["after_panel"])
    segments = (*panels[:after], shot, *panels[after:])
    return Plan(segments=segments, audio=(), output=manifest_dir / manifest["render"])


def plan_runtime(plan: Plan) -> float:
    return sum(segment.duration_s for segment in plan.segments)


def _drawtext(textfile: Path, size: int, y: str) -> str:
    # expansion=none stops ffmpeg treating %{...} in the file as a function call.
    return (
        f"drawtext=textfile='{textfile}':expansion=none:fontsize={size}:fontcolor=white"
        f":box=1:boxcolor=black@0.5:boxborderw=8:x=(w-text_w)/2:y={y}"
    )


def _write_text(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _segment_command(plan: Plan, index: int, segment: Segment, tmp: Path, ffmpeg: str) -> list:
    size = f"{plan.width}x{plan.height}"
    fit = (
        f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
        f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={plan.fps}"
    )
    dur = f"{segment.duration_s:.3f}"
    filters: list[str] = []
    if segment.source is None:
        inputs = ["-f", "lavfi", "-i", f"color=c={_BACKGROUND}:s={size}:r={plan.fps}:d={dur}"]
        if segment.placeholder_text:
            path = _write_text(tmp, f"{index}-placeholder.txt", segment.placeholder_text)
            filters.append(_drawtext(path, 22, "24"))
    elif segment.kind == "shot":
        inputs = ["-i", str(segment.source)]
        filters.append(fit)
        filters.append(f"tpad=stop_mode=clone:stop_duration={dur}")
    else:
        inputs = ["-loop", "1", "-framerate", str(plan.fps), "-i", str(segment.source)]
        filters.append(fit)
    if segment.overlay:
        path = _write_text(tmp, f"{index}-overlay.txt", segment.overlay)
        filters.append(_drawtext(path, 36, "h-th-40"))
    filters.append("format=yuv420p")
    head = [ffmpeg, "-y", "-v", "error", *inputs]
    tail = [
        "-t",
        dur,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(tmp / f"seg{index:02d}.mp4"),
    ]
    if not segment.audio:
        silence = ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur}"]
        return [*head, *silence, "-vf", ",".join(filters), "-shortest", *tail]
    voice_inputs: list[str] = []
    for path in segment.audio:
        voice_inputs += ["-i", str(path)]
    first = 1  # input 0 is the picture
    labels = "".join(f"[a{i}]" for i in range(len(segment.audio)))
    norm = ";".join(
        f"[{first + i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]"
        for i in range(len(segment.audio))
    )
    graph = (
        f"[0:v]{','.join(filters)}[v];{norm};"
        f"{labels}concat=n={len(segment.audio)}:v=0:a=1,apad=whole_dur={dur}[a]"
    )
    return [*head, *voice_inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *tail]


def _run(runner: Runner, args: list) -> None:
    result = runner(args, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RenderError(f"ffmpeg failed: {str(stderr or '').strip()[-_STDERR_TAIL:]}")


def _require_drawtext(runner: Runner, ffmpeg: str) -> None:
    result = runner([ffmpeg, "-hide_banner", "-filters"], capture_output=True, check=False)
    stdout = result.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if result.returncode != 0 or "drawtext" not in str(stdout or ""):
        raise RenderError(
            "ffmpeg has no drawtext filter: install an ffmpeg built with freetype (drawtext)"
        )


def render(plan: Plan, ffmpeg: str = "ffmpeg", runner: Runner = subprocess.run) -> None:
    """Write `plan.output` as H.264/yuv420p MP4 with the line voices mixed under voiced panels (silence elsewhere)."""
    _require_drawtext(runner, ffmpeg)
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="toolbox-tales-") as name:
        tmp = Path(name)
        for index, segment in enumerate(plan.segments):
            _run(runner, _segment_command(plan, index, segment, tmp, ffmpeg))
        listing = tmp / "concat.txt"
        listing.write_text(
            "".join(f"file 'seg{i:02d}.mp4'\n" for i in range(len(plan.segments))),
            encoding="utf-8",
        )
        _run(
            runner,
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(plan.output),
            ],
        )
