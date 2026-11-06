"""Oxen hub client: model prices and image generation."""

import base64
import binascii
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


class ImageResponseError(OxenError):
    """A 2xx image response that holds no readable image: billed, but unusable."""


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

    def generate_image(
        self,
        model: str,
        prompt: str,
        input_images: list[str] | None = None,
        aspect_ratio: str = "16:9",
    ) -> bytes:
        """Generate one image and return its decoded bytes.

        A non-2xx status raises OxenError (not billed). A 2xx response that holds no readable
        image raises ImageResponseError (billed). The caller checks the image magic.
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "response_format": "b64_json",
            "aspect_ratio": aspect_ratio,
        }
        if input_images:
            payload["input_image"] = input_images
        request = urllib.request.Request(
            f"{self.base_url}/ai/images/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        status, body = self.transport(request)
        if not 200 <= status < 300:
            raise OxenError(f"HTTP {status} generating image with {model}")
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ImageResponseError(f"HTTP {status}: image response is not JSON") from exc
        images = parsed.get("images") if isinstance(parsed, dict) else None
        if not isinstance(images, list) or not images:
            raise ImageResponseError(f"HTTP {status}: image response has no images")
        first = images[0]
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ImageResponseError(f"HTTP {status}: image response has no b64_json")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageResponseError(f"HTTP {status}: image b64_json is not valid base64") from exc
