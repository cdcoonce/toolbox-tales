"""Generate the arrival shot through the Oxen async queue, behind the spend guard.

The request is built before any paid call, so a bad input costs nothing. The generation id is
written to the manifest right after the paid enqueue, so an interrupted run resumes polling
instead of paying again. Manifest, cast sheet and API responses are untrusted.
"""

import base64
import os
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from toolbox_tales.ledger import Ledger
from toolbox_tales.manifest import load_and_validate
from toolbox_tales.oxen import OxenClient, OxenError
from toolbox_tales.panels import _image_kind
from toolbox_tales.spend import Estimate, SpendConfig, authorize, estimate, load_config
from toolbox_tales.voice import VoiceError, ffprobe_duration

MAX_REFERENCE_BYTES = 10 * 1024 * 1024
_MIME = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
_DEFAULTS: dict[str, Any] = {
    "fallback_video": "kling-video-v2-5-turbo-pro-image-to-video",
    "seconds": 5,
    "poll_interval_s": 15,
    "timeout_s": 1200,
}


class ShotError(Exception):
    """The arrival shot cannot run."""


@dataclass(frozen=True)
class ShotResult:
    status: str  # exists, succeeded, failed, cancelled, timeout, spend-refused
    model: str | None
    generation_id: str | None
    duration_s: float | None
    message: str


def _load_shot_config(config_path: Path) -> dict[str, Any]:
    try:
        with Path(config_path).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ShotError(f"config: {exc}") from exc
    section = data.get("shot", {})
    if not isinstance(section, dict):
        raise ShotError("config: [shot] must be a table")
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        value = section.get(key, default)
        if isinstance(default, str):
            if not isinstance(value, str) or not value:
                raise ShotError(f"config: shot.{key} must be a non-empty string")
        else:
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ShotError(f"config: shot.{key} must be a positive number")
        out[key] = value
    return out


def _image_uri(path: Path, label: str) -> str:
    mime = _MIME.get(path.suffix.lower())
    if mime is None:
        raise ShotError(f"{label}: {path.name} must be a .png, .jpg, .jpeg or .webp file")
    if not path.is_file():
        raise ShotError(f"{label}: {path.name} does not exist")
    try:
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise ShotError(f"{label}: {path.name} is larger than 10 MB")
        data = path.read_bytes()
    except OSError as exc:
        raise ShotError(f"{label}: cannot read {path.name}: {exc}") from exc
    kind = _image_kind(data)
    if kind is None:
        raise ShotError(f"{label}: {path.name} is not a PNG, JPEG or WebP image")
    return f"data:image/{kind};base64,{base64.b64encode(data).decode('ascii')}"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _update_manifest(manifest_path: Path, set_keys: dict[str, Any], drop: tuple[str, ...]) -> None:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    shot = data["arrival_shot"]
    shot.update(set_keys)
    for key in drop:
        shot.pop(key, None)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _write_atomic(manifest_path, text.encode("utf-8"))


def _priced_for_three(est: Estimate, remaining: float) -> bool:
    return est.cost_usd is not None and est.cost_usd * 3 <= remaining


def make_shot(
    manifest_path: Path,
    client: OxenClient,
    confirmed: bool,
    ledger: Ledger | None = None,
    config: SpendConfig | None = None,
    config_path: Path = Path("config/spend.toml"),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    measure: Callable[[Path], float] = ffprobe_duration,
) -> ShotResult:
    manifest_path = Path(manifest_path)
    errors = [e for e in load_and_validate(manifest_path) if not e.startswith("runtime:")]
    if errors:
        raise ShotError("\n".join(errors))
    manifest: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    shot = manifest["arrival_shot"]

    video_rel = shot.get("video")
    if not isinstance(video_rel, str) or not video_rel:
        raise ShotError("shot: no video path")
    video = (base / video_rel).resolve()
    try:
        video.relative_to(base.resolve())
    except ValueError:
        raise ShotError("shot: video path escapes the manifest directory") from None
    if video.exists():
        return ShotResult("exists", None, None, None, str(video))

    settings = _load_shot_config(config_path)
    config = config or _load_spend(config_path)
    ledger = ledger or Ledger()

    pending = shot.get("generation_id")
    if "generation_id" in shot and (not isinstance(pending, str) or not pending):
        raise ShotError("shot: generation_id must be a non-empty string; remove it to resubmit")
    if isinstance(pending, str):
        model = shot.get("model")
        return _poll(
            manifest_path,
            client,
            pending,
            model if isinstance(model, str) else None,
            video,
            settings,
            sleep,
            clock,
            measure,
        )

    sheet: Any = yaml.safe_load((base / manifest["cast_sheet"]).read_text(encoding="utf-8"))
    cast_dir = (base / manifest["cast_sheet"]).parent
    characters = sheet.get("characters") if isinstance(sheet, dict) else None
    char: dict = {}
    for c in characters if isinstance(characters, list) else []:
        if isinstance(c, dict) and c.get("name") == shot.get("reference"):
            char = c

    panels = manifest["panels"]
    panel = panels[shot["after_panel"] - 1]
    image_rel = panel.get("image") if isinstance(panel, dict) else None
    if not isinstance(image_rel, str) or not image_rel:
        raise ShotError(f"shot: panel {shot['after_panel']} has no image path")
    start = (base / image_rel).resolve()
    try:
        start.relative_to(base.resolve())
    except ValueError:
        raise ShotError("shot: start frame path escapes the manifest directory") from None
    if not start.is_file():
        raise ShotError(f"shot: start frame {start} is missing; run panels first")
    start_uri = _image_uri(start, "shot: start frame")

    prompt = shot.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    ref_uri: str | None = None
    name = char.get("name", "")
    ref = char.get("reference_image")
    if isinstance(ref, str) and ref:
        ref_uri = _image_uri(cast_dir / ref, f"shot: reference for {name}")

    seconds = settings["seconds"]
    spent = ledger.total_spent()
    remaining = config.total_cap_usd - spent
    preferred = config.video_model
    fallback = settings["fallback_video"]
    est = estimate(client.get_prices(preferred), preferred, "video", seconds)
    chosen = preferred
    if not _priced_for_three(est, remaining):
        est_f = estimate(client.get_prices(fallback), fallback, "video", seconds)
        if _priced_for_three(est_f, remaining):
            chosen, est = fallback, est_f
    decision = authorize(est, confirmed, spent, config)
    if not decision.allowed:
        return ShotResult("spend-refused", chosen, None, None, decision.reason)

    params: dict[str, Any] = {
        "prompt": prompt,
        "input_image": start_uri,
        "duration": seconds,
        "aspect_ratio": "16:9",
    }
    if chosen == preferred:
        params["resolution"] = "720p"
        params["generate_audio"] = False
        if ref_uri is not None:
            params["input_images"] = [ref_uri]
            params["prompt"] = f"{prompt} {name} is @Image1."
    try:
        generation_id = client.enqueue(chosen, params)
    except OxenError as exc:
        return ShotResult("failed", chosen, None, None, str(exc))
    ledger.append(est, output=f"queue:{generation_id}", actual_usd=est.cost_usd)
    _update_manifest(manifest_path, {"generation_id": generation_id, "model": chosen}, ())
    print(f"submitted {generation_id} ({chosen})", flush=True)
    return _poll(
        manifest_path, client, generation_id, chosen, video, settings, sleep, clock, measure
    )


def _load_spend(config_path: Path) -> SpendConfig:
    try:
        return load_config(config_path)
    except (TypeError, ValueError, tomllib.TOMLDecodeError, OSError) as exc:
        raise ShotError(f"config: {exc}") from exc


def _poll(
    manifest_path: Path,
    client: OxenClient,
    generation_id: str,
    model: str | None,
    video: Path,
    settings: dict[str, Any],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    measure: Callable[[Path], float],
) -> ShotResult:
    def result(status: str, message: str, duration: float | None = None) -> ShotResult:
        return ShotResult(status, model, generation_id, duration, message)

    start = clock()
    while True:
        try:
            g = client.get_generation(generation_id)
        except OxenError as exc:
            return result("failed", str(exc))
        status = g.get("status")
        if status == "succeeded":
            break
        if status in ("failed", "cancelled"):
            _update_manifest(manifest_path, {}, ("generation_id",))
            error = g.get("error_message")
            return result(status, error if isinstance(error, str) and error else status)
        if status in ("queued", "processing"):
            if clock() - start >= settings["timeout_s"]:
                return result(
                    "timeout",
                    f"shot: still running after {settings['timeout_s']} s; rerun to resume",
                )
            sleep(settings["poll_interval_s"])
            continue
        return result("failed", f"shot: unknown generation status {status!r}")

    url = g.get("result_url")
    if not isinstance(url, str) or not url:
        return result("failed", "shot: succeeded generation has no result_url")
    try:
        data = client.download(url)
    except OxenError as exc:
        return result("failed", str(exc))
    if data[4:8] != b"ftyp":
        return result("failed", "shot: result is not an MP4")
    _write_atomic(video, data)
    try:
        duration = round(float(measure(video)), 3)
    except (VoiceError, OSError, ValueError, TypeError) as exc:
        return result("failed", f"shot: cannot measure {video.name}: {exc}")
    _update_manifest(manifest_path, {"duration": duration}, ("generation_id",))
    return result("succeeded", str(video), duration)
