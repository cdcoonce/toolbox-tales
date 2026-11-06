"""Tests for panel generation, with a fake transport. Never calls the real Oxen API."""

import base64
import json
import shutil
import urllib.request
from pathlib import Path

import pytest
import yaml
from smoke_gate import require_smoke

from toolbox_tales import render as render_mod
from toolbox_tales.ledger import Ledger
from toolbox_tales.oxen import OxenClient
from toolbox_tales.panels import PanelsError, generate_panels
from toolbox_tales.spend import load_config

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PRICE_BODY = {"cost_per_image": 0.5}
SPEND = (
    "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
    '[models]\nimage = "flux-2-dev"\nvideo = ""\n'
    '[script]\nmodel = ""\n'
)


class Fake:
    def __init__(self, image_status=200, prices=None, image_body=None, per_panel=None):
        self.calls = []  # (method, url, headers, body)
        self.image_status = image_status
        self.prices = PRICE_BODY if prices is None else prices
        self.image_body = image_body
        self.per_panel = per_panel or {}  # nth generate call (1-based) -> (status, bytes)

    @property
    def generates(self):
        return [c for c in self.calls if c[1].endswith("/ai/images/generate")]

    def __call__(self, request: urllib.request.Request):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), request.full_url, dict(request.headers), body))
        if request.full_url.endswith("/ai/images/generate"):
            n = len(self.generates)
            if n in self.per_panel:
                return self.per_panel[n]
            if self.image_body is not None:
                return self.image_status, json.dumps(self.image_body).encode()
            payload = {"images": [{"b64_json": base64.b64encode(PNG).decode()}]}
            return self.image_status, json.dumps(payload).encode()
        return 200, json.dumps(self.prices).encode()


def make(tmp_path, spend=SPEND, panels_section="", cast_edit=None):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    if cast_edit:
        cast = yaml.safe_load((tmp_path / "cast.yaml").read_text())
        cast_edit(cast)
        (tmp_path / "cast.yaml").write_text(yaml.safe_dump(cast))
    cfg = tmp_path / "spend.toml"
    cfg.write_text(spend + panels_section)
    return tmp_path / "episode.yaml", cfg


def run(tmp_path, fake, confirmed=True, panel=None, **kw):
    manifest, cfg = make(tmp_path, **kw)
    return run_made(tmp_path, fake, manifest, cfg, confirmed, panel)


def run_made(tmp_path, fake, manifest, cfg, confirmed=True, panel=None):
    ledger = Ledger(tmp_path / "ledger.csv")
    client = OxenClient("key9", transport=fake)
    result = generate_panels(
        manifest,
        client,
        confirmed=confirmed,
        panel=panel,
        ledger=ledger,
        config=load_config(cfg),
        config_path=cfg,
    )
    return result, ledger


def ledger_rows(ledger):
    if not ledger.path.exists():
        return []
    return ledger.path.read_text().splitlines()[1:]


def test_only_missing_panels_generated(tmp_path):
    manifest, cfg = make(tmp_path)
    for n in (1, 4):
        p = tmp_path / f"media/fixture/p{n}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"old%d" % n)
    fake = Fake()
    result, _ = run_made(tmp_path, fake, manifest, cfg)
    assert len(fake.generates) == 3
    assert result.skipped == 2
    assert result.generated == (2, 3, 5)
    assert (tmp_path / "media/fixture/p1.png").read_bytes() == b"old1"
    assert (tmp_path / "media/fixture/p4.png").read_bytes() == b"old4"
    assert (tmp_path / "media/fixture/p2.png").read_bytes() == PNG


def test_panel_flag_regenerates_one_even_if_exists(tmp_path):
    manifest, cfg = make(tmp_path)
    p = tmp_path / "media/fixture/p2.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"old")
    fake = Fake()
    result, _ = run_made(tmp_path, fake, manifest, cfg, panel=2)
    assert len(fake.generates) == 1
    assert result.generated == (2,)
    assert result.skipped == 0
    assert p.read_bytes() == PNG


@pytest.mark.parametrize("panel", [0, 6, -1])
def test_panel_out_of_range(tmp_path, panel):
    fake = Fake()
    with pytest.raises(PanelsError, match=f"--panel {panel}: episode has 5 panels"):
        run(tmp_path, fake, panel=panel)
    assert fake.calls == []


def test_one_ledger_row_per_success(tmp_path):
    fake = Fake()
    result, ledger = run(tmp_path, fake)
    rows = ledger_rows(ledger)
    assert len(rows) == 5
    cols = rows[0].split(",")
    assert cols[1:3] == ["flux-2-dev", "image"]
    assert float(cols[4]) == 0.5 and float(cols[5]) == 0.5
    assert [r.split(",")[6] for r in rows] == [f"media/fixture/p{n}.png" for n in range(1, 6)]
    assert result.generated == (1, 2, 3, 4, 5)


def test_unconfirmed_refused(tmp_path):
    fake = Fake()
    result, ledger = run(tmp_path, fake, confirmed=False)
    assert result.refused == "unconfirmed"
    assert fake.generates == []
    assert ledger_rows(ledger) == []


def test_total_cap_midrun(tmp_path):
    manifest, cfg = make(tmp_path)
    ledger = Ledger(tmp_path / "ledger.csv")
    ledger.path.write_text(
        "timestamp,model,kind,quantity,estimated_usd,actual_usd,output\n"
        "2026-11-01T00:00:00+00:00,flux-2-dev,image,1,24.0,,x.png\n"
    )
    fake = Fake()
    result, _ = run_made(tmp_path, fake, manifest, cfg)
    assert len(fake.generates) == 1
    assert result.refused == "total-cap"
    assert result.generated == (1,)
    assert (tmp_path / "media/fixture/p1.png").exists()
    assert not (tmp_path / "media/fixture/p2.png").exists()


def test_unpriced_refused(tmp_path):
    fake = Fake(prices={})
    result, _ = run(tmp_path, fake)
    assert result.refused == "unpriced"
    assert fake.generates == []


def _with_ref(tmp_path, ref="refs/teeth.png", create=True):
    def edit(cast):
        cast["characters"][0]["reference_image"] = ref

    if create and ref:
        r = tmp_path / ref
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_bytes(PNG)
    return edit


REFS_ON = '[panels]\nreferences = true\naspect_ratio = "16:9"\n'


def test_references_used(tmp_path):
    fake = Fake()
    run(tmp_path, fake, panels_section=REFS_ON, cast_edit=_with_ref(tmp_path))
    body = fake.generates[2][3]  # panel 3: only Teeth speaks
    assert len(body["input_image"]) == 1
    assert body["input_image"][0] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert "Teeth is @Image1." in body["prompt"]
    assert "Teeth:" not in body["prompt"]
    assert "blade guard" not in body["prompt"]
    # panel 1 has no Teeth: no reference
    assert "input_image" not in fake.generates[0][3]


def test_references_off_describes(tmp_path):
    fake = Fake()
    run(tmp_path, fake, cast_edit=_with_ref(tmp_path))  # section absent
    body = fake.generates[2][3]
    assert "input_image" not in body
    assert (
        "\nTeeth: A blade guard who insists on being tested with a scrap board." in body["prompt"]
    )


def test_references_false_explicit(tmp_path):
    fake = Fake()
    run(
        tmp_path,
        fake,
        panels_section="[panels]\nreferences = false\n",
        cast_edit=_with_ref(tmp_path),
    )
    assert "input_image" not in fake.generates[2][3]


def test_empty_reference_image_described(tmp_path):
    fake = Fake()
    run(tmp_path, fake, panels_section=REFS_ON)
    body = fake.generates[2][3]
    assert "input_image" not in body
    assert "Teeth: A blade guard" in body["prompt"]


def test_missing_reference_fails_before_any_call(tmp_path):
    fake = Fake()
    with pytest.raises(PanelsError, match=r"panel 3: reference for Teeth"):
        run(
            tmp_path,
            fake,
            panels_section=REFS_ON,
            cast_edit=_with_ref(tmp_path, "refs/none.png", create=False),
        )
    assert fake.calls == []


def test_bad_reference_suffix(tmp_path):
    def edit(cast):
        (tmp_path / "t.gif").write_bytes(PNG)
        cast["characters"][0]["reference_image"] = "t.gif"

    fake = Fake()
    with pytest.raises(PanelsError, match="reference for Teeth"):
        run(tmp_path, fake, panels_section=REFS_ON, cast_edit=edit)
    assert fake.calls == []


def test_oversize_reference(tmp_path):
    def edit(cast):
        (tmp_path / "big.png").write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
        cast["characters"][0]["reference_image"] = "big.png"

    fake = Fake()
    with pytest.raises(PanelsError, match="reference for Teeth"):
        run(tmp_path, fake, panels_section=REFS_ON, cast_edit=edit)
    assert fake.calls == []


def test_multiple_speakers_prompt_order(tmp_path):
    def edit(cast):
        cast["characters"][1]["reference_image"] = "g.jpg"
        (tmp_path / "g.jpg").write_bytes(b"\xff\xd8\xff\x00")

    fake = Fake()
    run(tmp_path, fake, panels_section=REFS_ON, cast_edit=edit)
    body = fake.generates[3][3]  # panel 4: Teeth (described), Gauge (reference)
    style = "warm illustrated wood shop, soft lamp light, hand-inked outlines"
    assert body["prompt"] == (
        f"{style}. Teeth snaps a scrap board and the alarm finally rings"
        "\nGauge is @Image1."
        "\nTeeth: A blade guard who insists on being tested with a scrap board."
    )
    assert body["input_image"][0].startswith("data:image/jpeg;base64,")


def test_prompt_and_request_shape(tmp_path):
    fake = Fake()
    run(tmp_path, fake)
    method, url, headers, body = fake.generates[0]
    assert method == "POST"
    assert url.endswith("/ai/images/generate")
    assert headers["Authorization"] == "Bearer key9"
    assert body["response_format"] == "b64_json"
    assert body["aspect_ratio"] == "16:9"
    assert body["model"] == "flux-2-dev"
    style = "warm illustrated wood shop, soft lamp light, hand-inked outlines"
    assert body["prompt"].startswith(style + ". Foreman admires a freshly glued joint")


def test_config_type_errors(tmp_path):
    with pytest.raises(PanelsError, match="config"):
        run(tmp_path, Fake(), panels_section='[panels]\nreferences = "yes"\n')
    with pytest.raises(PanelsError, match="config"):
        run(tmp_path, Fake(), panels_section="[panels]\naspect_ratio = 3\n")


def test_non_2xx_is_failure_others_continue(tmp_path):
    fake = Fake(per_panel={2: (500, b"err")})
    result, ledger = run(tmp_path, fake)
    assert len(fake.generates) == 5
    assert result.generated == (1, 3, 4, 5)
    assert len(result.failures) == 1 and result.failures[0].startswith("panel 2:")
    assert len(ledger_rows(ledger)) == 4
    assert not (tmp_path / "media/fixture/p2.png").exists()


def test_non_image_bytes_billed_not_written(tmp_path):
    payload = {"images": [{"b64_json": base64.b64encode(b"hello").decode()}]}
    fake = Fake(image_body=payload)
    result, ledger = run(tmp_path, fake, panel=1)
    assert result.failures == ("panel 1: response is not an image",)
    assert len(ledger_rows(ledger)) == 1
    assert not (tmp_path / "media/fixture/p1.png").exists()
    assert result.generated == ()


def test_no_images_key_billed(tmp_path):
    fake = Fake(image_body={"model": "x"})
    result, ledger = run(tmp_path, fake, panel=1)
    assert len(result.failures) == 1 and result.failures[0].startswith("panel 1:")
    assert len(ledger_rows(ledger)) == 1


def test_escape_path_rejected(tmp_path):
    manifest, cfg = make(tmp_path)
    data = yaml.safe_load(manifest.read_text())
    data["panels"][1]["image"] = "../escape.png"
    manifest.write_text(yaml.safe_dump(data))
    fake = Fake()
    with pytest.raises(PanelsError, match="panel 2: image path escapes the manifest directory"):
        run_made(tmp_path, fake, manifest, cfg)
    assert fake.calls == []


def test_missing_image_key(tmp_path):
    manifest, cfg = make(tmp_path)
    data = yaml.safe_load(manifest.read_text())
    del data["panels"][0]["image"]
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(PanelsError, match="panel 1: no image path"):
        run_made(tmp_path, Fake(), manifest, cfg)


def test_invalid_manifest_raises(tmp_path):
    manifest, cfg = make(tmp_path)
    data = yaml.safe_load(manifest.read_text())
    data["panels"] = data["panels"][:2]
    manifest.write_text(yaml.safe_dump(data))
    fake = Fake()
    with pytest.raises(PanelsError):
        run_made(tmp_path, fake, manifest, cfg)
    assert fake.calls == []


def test_nothing_to_do_makes_no_call(tmp_path):
    manifest, cfg = make(tmp_path)
    for n in range(1, 6):
        p = tmp_path / f"media/fixture/p{n}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG)
    fake = Fake()
    result, _ = run_made(tmp_path, fake, manifest, cfg)
    assert fake.calls == []
    assert result.generated == () and result.skipped == 5


def test_manifest_untouched(tmp_path):
    manifest, cfg = make(tmp_path)
    before = manifest.read_bytes()
    run_made(tmp_path, Fake(), manifest, cfg)
    assert manifest.read_bytes() == before


def test_render_uses_generated_images(tmp_path):
    require_smoke()
    manifest, cfg = make(tmp_path)
    run_made(tmp_path, Fake(), manifest, cfg)
    plan = render_mod.build_plan(yaml.safe_load(manifest.read_text()), tmp_path)
    panels = [s for s in plan.segments if s.kind == "panel"]
    assert len(panels) == 5 and all(s.source is not None for s in panels)
