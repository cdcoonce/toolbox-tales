"""Tests for the Oxen price client, with a fake transport."""

import json

import pytest

from toolbox_tales.oxen import OxenClient, OxenError


class FakeTransport:
    def __init__(self, status, body):
        self.status, self.body, self.requests = status, body, []

    def __call__(self, request):
        self.requests.append(request)
        body = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, body


def client(status, body):
    t = FakeTransport(status, body)
    return OxenClient("k123", transport=t), t


def test_request_shape():
    c, t = client(200, {"cost_per_image": 0.01})
    c.get_prices("flux-2-dev")
    req = t.requests[0]
    assert req.get_method() == "GET"
    assert req.full_url == "https://hub.oxen.ai/api/ai/models/flux-2-dev"
    assert req.get_header("Authorization") == "Bearer k123"


def test_top_level_fields():
    c, _ = client(
        200,
        {
            "cost_per_image": 0.01,
            "cost_per_second": 0.5,
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        },
    )
    p = c.get_prices("m")
    assert (p.cost_per_image, p.cost_per_second) == (0.01, 0.5)
    assert (p.input_cost_per_token, p.output_cost_per_token) == (1e-6, 2e-6)


def test_fields_under_data_and_model():
    c, _ = client(200, {"data": {"cost_per_second": 0.25}})
    assert c.get_prices("m").cost_per_second == 0.25
    c, _ = client(200, {"model": {"cost_per_image": 0.02}})
    assert c.get_prices("m").cost_per_image == 0.02


def test_invalid_values_become_none():
    c, _ = client(
        200,
        {"cost_per_image": True, "cost_per_second": -1, "input_cost_per_token": "0.1"},
    )
    p = c.get_prices("m")
    assert p.cost_per_image is None
    assert p.cost_per_second is None
    assert p.input_cost_per_token is None
    assert p.output_cost_per_token is None


def test_nan_and_inf_become_none():
    c, _ = client(200, b'{"cost_per_image": NaN, "cost_per_second": Infinity}')
    p = c.get_prices("m")
    assert p.cost_per_image is None and p.cost_per_second is None


def test_raw_is_kept():
    c, _ = client(200, {"cost_per_image": 0.01, "extra": 1})
    assert c.get_prices("m").raw["extra"] == 1


def test_401_raises():
    c, _ = client(401, {"error": "nope"})
    with pytest.raises(OxenError, match="401"):
        c.get_prices("m")


def test_non_json_raises():
    c, _ = client(200, b"<html>")
    with pytest.raises(OxenError):
        c.get_prices("m")


def test_fields_under_pricing_live_shape():
    # Shape returned by GET /ai/models/flux-2-dev on 2026-11-01.
    body = {
        "id": "flux-2-dev",
        "fine_tuning": {"cost_per_second": 0.002772},
        "pricing": {"cost_per_image": 0.014, "method": "per_image"},
    }
    c, _ = client(200, body)
    p = c.get_prices("flux-2-dev")
    assert p.cost_per_image == 0.014
    assert p.cost_per_second is None
