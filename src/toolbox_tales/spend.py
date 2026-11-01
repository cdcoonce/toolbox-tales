"""Spend guard: cost estimates and the authorize decision. Every paid call goes through here."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from toolbox_tales.oxen import Prices


@dataclass(frozen=True)
class SpendConfig:
    per_call_cap_usd: float
    total_cap_usd: float
    image_model: str = ""
    video_model: str = ""
    script_model: str = ""
    script_input_usd_per_mtok: float = 0.0
    script_output_usd_per_mtok: float = 0.0
    est_input_tokens: float = 0
    est_output_tokens: float = 0


@dataclass(frozen=True)
class Estimate:
    model: str
    kind: str  # "image", "video" or "text"
    quantity: float
    cost_usd: float | None  # None means unpriced


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str  # ok, unpriced, per-call-cap, total-cap, unconfirmed


def _num(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"config {name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"config {name} must be finite and non-negative")
    return float(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"config {name} must be a string")
    return value


def load_config(path: Path = Path("config/spend.toml")) -> SpendConfig:
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    models = data.get("models", {})
    script = data.get("script", {})
    if not isinstance(models, dict) or not isinstance(script, dict):
        raise TypeError("config [models] and [script] must be tables")
    return SpendConfig(
        per_call_cap_usd=_num(data.get("per_call_cap_usd"), "per_call_cap_usd"),
        total_cap_usd=_num(data.get("total_cap_usd"), "total_cap_usd"),
        image_model=_text(models.get("image", ""), "models.image"),
        video_model=_text(models.get("video", ""), "models.video"),
        script_model=_text(script.get("model", ""), "script.model"),
        script_input_usd_per_mtok=_num(script.get("input_usd_per_mtok", 0.0), "script.input"),
        script_output_usd_per_mtok=_num(script.get("output_usd_per_mtok", 0.0), "script.output"),
        est_input_tokens=_num(script.get("est_input_tokens", 0), "script.est_input_tokens"),
        est_output_tokens=_num(script.get("est_output_tokens", 0), "script.est_output_tokens"),
    )


def estimate(
    prices: Prices, model: str, kind: str, quantity: float, output_tokens: float = 0
) -> Estimate:
    """Price a call. A missing price gives cost_usd None, never 0."""
    cost: float | None
    if kind == "image":
        cost = None if prices.cost_per_image is None else prices.cost_per_image * quantity
    elif kind == "video":
        cost = None if prices.cost_per_second is None else prices.cost_per_second * quantity
    elif kind == "text":
        if prices.input_cost_per_token is None or prices.output_cost_per_token is None:
            cost = None
        else:
            cost = (
                prices.input_cost_per_token * quantity
                + prices.output_cost_per_token * output_tokens
            )
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return Estimate(model=model, kind=kind, quantity=quantity, cost_usd=cost)


def authorize(est: Estimate, confirmed: bool, spent_usd: float, config: SpendConfig) -> Decision:
    """Refuse in order: unpriced, per-call-cap, total-cap, unconfirmed. Caps allow equality."""
    if est.cost_usd is None:
        return Decision(False, "unpriced")
    if est.cost_usd > config.per_call_cap_usd:
        return Decision(False, "per-call-cap")
    if spent_usd + est.cost_usd > config.total_cap_usd:
        return Decision(False, "total-cap")
    if not confirmed:
        return Decision(False, "unconfirmed")
    return Decision(True, "ok")
