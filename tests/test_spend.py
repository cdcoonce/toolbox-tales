"""Tests for estimate and authorize."""

import pytest

from toolbox_tales.oxen import Prices
from toolbox_tales.spend import Estimate, SpendConfig, authorize, estimate, load_config


def prices(**kw):
    base = dict(
        cost_per_image=None,
        cost_per_second=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw={},
    )
    base.update(kw)
    return Prices(**base)


def cfg(per=3.0, total=25.0):
    return SpendConfig(per_call_cap_usd=per, total_cap_usd=total)


def est(cost, kind="image"):
    return Estimate(model="m", kind=kind, quantity=1, cost_usd=cost)


def test_estimate_image():
    e = estimate(prices(cost_per_image=0.5), "m", "image", 4)
    assert e.cost_usd == pytest.approx(2.0) and e.kind == "image" and e.quantity == 4


def test_estimate_video():
    assert estimate(prices(cost_per_second=0.1), "m", "video", 5).cost_usd == pytest.approx(0.5)


def test_estimate_text():
    p = prices(input_cost_per_token=1e-6, output_cost_per_token=2e-6)
    e = estimate(p, "m", "text", 1000, output_tokens=500)
    assert e.cost_usd == pytest.approx(0.001 + 0.001)


@pytest.mark.parametrize(
    "kind,p",
    [
        ("image", prices(cost_per_second=1.0)),
        ("video", prices(cost_per_image=1.0)),
        ("text", prices(input_cost_per_token=1e-6)),
        ("text", prices(output_cost_per_token=1e-6)),
    ],
)
def test_missing_needed_price_is_none(kind, p):
    assert estimate(p, "m", kind, 3, output_tokens=3).cost_usd is None


def test_zero_price_is_zero_not_none():
    assert estimate(prices(cost_per_image=0.0), "m", "image", 3).cost_usd == 0.0


def test_unpriced():
    d = authorize(est(None), True, 0.0, cfg())
    assert (d.allowed, d.reason) == (False, "unpriced")


def test_per_call_cap():
    d = authorize(est(3.01), True, 0.0, cfg())
    assert (d.allowed, d.reason) == (False, "per-call-cap")


def test_per_call_cap_equal_allowed():
    assert authorize(est(3.0), True, 0.0, cfg()).reason == "ok"


def test_total_cap():
    d = authorize(est(2.0), True, 23.01, cfg())
    assert (d.allowed, d.reason) == (False, "total-cap")


def test_total_cap_equal_allowed_and_over_refused():
    assert authorize(est(1.0), True, 24.0, cfg(per=3.0, total=25.0)).reason == "ok"
    assert authorize(est(1.01), True, 24.0, cfg(per=3.0, total=25.0)).reason == "total-cap"


def test_unconfirmed():
    d = authorize(est(1.0), False, 0.0, cfg())
    assert (d.allowed, d.reason) == (False, "unconfirmed")


def test_ok():
    d = authorize(est(1.0), True, 0.0, cfg())
    assert (d.allowed, d.reason) == (True, "ok")


def test_order_unpriced_before_unconfirmed():
    assert authorize(est(None), False, 0.0, cfg()).reason == "unpriced"


def test_order_per_call_before_unconfirmed():
    assert authorize(est(9.0), False, 0.0, cfg()).reason == "per-call-cap"


def test_order_per_call_before_total():
    assert authorize(est(9.0), True, 24.0, cfg()).reason == "per-call-cap"


def test_order_total_before_unconfirmed():
    assert authorize(est(2.0), False, 24.0, cfg()).reason == "total-cap"


def test_load_config(tmp_path):
    f = tmp_path / "spend.toml"
    f.write_text(
        'per_call_cap_usd = 1.5\ntotal_cap_usd = 10\n[models]\nimage = "a"\nvideo = "b"\n'
        '[script]\nmodel = "c"\ninput_usd_per_mtok = 3.0\noutput_usd_per_mtok = 15.0\n'
        "est_input_tokens = 100\nest_output_tokens = 50\n"
    )
    c = load_config(f)
    assert (c.per_call_cap_usd, c.total_cap_usd) == (1.5, 10.0)
    assert (c.image_model, c.video_model, c.script_model) == ("a", "b", "c")
    assert (c.script_input_usd_per_mtok, c.script_output_usd_per_mtok) == (3.0, 15.0)
    assert (c.est_input_tokens, c.est_output_tokens) == (100, 50)


def test_committed_config_loads():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    c = load_config(root / "config" / "spend.toml")
    assert (c.per_call_cap_usd, c.total_cap_usd) == (3.0, 25.0)
    assert c.image_model == "flux-2-dev"


def test_committed_ledger_is_header_only():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "ledger" / "spend.csv").read_text()
    assert text.strip() == "timestamp,model,kind,quantity,estimated_usd,actual_usd,output"
