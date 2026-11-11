"""Tests for cast candidates and approval, with a fake transport. Never calls the real API."""

import base64
import json
import shutil
import urllib.request
from pathlib import Path

import pytest
import yaml

from toolbox_tales.cast import CastError, approve, generate_candidates
from toolbox_tales.ledger import Ledger
from toolbox_tales.manifest import validate
from toolbox_tales.oxen import OxenClient
from toolbox_tales.spend import load_config

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
SPEND = (
    "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
    '[models]\nimage = "flux-2-dev"\nvideo = ""\n'
    '[script]\nmodel = ""\n'
)


class Fake:
    def __init__(self, per_call=None, payload=PNG):
        self.calls = []
        self.per_call = per_call or {}  # nth generate call (1-based) -> (status, bytes)
        self.payload = payload

    @property
    def generates(self):
        return [c for c in self.calls if c[0].endswith("/ai/images/generate")]

    def __call__(self, request: urllib.request.Request):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.full_url, body))
        if request.full_url.endswith("/ai/images/generate"):
            n = len(self.generates)
            if n in self.per_call:
                return self.per_call[n]
            img = {"b64_json": base64.b64encode(self.payload).decode()}
            return 200, json.dumps({"images": [img]}).encode()
        return 200, json.dumps({"cost_per_image": 0.5}).encode()


def make(tmp_path, spend=SPEND):
    cast = tmp_path / "cast" / "cast.yaml"
    cast.parent.mkdir()
    shutil.copy(FIXTURES / "cast.yaml", cast)
    cfg = tmp_path / "spend.toml"
    cfg.write_text(spend)
    return cast, cfg


def gen(tmp_path, fake, name="Teeth", n=3, confirmed=True, spend=SPEND):
    cast, cfg = (
        make(tmp_path, spend)
        if not (tmp_path / "cast").exists()
        else (
            tmp_path / "cast" / "cast.yaml",
            tmp_path / "spend.toml",
        )
    )
    ledger = Ledger(tmp_path / "ledger.csv")
    client = OxenClient("key9", transport=fake)
    result = generate_candidates(
        cast, name, n, client, confirmed, ledger=ledger, config=load_config(cfg)
    )
    return result, ledger, cast


def rows(ledger):
    return ledger.path.read_text().splitlines()[1:] if ledger.path.exists() else []


def test_generate_three(tmp_path):
    fake = Fake()
    result, ledger, cast = gen(tmp_path, fake, n=3)
    assert len(fake.generates) == 3
    d = cast.parent / "media/cast/Teeth"
    assert [p.name for p in result.written] == ["c01.png", "c02.png", "c03.png"]
    assert all((d / f"c0{i}.png").read_bytes() == PNG for i in (1, 2, 3))
    r = rows(ledger)
    assert len(r) == 3
    assert all(",image," in line for line in r)
    assert "media/cast/Teeth/c01.png" in r[0]
    sheet = yaml.safe_load(cast.read_text())
    expected = (
        f"{sheet['style_prompt']}. {sheet['characters'][0]['personality']} "
        "One character, full figure, centered, plain background, no text."
    )
    assert "Teeth" not in expected
    for _, body in fake.generates:
        assert body["aspect_ratio"] == "1:1"
        assert "input_image" not in body
        assert expected in json.dumps(body)
        assert body["prompt"] == expected


def test_numbering_never_overwrites(tmp_path):
    gen(tmp_path, Fake(), n=3)
    cast = tmp_path / "cast" / "cast.yaml"
    d = cast.parent / "media/cast/Teeth"
    result, _, _ = gen(tmp_path, Fake(payload=PNG2), n=2)
    assert [p.name for p in result.written] == ["c04.png", "c05.png"]
    assert all((d / f"c0{i}.png").read_bytes() == PNG for i in (1, 2, 3))
    assert (d / "c04.png").read_bytes() == PNG2


def test_unconfirmed_refused(tmp_path):
    fake = Fake()
    result, ledger, _ = gen(tmp_path, fake, confirmed=False)
    assert result.refused == "unconfirmed"
    assert fake.generates == []
    assert rows(ledger) == []


def test_total_cap_fits_one(tmp_path):
    spend = SPEND.replace("total_cap_usd = 25.0", "total_cap_usd = 0.75")
    fake = Fake()
    result, _, _ = gen(tmp_path, fake, n=3, spend=spend)
    assert len(fake.generates) == 1
    assert len(result.written) == 1
    assert result.refused == "total-cap"


@pytest.mark.parametrize(
    ("name", "n"), [("Nobody", 3), ("../x", 3), ("a/b", 3), ("Teeth", 0), ("Teeth", 9)]
)
def test_bad_input_zero_requests(tmp_path, name, n):
    fake = Fake()
    with pytest.raises(CastError):
        gen(tmp_path, fake, name=name, n=n)
    assert fake.calls == []


def test_non_2xx_is_failure_without_ledger_row(tmp_path):
    fake = Fake(per_call={2: (500, b"boom")})
    result, ledger, cast = gen(tmp_path, fake, n=3)
    assert len(result.failures) == 1
    assert len(result.written) == 2
    assert len(rows(ledger)) == 2
    assert not (cast.parent / "media/cast/Teeth/c02.png").exists()


def test_2xx_non_image_logged_not_written(tmp_path):
    fake = Fake(payload=b"not an image at all")
    result, ledger, cast = gen(tmp_path, fake, n=1)
    assert result.failures == ("candidate 01: response is not an image",)
    assert len(rows(ledger)) == 1
    assert rows(ledger)[0].endswith(",media/cast/Teeth/c01")
    assert not (cast.parent / "media/cast/Teeth/c01.png").exists()
    assert list((cast.parent / "media/cast/Teeth").glob("c01*")) == []


def test_generate_leaves_sheet_identical(tmp_path):
    make(tmp_path)
    before = (tmp_path / "cast" / "cast.yaml").read_bytes()
    gen(tmp_path, Fake(), n=2)
    assert (tmp_path / "cast" / "cast.yaml").read_bytes() == before


def seeded(tmp_path):
    gen(tmp_path, Fake(), n=3)
    d = tmp_path / "cast/media/cast/Teeth"
    (d / "c02.png").write_bytes(PNG2)
    return tmp_path / "cast" / "cast.yaml", d


@pytest.mark.parametrize("candidate", ["3", "c03.png"])
def test_approve_by_number_and_name(tmp_path, candidate):
    cast, _ = seeded(tmp_path)
    before = yaml.safe_load(cast.read_text())
    ref = approve(cast, "Teeth", candidate)
    assert ref == cast.parent / "refs/Teeth.png"
    assert ref.read_bytes() == PNG
    after = yaml.safe_load(cast.read_text())
    assert after["characters"][0]["reference_image"] == "refs/Teeth.png"
    after["characters"][0]["reference_image"] = before["characters"][0]["reference_image"]
    assert after == before
    episode = yaml.safe_load((FIXTURES / "episode.yaml").read_text())
    sheet = yaml.safe_load(cast.read_text())
    assert validate(episode, sheet) == []


@pytest.mark.parametrize("candidate", ["../c01.png", "sub/c01.png", "9", "0", "notes.png"])
def test_approve_rejects_bad_candidates(tmp_path, candidate):
    cast, d = seeded(tmp_path)
    (d / "notes.png").write_bytes(b"plain text")
    before = cast.read_bytes()
    with pytest.raises(CastError):
        approve(cast, "Teeth", candidate)
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


def test_approve_twice_overwrites(tmp_path):
    cast, _ = seeded(tmp_path)
    approve(cast, "Teeth", "1")
    ref = approve(cast, "Teeth", "2")
    assert ref.read_bytes() == PNG2


def edit_sheet(cast, fn):
    sheet = yaml.safe_load(cast.read_text())
    fn(sheet)
    cast.write_text(yaml.safe_dump(sheet, sort_keys=False))


def test_unsafe_name_in_sheet_refused_by_generate(tmp_path):
    # The sheet lookup would find this name, so only the name pattern can refuse it.
    cast, _ = make(tmp_path)
    edit_sheet(cast, lambda s: s["characters"][0].update(name="Bad Name!"))
    fake = Fake()
    with pytest.raises(CastError):
        gen(tmp_path, fake, name="Bad Name!", n=1)
    assert fake.calls == []
    assert not (cast.parent / "media").exists()


def test_unsafe_name_in_sheet_refused_by_approve(tmp_path):
    cast, _ = make(tmp_path)
    edit_sheet(cast, lambda s: s["characters"][0].update(name="Bad Name!"))
    d = cast.parent / "media/cast/Bad Name!"
    d.mkdir(parents=True)
    (d / "c01.png").write_bytes(PNG)
    before = cast.read_bytes()
    with pytest.raises(CastError):
        approve(cast, "Bad Name!", "1")
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


@pytest.mark.parametrize("dup", ["Teeth", "Gauge"])
def test_duplicate_names_refused_by_generate(tmp_path, dup):
    cast, _ = make(tmp_path)
    edit_sheet(cast, lambda s: s["characters"][2].update(name=dup))
    fake = Fake()
    with pytest.raises(CastError, match="duplicate"):
        gen(tmp_path, fake, name="Teeth", n=1)
    assert fake.calls == []
    assert not (cast.parent / "media").exists()


@pytest.mark.parametrize("dup", ["Teeth", "Gauge"])
def test_duplicate_names_refused_by_approve(tmp_path, dup):
    cast, _ = seeded(tmp_path)
    edit_sheet(cast, lambda s: s["characters"][2].update(name=dup))
    before = cast.read_bytes()
    with pytest.raises(CastError, match="duplicate"):
        approve(cast, "Teeth", "1")
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


@pytest.mark.parametrize(
    ("index", "field"), [(0, "personality"), (1, "voice"), (2, "tool"), (1, "reference_image")]
)
def test_approve_validates_whole_sheet_before_writing(tmp_path, index, field):
    cast, _ = seeded(tmp_path)
    edit_sheet(cast, lambda s: s["characters"][index].update({field: 7}))
    before = cast.read_bytes()
    with pytest.raises(CastError):
        approve(cast, "Teeth", "1")
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


@pytest.mark.parametrize("candidate", ["c07.png", "7"])
def test_approve_refuses_symlinked_candidate(tmp_path, candidate):
    cast, d = seeded(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG2)
    (d / "c07.png").symlink_to(outside)
    before = cast.read_bytes()
    with pytest.raises(CastError):
        approve(cast, "Teeth", candidate)
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


def test_approve_png_removes_stale_ref_of_other_suffix(tmp_path):
    cast, _ = seeded(tmp_path)
    refs = cast.parent / "refs"
    refs.mkdir()
    (refs / "Teeth.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 8)
    (refs / "Gauge.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 8)
    approve(cast, "Teeth", "1")
    assert sorted(p.name for p in refs.iterdir()) == ["Gauge.jpg", "Teeth.png"]


@pytest.mark.parametrize("candidate", ["c08.png", "8"])
def test_approve_refuses_symlink_inside_candidate_dir(tmp_path, candidate):
    # Containment alone would accept this link, so only the symlink check refuses it.
    cast, d = seeded(tmp_path)
    (d / "c08.png").symlink_to(d / "c01.png")
    before = cast.read_bytes()
    with pytest.raises(CastError):
        approve(cast, "Teeth", candidate)
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()


@pytest.mark.parametrize(
    ("payload", "ext"), [(WEBP, ".webp"), (JPEG, ".jpg"), (PNG, ".png")]
)
def test_extension_from_sniffed_kind(tmp_path, payload, ext):
    result, ledger, cast = gen(tmp_path, Fake(payload=payload), n=1)
    d = cast.parent / "media/cast/Teeth"
    assert [p.name for p in result.written] == [f"c01{ext}"]
    assert (d / f"c01{ext}").read_bytes() == payload
    assert f"media/cast/Teeth/c01{ext}" in rows(ledger)[0]


def test_numbering_counts_any_extension(tmp_path):
    make(tmp_path)
    d = tmp_path / "cast/media/cast/Teeth"
    d.mkdir(parents=True)
    (d / "c01.png").write_bytes(PNG)
    (d / "c02.webp").write_bytes(WEBP)
    result, _, _ = gen(tmp_path, Fake(payload=WEBP), n=1)
    assert [p.name for p in result.written] == ["c03.webp"]


def test_non_image_ledger_output_has_no_extension(tmp_path):
    _, ledger, cast = gen(tmp_path, Fake(payload=b"junk"), n=1)
    assert rows(ledger)[0].endswith(",media/cast/Teeth/c01")
    assert not (cast.parent / "media/cast/Teeth").exists() or not any(
        (cast.parent / "media/cast/Teeth").iterdir()
    )


def test_approve_webp_candidate_keeps_webp_suffix(tmp_path):
    gen(tmp_path, Fake(payload=WEBP), n=1)
    cast = tmp_path / "cast" / "cast.yaml"
    ref = approve(cast, "Teeth", "1")
    assert ref == cast.parent / "refs/Teeth.webp"
    assert ref.read_bytes() == WEBP
    assert yaml.safe_load(cast.read_text())["characters"][0]["reference_image"] == "refs/Teeth.webp"


def test_approve_refuses_suffix_kind_mismatch(tmp_path):
    cast, d = seeded(tmp_path)
    (d / "c09.png").write_bytes(WEBP)
    before = cast.read_bytes()
    with pytest.raises(CastError, match=r"\.webp"):
        approve(cast, "Teeth", "c09.png")
    assert cast.read_bytes() == before
    assert not (cast.parent / "refs").exists()
