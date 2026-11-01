"""Synthesize each manifest line with edge-tts and write the timing back into the manifest.

Every line gets `media/voice/<manifest stem>/pPP-lLL.mp3` plus a `.key` sidecar holding the
sha256 of `voice + "\\n" + text`, so unchanged lines are skipped on a re-run. A line that fails
keeps its exact prior `duration` and `audio`; the rest of the manifest is still written, once and
atomically. The manifest is untrusted YAML, so every field is type-checked.
"""

import asyncio
import hashlib
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from toolbox_tales.manifest import load_and_validate

Synthesizer = Callable[[str, str, Path], None]
Measurer = Callable[[Path], float]
Runner = Callable[..., Any]


class VoiceError(Exception):
    """The voice step cannot run, or a measurement was unusable."""


@dataclass(frozen=True)
class VoiceResult:
    voiced: int
    skipped: int
    failures: tuple[str, ...]


def edge_synthesize(text: str, voice: str, out_path: Path) -> None:
    """Speak `text` in `voice` with edge-tts (the only network call) and save an MP3."""
    import edge_tts  # lazy: tests and non-voice commands never need the package

    out_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(edge_tts.Communicate(text, voice).save(str(out_path)))


def ffprobe_duration(path: Path, runner: Runner = subprocess.run) -> float:
    """Return the audio duration in seconds, or raise VoiceError on unusable output."""
    result = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    out = result.stdout
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    try:
        value = float(str(out).strip())
    except ValueError:
        raise VoiceError(
            f"ffprobe gave no duration for {path.name}: {str(out).strip()!r}"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise VoiceError(f"ffprobe gave an unusable duration for {path.name}: {value}")
    return value


def line_key(voice: str, text: str) -> str:
    return hashlib.sha256((voice + "\n" + text).encode("utf-8")).hexdigest()


def _voices(cast_sheet: Any) -> dict[str, str]:
    voices: dict[str, str] = {}
    characters = cast_sheet.get("characters") if isinstance(cast_sheet, dict) else None
    for entry in characters if isinstance(characters, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            voice = entry.get("voice")
            voices[entry["name"]] = voice.strip() if isinstance(voice, str) else ""
    return voices


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise VoiceError(f"cannot read {path}: {exc}") from exc


def _write_back(manifest_path: Path, data: dict) -> None:
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=manifest_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
        os.replace(tmp_name, manifest_path)
    except Exception as exc:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
        raise VoiceError(f"cannot write manifest {manifest_path}: {exc}") from exc


def _voice_line(
    line: dict,
    voices: dict[str, str],
    audio: Path,
    base: Path,
    synth: Synthesizer,
    measure: Measurer,
    redo: bool,
) -> bool:
    """Voice one line; return True if synthesized, False if skipped. Raises on failure."""
    text = line.get("text")
    speaker = line.get("speaker")
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("line has no text")
    voice = voices.get(speaker, "") if isinstance(speaker, str) else ""
    if not voice:
        raise VoiceError(f"no voice for speaker {speaker!r} in the cast sheet")
    key = line_key(voice, text)
    key_path = audio.with_suffix(".key")
    skip = False
    if not redo and audio.exists() and key_path.exists():
        try:
            skip = key_path.read_text(encoding="utf-8").strip() == key
        except (OSError, UnicodeDecodeError):
            skip = False
    if not skip:
        synth(text, voice, audio)
        key_path.write_text(key, encoding="utf-8")
    duration = round(float(measure(audio)), 3)
    line["audio"] = audio.relative_to(base).as_posix()
    line["duration"] = duration
    return not skip


def voice_episode(
    manifest_path: Path,
    synth: Synthesizer = edge_synthesize,
    measure: Measurer = ffprobe_duration,
    redo: bool = False,
) -> VoiceResult:
    """Voice every line of the manifest and write audio and duration back."""
    manifest_path = Path(manifest_path)
    errors = [e for e in load_and_validate(manifest_path) if not e.startswith("runtime:")]
    if errors:
        raise VoiceError("invalid manifest: " + "; ".join(errors))
    data = _read_yaml(manifest_path)
    if not isinstance(data, dict):
        raise VoiceError("manifest must be a YAML mapping")
    sheet_ref = data.get("cast_sheet")
    if not isinstance(sheet_ref, str):
        raise VoiceError("manifest cast_sheet must be a path string")
    voices = _voices(_read_yaml(manifest_path.parent / sheet_ref))

    base = manifest_path.parent
    voiced = skipped = 0
    failures: list[str] = []
    panels = data.get("panels")
    for p, panel in enumerate(panels if isinstance(panels, list) else [], start=1):
        lines = panel.get("lines") if isinstance(panel, dict) else None
        for k, line in enumerate(lines if isinstance(lines, list) else [], start=1):
            label = f"panel {p} line {k}"
            if not isinstance(line, dict):
                failures.append(f"{label}: VoiceError: line must be a mapping")
                continue
            audio = base / "media" / "voice" / manifest_path.stem / f"p{p:02d}-l{k:02d}.mp3"
            try:
                if _voice_line(line, voices, audio, base, synth, measure, redo):
                    voiced += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - one bad line must never abort the rest
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
    _write_back(manifest_path, data)
    return VoiceResult(voiced=voiced, skipped=skipped, failures=tuple(failures))
