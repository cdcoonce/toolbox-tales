"""Tests for the cast subcommands, with a fake transport."""

import base64
import json
import shutil
from pathlib import Path

import pytest
import yaml

from toolbox_tales.cli import main
from toolbox_tales.oxen import OxenClient

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class Fake:
    def __init__(self, fail_nth=None):
        self.generates = 0
        self.fail_nth = fail_nth

    def __call__(self, request):
        if request.full_url.endswith("/ai/images/generate"):
            self.generates += 1
            if self.generates == self.fail_nth:
                return 500, b"x"
            img = {"b64_json": base64.b64encode(PNG).decode()}
            return 200, json.dumps({"images": [img]}).encode()
        return 200, json.dumps({"cost_per_image": 0.5}).encode()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "spend.toml").write_text(
        "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
        '[models]\nimage = "flux-2-dev"\nvideo = ""\n[script]\nmodel = ""\n'
    )
    (tmp_path / "cast").mkdir()
    shutil.copy(FIXTURES / "cast.yaml", tmp_path / "cast" / "cast.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OXEN_API_KEY", "k")
    return tmp_path


def factory(fake):
    return lambda key: OxenClient(key, transport=fake)


def test_generate_exit_0(repo, capsys):
    fake = Fake()
    assert main(["cast", "generate", "Teeth", "--n", "2", "--yes"], factory(fake)) == 0
    out = capsys.readouterr().out
    assert out.count("wrote ") == 2 and "c01.png" in out
    assert fake.generates == 2
    assert len((repo / "ledger" / "spend.csv").read_text().splitlines()) == 3


def test_without_yes_exit_4(repo, capsys):
    fake = Fake()
    assert main(["cast", "generate", "Teeth"], factory(fake)) == 4
    assert "spend: unconfirmed" in capsys.readouterr().out
    assert fake.generates == 0


def test_failure_exit_1(repo, capsys):
    fake = Fake(fail_nth=2)
    assert main(["cast", "generate", "Teeth", "--n", "3", "--yes"], factory(fake)) == 1
    out = capsys.readouterr().out
    assert out.count("wrote ") == 2 and "cast: candidate 02" in out


def test_missing_key_exit_2(repo, monkeypatch, capsys):
    monkeypatch.delenv("OXEN_API_KEY")
    assert main(["cast", "generate", "Teeth", "--yes"], factory(Fake())) == 2


def test_unknown_name_exit_1(repo, capsys):
    assert main(["cast", "generate", "Nobody", "--yes"], factory(Fake())) == 1
    assert "no character named" in capsys.readouterr().out


def test_approve_needs_no_key(repo, monkeypatch, capsys):
    main(["cast", "generate", "Teeth", "--n", "1", "--yes"], factory(Fake()))
    monkeypatch.delenv("OXEN_API_KEY")
    assert main(["cast", "approve", "Teeth", "1"]) == 0
    assert "approved Teeth -> " in capsys.readouterr().out
    sheet = yaml.safe_load((repo / "cast" / "cast.yaml").read_text())
    assert sheet["characters"][0]["reference_image"] == "refs/Teeth.png"
    assert (repo / "cast" / "refs" / "Teeth.png").read_bytes() == PNG


def test_approve_bad_candidate_exit_1(repo, capsys):
    assert main(["cast", "approve", "Teeth", "7"]) == 1
