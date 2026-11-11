"""Generate panel images through Oxen, behind the spend guard.

Every request, including cast reference checks, is built before the first API call, so a bad
reference fails the run with zero spend. The manifest, cast sheet and API response are untrusted,
so every field is type-checked. The manifest file is never modified.
"""

import base64
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from toolbox_tales.ledger import Ledger
from toolbox_tales.manifest import load_and_validate
from toolbox_tales.oxen import ImageResponseError, OxenClient, OxenError
from toolbox_tales.spend import SpendConfig, authorize, estimate, load_config

MAX_REFERENCE_BYTES = 10 * 1024 * 1024
_MIME = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


class PanelsError(Exception):
    """Panel generation cannot run."""


@dataclass(frozen=True)
class PanelsResult:
    generated: tuple[int, ...]
    skipped: int
    failures: tuple[str, ...]
    refused: str | None


@dataclass(frozen=True)
class _Request:
    number: int
    image: Path
    image_rel: str
    prompt: str
    input_images: list[str]


def _load_panels_config(config_path: Path) -> tuple[bool, str]:
    try:
        with Path(config_path).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PanelsError(f"config: {exc}") from exc
    section = data.get("panels", {})
    if not isinstance(section, dict):
        raise PanelsError("config: [panels] must be a table")
    references = section.get("references", False)
    aspect = section.get("aspect_ratio", "16:9")
    if not isinstance(references, bool):
        raise PanelsError("config: panels.references must be true or false")
    if not isinstance(aspect, str):
        raise PanelsError("config: panels.aspect_ratio must be a string")
    return references, aspect


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _is_image(data: bytes) -> bool:
    return _image_kind(data) is not None


def _reference(n: int, name: str, value: str, cast_dir: Path) -> str:
    def fail(why: str) -> PanelsError:
        return PanelsError(f"panel {n}: reference for {name}: {why}")

    path = cast_dir / value
    mime = _MIME.get(path.suffix.lower())
    if mime is None:
        raise fail(f"{value} must be a .png, .jpg, .jpeg or .webp file")
    if not path.is_file():
        raise fail(f"{value} does not exist")
    try:
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise fail(f"{value} is larger than 10 MB")
        data = path.read_bytes()
    except OSError as exc:
        raise fail(f"cannot read {value}: {exc}") from exc
    kind = _image_kind(data)
    if kind is None:
        raise fail(f"{value} is not a PNG, JPEG or WebP image")
    return f"data:image/{kind};base64,{base64.b64encode(data).decode('ascii')}"


def _speakers(panel: dict) -> list[str]:
    lines = panel.get("lines")
    seen: list[str] = []
    for line in lines if isinstance(lines, list) else []:
        speaker = line.get("speaker") if isinstance(line, dict) else None
        if isinstance(speaker, str) and speaker not in seen:
            seen.append(speaker)
    return seen


def _build_request(
    n: int, panel: dict, base: Path, cast: dict[str, dict], cast_dir: Path, style: str, refs: bool
) -> _Request:
    image_rel = panel.get("image")
    if not isinstance(image_rel, str) or not image_rel:
        raise PanelsError(f"panel {n}: no image path")
    base_resolved = base.resolve()
    image = (base / image_rel).resolve()
    try:
        image.relative_to(base_resolved)
    except ValueError:
        raise PanelsError(f"panel {n}: image path escapes the manifest directory") from None
    prompt_text = panel.get("prompt")
    prompt = f"{style}. {prompt_text if isinstance(prompt_text, str) else ''}"
    referenced: list[str] = []
    described: list[tuple[str, str]] = []
    input_images: list[str] = []
    for name in _speakers(panel):
        char = cast.get(name, {})
        ref = char.get("reference_image")
        if refs and isinstance(ref, str) and ref:
            input_images.append(_reference(n, name, ref, cast_dir))
            referenced.append(name)
        else:
            personality = char.get("personality")
            described.append((name, personality if isinstance(personality, str) else ""))
    for k, name in enumerate(referenced, start=1):
        prompt += f"\n{name} is @Image{k}."
    for name, personality in described:
        prompt += f"\n{name}: {personality}"
    return _Request(n, image, image_rel, prompt, input_images)


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


def generate_panels(
    manifest_path: Path,
    client: OxenClient,
    confirmed: bool,
    panel: int | None = None,
    ledger: Ledger | None = None,
    config: SpendConfig | None = None,
    config_path: Path = Path("config/spend.toml"),
) -> PanelsResult:
    manifest_path = Path(manifest_path)
    errors = [e for e in load_and_validate(manifest_path) if not e.startswith("runtime:")]
    if errors:
        raise PanelsError("\n".join(errors))
    manifest: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    sheet: Any = yaml.safe_load((base / manifest["cast_sheet"]).read_text(encoding="utf-8"))
    cast_dir = (base / manifest["cast_sheet"]).parent
    references, aspect = _load_panels_config(config_path)
    config = config or load_config(config_path)
    ledger = ledger or Ledger()

    panels = manifest["panels"]
    for n, p in enumerate(panels, start=1):
        if not isinstance(p, dict):
            raise PanelsError(f"panel {n}: must be a mapping")
    characters = sheet.get("characters") if isinstance(sheet, dict) else None
    cast: dict[str, dict] = {}
    for c in characters if isinstance(characters, list) else []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            cast[c["name"]] = c
    style = sheet.get("style_prompt") if isinstance(sheet, dict) else None
    if not isinstance(style, str):
        raise PanelsError("cast sheet: style_prompt must be a string")

    # Resolve every panel's image path first (escape / missing key fail the run).
    resolved: list[tuple[int, Path]] = []
    for n, p in enumerate(panels, start=1):
        req = _build_request(n, p, base, cast, cast_dir, style, False)
        resolved.append((n, req.image))

    if panel is not None:
        if not 1 <= panel <= len(panels):
            raise PanelsError(f"--panel {panel}: episode has {len(panels)} panels")
        target_numbers = [panel]
        skipped = 0
    else:
        target_numbers = [n for n, img in resolved if not img.exists()]
        skipped = len(panels) - len(target_numbers)
    if not target_numbers:
        return PanelsResult((), skipped, (), None)

    requests = [
        _build_request(n, panels[n - 1], base, cast, cast_dir, style, references)
        for n in target_numbers
    ]

    model = config.image_model
    prices = client.get_prices(model)
    est = estimate(prices, model, "image", 1)

    generated: list[int] = []
    failures: list[str] = []
    refused: str | None = None
    for req in requests:
        decision = authorize(est, confirmed, ledger.total_spent(), config)
        if not decision.allowed:
            refused = decision.reason
            break
        try:
            data = client.generate_image(model, req.prompt, req.input_images or None, aspect)
        except ImageResponseError as exc:
            ledger.append(est, output=req.image_rel, actual_usd=est.cost_usd)
            failures.append(f"panel {req.number}: {exc}")
            continue
        except OxenError as exc:
            failures.append(f"panel {req.number}: {exc}")
            continue
        ledger.append(est, output=req.image_rel, actual_usd=est.cost_usd)
        if not _is_image(data):
            failures.append(f"panel {req.number}: response is not an image")
            continue
        _write_atomic(req.image, data)
        generated.append(req.number)
    return PanelsResult(tuple(generated), skipped, tuple(failures), refused)
