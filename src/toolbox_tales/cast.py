"""Generate cast candidate images through Oxen and approve one as a reference image.

The cast sheet and API responses are untrusted, so every field is type-checked. Generating never
modifies the cast sheet; approving changes exactly one key, `reference_image`, of one character.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from toolbox_tales.ledger import Ledger
from toolbox_tales.oxen import ImageResponseError, OxenClient, OxenError
from toolbox_tales.panels import _is_image, _write_atomic
from toolbox_tales.spend import SpendConfig, authorize, estimate, load_config

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CANDIDATE = re.compile(r"^c(\d{2,})\.[^.]+$")
_REQUIRED = ("name", "tool", "personality", "voice", "reference_image")
MIN_N, MAX_N = 1, 8


class CastError(Exception):
    """Cast tooling cannot run."""


@dataclass(frozen=True)
class CandidatesResult:
    written: tuple[Path, ...]
    failures: tuple[str, ...]
    refused: str | None


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME.match(name):
        raise CastError(f"cast: name {name!r} must be letters, digits, _ or -")


def _load_sheet(cast_path: Path) -> dict:
    try:
        sheet: Any = yaml.safe_load(Path(cast_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CastError(f"cast: cannot read {cast_path}: {exc}") from exc
    if not isinstance(sheet, dict) or not isinstance(sheet.get("characters"), list):
        raise CastError("cast: cast sheet must be a mapping with a characters list")
    return sheet


def _character(sheet: dict, name: str) -> dict:
    for char in sheet["characters"]:
        if isinstance(char, dict) and char.get("name") == name:
            return char
    raise CastError(f"cast: no character named {name!r}")


def _candidate_dir(cast_path: Path, name: str) -> Path:
    return Path(cast_path).parent / "media" / "cast" / name


def _highest(directory: Path) -> int:
    highest = 0
    if directory.is_dir():
        for entry in directory.iterdir():
            match = _CANDIDATE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest


def generate_candidates(
    cast_path: Path,
    name: str,
    n: int,
    client: OxenClient,
    confirmed: bool,
    ledger: Ledger | None = None,
    config: SpendConfig | None = None,
) -> CandidatesResult:
    cast_path = Path(cast_path)
    _check_name(name)
    if isinstance(n, bool) or not isinstance(n, int) or not MIN_N <= n <= MAX_N:
        raise CastError("cast: --n must be 1 to 8")
    sheet = _load_sheet(cast_path)
    style = sheet.get("style_prompt")
    if not isinstance(style, str):
        raise CastError("cast: style_prompt must be a string")
    character = _character(sheet, name)
    personality = character.get("personality")
    if not isinstance(personality, str):
        raise CastError(f"cast: personality for {name!r} must be a string")
    config = config or load_config()
    ledger = ledger or Ledger()

    prompt = (
        f"{style}. Character design sheet for {name}. {personality} "
        "One character, full figure, centered, plain background, no text."
    )
    directory = _candidate_dir(cast_path, name)
    first = _highest(directory) + 1
    model = config.image_model
    prices = client.get_prices(model)
    est = estimate(prices, model, "image", 1)

    written: list[Path] = []
    failures: list[str] = []
    refused: str | None = None
    for number in range(first, first + n):
        decision = authorize(est, confirmed, ledger.total_spent(), config)
        if not decision.allowed:
            refused = decision.reason
            break
        path = directory / f"c{number:02d}.png"
        rel = path.relative_to(cast_path.parent).as_posix()
        try:
            data = client.generate_image(model, prompt, None, "1:1")
        except ImageResponseError as exc:
            ledger.append(est, output=rel, actual_usd=est.cost_usd)
            failures.append(f"candidate {number:02d}: {exc}")
            continue
        except OxenError as exc:
            failures.append(f"candidate {number:02d}: {exc}")
            continue
        ledger.append(est, output=rel, actual_usd=est.cost_usd)
        if not _is_image(data):
            failures.append(f"candidate {number:02d}: response is not an image")
            continue
        _write_atomic(path, data)
        written.append(path)
    return CandidatesResult(tuple(written), tuple(failures), refused)


def _find_candidate(directory: Path, candidate: str) -> Path:
    if not isinstance(candidate, str) or not candidate:
        raise CastError("cast: candidate must be a number or a file name")
    if candidate.isascii() and candidate.isdigit():
        number = int(candidate)
        matches = (
            sorted(directory.glob(f"c{number:02d}.*")) if number >= 1 and directory.is_dir() else []
        )
        matches = [m for m in matches if m.is_file()]
        if len(matches) != 1:
            raise CastError(f"cast: candidate {candidate} matched {len(matches)} files, want 1")
        return matches[0]
    if (
        "/" in candidate
        or "\\" in candidate
        or ".." in candidate
        or candidate != Path(candidate).name
    ):
        raise CastError(f"cast: candidate {candidate!r} must be a bare file name")
    path = directory / candidate
    if not path.is_file():
        raise CastError(f"cast: candidate {candidate!r} does not exist")
    return path


def approve(cast_path: Path, name: str, candidate: str) -> Path:
    cast_path = Path(cast_path)
    _check_name(name)
    sheet = _load_sheet(cast_path)
    character = _character(sheet, name)
    source = _find_candidate(_candidate_dir(cast_path, name), candidate)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise CastError(f"cast: cannot read {source.name}: {exc}") from exc
    if not _is_image(data):
        raise CastError(f"cast: {source.name} is not an image")

    rel = f"refs/{name}{source.suffix.lower()}"
    ref = cast_path.parent / rel
    _write_atomic(ref, data)

    character["reference_image"] = rel
    text = yaml.safe_dump(sheet, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=cast_path.parent, prefix=f".{cast_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, cast_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    reloaded = _load_sheet(cast_path)
    for char in reloaded["characters"]:
        if not isinstance(char, dict) or any(not isinstance(char.get(k), str) for k in _REQUIRED):
            raise CastError(
                "cast: cast sheet no longer validates after approval (the write already happened)"
            )
    return ref
