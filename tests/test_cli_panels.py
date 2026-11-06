"""Tests for the panels subcommand, with a fake transport."""

import base64
import json
import shutil
from pathlib import Path

import pytest

from toolbox_tales import cli
from toolbox_tales.cli import main
from toolbox_tales.oxen import OxenClient

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
HEADER = "timestamp,model,kind,quantity,estimated_usd,actual_usd,output\n"


class Fake:
    def __init__(self, fail_nth=None):
        self.generates = 0
        self.total = 0
        self.fail_nth = fail_nth

    def __call__(self, request):
        self.total += 1
        if request.full_url.endswith("/ai/images/generate"):
            self.generates += 1
            if self.generates == self.fail_nth:
                return 500, b"x"
            return 200, json.dumps(
                {"images": [{"b64_json": base64.b64encode(PNG).decode()}]}
            ).encode()
        return 200, json.dumps({"cost_per_image": 0.5}).encode()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "spend.toml").write_text(
        "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
        '[models]\nimage = "flux-2-dev"\nvideo = ""\n[script]\nmodel = ""\n'
    )
    ep = tmp_path / "ep"
    ep.mkdir()
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, ep / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OXEN_API_KEY", "k")
    return tmp_path


def factory(fake):
    return lambda key: OxenClient(key, transport=fake)


def test_generates_and_exits_0(repo, capsys):
    fake = Fake()
    assert main(["panels", "ep/episode.yaml", "--yes"], client_factory=factory(fake)) == 0
    out = capsys.readouterr().out
    assert "generated panel 1 -> " in out and "p1.png" in out
    assert "generated 5, skipped 0" in out
    assert fake.generates == 5
    ledger = (repo / "ledger" / "spend.csv").read_text().splitlines()
    assert len(ledger) == 6


def test_panel_flag(repo):
    fake = Fake()
    assert (
        main(["panels", "ep/episode.yaml", "--panel", "2", "--yes"], client_factory=factory(fake))
        == 0
    )
    assert fake.generates == 1


def test_panel_out_of_range_exit_1(repo, capsys):
    fake = Fake()
    assert (
        main(["panels", "ep/episode.yaml", "--panel", "0", "--yes"], client_factory=factory(fake))
        == 1
    )
    assert "--panel 0" in capsys.readouterr().out
    assert fake.total == 0


def test_without_yes_exit_4(repo, capsys):
    fake = Fake()
    assert main(["panels", "ep/episode.yaml"], client_factory=factory(fake)) == 4
    assert "spend: unconfirmed" in capsys.readouterr().out
    assert fake.generates == 0


def test_failure_exit_1(repo, capsys):
    fake = Fake(fail_nth=2)
    assert main(["panels", "ep/episode.yaml", "--yes"], client_factory=factory(fake)) == 1
    out = capsys.readouterr().out
    assert "panels: panel 2:" in out
    assert "generated 4, skipped 0" in out


def test_missing_key_exit_2(repo, monkeypatch, capsys):
    monkeypatch.delenv("OXEN_API_KEY", raising=False)
    monkeypatch.setattr(
        cli, "require_key", lambda name: (_ for _ in ()).throw(cli.MissingKeyError("no key"))
    )
    assert main(["panels", "ep/episode.yaml", "--yes"], client_factory=factory(Fake())) == 2


def test_bad_ledger_exit_1(repo, capsys):
    (repo / "ledger").mkdir()
    (repo / "ledger" / "spend.csv").write_text("bad\n")
    assert main(["panels", "ep/episode.yaml", "--yes"], client_factory=factory(Fake())) == 1
    assert "ledger:" in capsys.readouterr().out


def test_nothing_to_do_exit_0(repo, capsys):
    for n in range(1, 6):
        p = repo / f"ep/media/fixture/p{n}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG)
    fake = Fake()
    assert main(["panels", "ep/episode.yaml"], client_factory=factory(fake)) == 0
    assert "generated 0, skipped 5" in capsys.readouterr().out
    assert fake.total == 0
