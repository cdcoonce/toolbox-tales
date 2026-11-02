"""Oxen hub client. Prices only in this slice; no paid endpoint is called here."""

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Transport = Callable[[urllib.request.Request], tuple[int, bytes]]

_PRICE_FIELDS = (
    "cost_per_image",
    "cost_per_second",
    "input_cost_per_token",
    "output_cost_per_token",
)


class OxenError(Exception):
    """Raised on a non-2xx status or an unreadable response."""


@dataclass(frozen=True)
class Prices:
    cost_per_image: float | None
    cost_per_second: float | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    raw: dict = field(default_factory=dict)


def _default_transport(request: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OxenError(f"request failed: {exc}") from exc


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


class OxenClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://hub.oxen.ai/api",
        transport: Transport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _default_transport

    def get_prices(self, model_id: str) -> Prices:
        request = urllib.request.Request(
            f"{self.base_url}/ai/models/{model_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        status, body = self.transport(request)
        if not 200 <= status < 300:
            raise OxenError(f"HTTP {status} fetching prices for {model_id}")
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OxenError(f"HTTP {status}: response for {model_id} is not JSON") from exc
        if not isinstance(parsed, dict):
            raise OxenError(f"HTTP {status}: response for {model_id} is not a JSON object")
        source: dict = parsed
        for key in ("pricing", "data", "model"):
            if isinstance(parsed.get(key), dict):
                source = parsed[key]
                break
        return Prices(**{name: _price(source.get(name)) for name in _PRICE_FIELDS}, raw=parsed)
