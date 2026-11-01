"""Tests for the price and dry-run commands, with a fake client."""

import shutil
from pathlib import Path

import pytest

from toolbox_tales.cli import main
from toolbox_tales.oxen import OxenClient, OxenError, Prices

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = "timestamp,model,kind,quantity,estimated_usd,actual_usd,output\n"


def P(**kw):
    base = dict(
        cost_per_image=None,
        cost_per_second=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw={},
    )
    base.update(kw)
    return Prices(**base)


class FakeClient:
    def __init__(self, table):
        self.table, self.calls = table, []

    def get_prices(self, model_id):
        self.calls.append(model_id)
        v = self.table[model_id]
        if isinstance(v, Exception):
            raise v
        return v


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "spend.toml").write_text(
        "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
        '[models]\nimage = "img"\nvideo = ""\n'
        '[script]\nmodel = ""\ninput_usd_per_mtok = 0.0\noutput_usd_per_mtok = 0.0\n'
        "est_input_tokens = 4000\nest_output_tokens = 1500\n"
    )
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "spend.csv").write_text(
        HEADER + "2026-11-01T00:00:00+00:00,img,image,1,5.0,,x.png\n"
    )
    ep = tmp_path / "ep"
    shutil.copytree(FIXTURES, ep)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OXEN_API_KEY", "k")
    return tmp_path


def factory(client):
    return lambda key: client


def test_price_output(repo, capsys):
    c = FakeClient({"img": P(cost_per_image=0.02), "none": P()})
    rc = main(["price", "img", "none"], client_factory=factory(c))
    out = capsys.readouterr().out
    assert rc == 0
    assert "img\n  cost_per_image: 0.02\n" in out
    assert "none\n  unpriced\n" in out
    assert "remaining: $20.00 of $25.00" in out
    assert c.calls == ["img", "none"]


def test_price_missing_key_exits_2(repo, monkeypatch, capsys):
    monkeypatch.delenv("OXEN_API_KEY", raising=False)
    rc = main(["price", "img"], client_factory=factory(FakeClient({})))
    assert rc == 2
    assert "missing OXEN_API_KEY: set it in .env or the environment" in capsys.readouterr().out


def test_price_oxen_error_exits_1(repo, capsys):
    c = FakeClient({"img": OxenError("HTTP 401")})
    rc = main(["price", "img"], client_factory=factory(c))
    assert rc == 1
    assert "401" in capsys.readouterr().out


def test_dry_run_counts_only_missing_panels(repo, capsys):
    media = repo / "ep" / "media" / "fixture"
    media.mkdir(parents=True)
    (media / "p1.png").write_bytes(b"x")
    (media / "p2.png").write_bytes(b"x")
    c = FakeClient({"img": P(cost_per_image=0.5)})
    rc = main(["dry-run", "ep/episode.yaml"], client_factory=factory(c))
    out = capsys.readouterr().out
    assert rc == 0
    assert "panels: 3 x img = $1.50" in out
    assert "shot: 5.0 s x" in out and "unpriced" in out
    assert "script:" in out
    assert out.strip().splitlines()[-1] == "total: $1.50 (remaining $18.50)"


def test_dry_run_unset_video_model_unpriced(repo, capsys):
    c = FakeClient({"img": P(cost_per_image=0.5)})
    main(["dry-run", "ep/episode.yaml"], client_factory=factory(c))
    shot = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("shot:")][0]
    assert shot.endswith("= unpriced")
    assert c.calls == ["img"]


def test_dry_run_existing_shot_costs_zero_seconds(repo, capsys):
    media = repo / "ep" / "media" / "fixture"
    media.mkdir(parents=True)
    (media / "arrival.mp4").write_bytes(b"x")
    (repo / "config" / "spend.toml").write_text(
        (repo / "config" / "spend.toml").read_text().replace('video = ""', 'video = "vid"')
    )
    c = FakeClient({"img": P(cost_per_image=0.5), "vid": P(cost_per_second=0.1)})
    main(["dry-run", "ep/episode.yaml"], client_factory=factory(c))
    out = capsys.readouterr().out
    assert "shot: 0.0 s x vid = $0.00" in out


def test_dry_run_script_priced(repo, capsys):
    toml = repo / "config" / "spend.toml"
    toml.write_text(
        toml.read_text()
        .replace('model = ""', 'model = "claude-x"')
        .replace("input_usd_per_mtok = 0.0", "input_usd_per_mtok = 3.0")
        .replace("output_usd_per_mtok = 0.0", "output_usd_per_mtok = 15.0")
    )
    c = FakeClient({"img": P(cost_per_image=0.5)})
    main(["dry-run", "ep/episode.yaml"], client_factory=factory(c))
    out = capsys.readouterr().out
    # 4000 * 3/1e6 + 1500 * 15/1e6 = 0.012 + 0.0225
    assert "script: claude-x = $0.03" in out


def test_dry_run_invalid_manifest_exits_1(repo, capsys):
    (repo / "ep" / "episode.yaml").write_text("cast_sheet: cast.yaml\n")
    c = FakeClient({})
    rc = main(["dry-run", "ep/episode.yaml"], client_factory=factory(c))
    assert rc == 1
    assert c.calls == []
    assert capsys.readouterr().out.strip()


def test_dry_run_missing_key_exits_2(repo, monkeypatch, capsys):
    monkeypatch.delenv("OXEN_API_KEY", raising=False)
    rc = main(["dry-run", "ep/episode.yaml"], client_factory=factory(FakeClient({})))
    assert rc == 2


def test_dry_run_issues_only_get_requests(repo, monkeypatch, capsys):
    seen = []

    def transport(request):
        seen.append(request.get_method())
        return 200, b'{"cost_per_image": 0.5}'

    monkeypatch.setattr(
        "toolbox_tales.cli.OxenClient",
        lambda key: OxenClient(key, transport=transport),
    )
    rc = main(["dry-run", "ep/episode.yaml"])
    assert rc == 0
    assert seen and set(seen) == {"GET"}
    assert not (repo / "ledger" / "spend.csv").read_text().count("\n") > 2
