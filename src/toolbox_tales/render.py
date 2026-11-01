"""Plan an episode as ordered segments and render it to an MP4 with ffmpeg.

`build_plan` is pure: it turns a validated manifest into a `Plan` and does no subprocess
calls and no writes. `render` is the thin ffmpeg runner. All on-screen text (placeholder
prompts and overlays) is untrusted, so it only ever reaches ffmpeg through drawtext
`textfile=` temp files, never inline in a filter string.
"""

import subprocess
import sys
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
        )
        for panel in manifest["panels"]
    ]
    shot_data = manifest["arrival_shot"]
    shot = Segment(
        kind="shot",
        source=_existing(manifest_dir, shot_data.get("video")),
        duration_s=_number(shot_data.get("duration"), SHOT_ALLOWANCE_S),
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


def _segment_command(
    plan: Plan, index: int, segment: Segment, tmp: Path, ffmpeg: str, text: bool = True
) -> list:
    size = f"{plan.width}x{plan.height}"
    fit = (
        f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
        f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={plan.fps}"
    )
    dur = f"{segment.duration_s:.3f}"
    filters: list[str] = []
    if segment.source is None:
        inputs = ["-f", "lavfi", "-i", f"color=c={_BACKGROUND}:s={size}:r={plan.fps}:d={dur}"]
        if text and segment.placeholder_text:
            path = _write_text(tmp, f"{index}-placeholder.txt", segment.placeholder_text)
            filters.append(_drawtext(path, 22, "24"))
    elif segment.kind == "shot":
        inputs = ["-i", str(segment.source)]
        filters.append(fit)
        filters.append(f"tpad=stop_mode=clone:stop_duration={dur}")
    else:
        inputs = ["-loop", "1", "-framerate", str(plan.fps), "-i", str(segment.source)]
        filters.append(fit)
    if text and segment.overlay:
        path = _write_text(tmp, f"{index}-overlay.txt", segment.overlay)
        filters.append(_drawtext(path, 36, "h-th-40"))
    filters.append("format=yuv420p")
    return [
        ffmpeg,
        "-y",
        "-v",
        "error",
        *inputs,
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=44100:cl=stereo:d={dur}",
        "-vf",
        ",".join(filters),
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
        "-shortest",
        str(tmp / f"seg{index:02d}.mp4"),
    ]


def _run(runner: Runner, args: list) -> None:
    result = runner(args, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RenderError(f"ffmpeg failed: {str(stderr or '').strip()[-_STDERR_TAIL:]}")


def render(plan: Plan, ffmpeg: str = "ffmpeg", runner: Runner = subprocess.run) -> None:
    """Write `plan.output` as H.264/yuv420p MP4 with a silent audio track."""
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="toolbox-tales-") as name:
        tmp = Path(name)
        text = True
        for index, segment in enumerate(plan.segments):
            try:
                _run(runner, _segment_command(plan, index, segment, tmp, ffmpeg, text))
            except RenderError as exc:
                if not text or "drawtext" not in str(exc):
                    raise
                # This ffmpeg build lacks drawtext (no freetype). Text is never inlined as a
                # fallback; the episode renders without on-screen text instead.
                print(
                    "warning: ffmpeg has no drawtext filter; rendering without text",
                    file=sys.stderr,
                )
                text = False
                _run(runner, _segment_command(plan, index, segment, tmp, ffmpeg, text))
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
