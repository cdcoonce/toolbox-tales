"""Tests for the script subcommand. Offline: write_script gets a fake client."""

import functools
import shutil
from pathlib import Path

import anthropic
import httpx2
import pytest
from test_script import GOTCHA, FakeClient, reply

from toolbox_tales import cli
from toolbox_tales.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SPEND = Path(__file__).parent.parent / "config" / "spend.toml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    shutil.copy(REAL_SPEND, tmp_path / "config" / "spend.toml")
    shutil.copy(FIXTURES / "cast.yaml", tmp_path / "cast.yaml")
    (tmp_path / "gotcha.txt").write_text(GOTCHA, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def patch_client(monkeypatch, client):
    real = cli.script_mod.write_script
    monkeypatch.setattr(
        cli.script_mod,
        "write_script",
        functools.partial(real, client_factory=lambda: client),
    )


def args(*extra, tool="detector-teeth-check"):
    return [
        "script",
        "--gotcha",
        "gotcha.txt",
        "--tool",
        tool,
        "--cast",
        "cast.yaml",
        "--out",
        "ep/episode.yaml",
        *extra,
    ]


def test_written_prints_path_and_cost_and_exits_0(repo, monkeypatch, capsys):
    patch_client(monkeypatch, FakeClient(reply()))
    assert main(args("--yes")) == 0
    out = capsys.readouterr().out.strip()
    assert out == "written ep/episode.yaml (cost $0.1120)"
    assert (repo / "ep" / "episode.yaml").exists()
    assert main(["validate", "ep/episode.yaml"]) == 1  # runtime is unknown until voice


def test_rejected_exits_2_and_writes_nothing(repo, monkeypatch, capsys):
    patch_client(monkeypatch, FakeClient(reply("not_holding.json")))
    assert main(args("--yes")) == 2
    out = capsys.readouterr().out
    assert out.startswith("rejected: A glued joint")
    assert not (repo / "ep").exists()


@pytest.mark.parametrize("name", ["seven_panels.json", "long_overlay.json", "short.json"])
def test_draft_exits_1_and_leaves_no_manifest(repo, monkeypatch, capsys, name):
    patch_client(monkeypatch, FakeClient(reply(name)))
    assert main(args("--yes")) == 1
    out = capsys.readouterr().out
    assert "draft ep/episode.yaml.draft" in out
    assert (repo / "ep" / "episode.yaml.draft").exists()
    assert not (repo / "ep" / "episode.yaml").exists()


def test_refusal_exits_3_and_appends_one_ledger_row(repo, monkeypatch, capsys):
    client = FakeClient(reply(stop_reason="refusal", category="cyber", content=[]))
    patch_client(monkeypatch, client)
    assert main(args("--yes")) == 3
    assert "cyber" in capsys.readouterr().out
    lines = (repo / "ledger" / "spend.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # header plus one row


def test_without_yes_exits_4_with_zero_calls(repo, monkeypatch, capsys):
    client = FakeClient(reply())
    patch_client(monkeypatch, client)
    assert main(args()) == 4
    assert capsys.readouterr().out.startswith("spend: unconfirmed")
    assert client.calls == []


def test_unknown_tool_exits_1_with_zero_calls(repo, monkeypatch, capsys):
    client = FakeClient(reply())
    patch_client(monkeypatch, client)
    assert main(args("--yes", tool="nope")) == 1
    assert "no cast member plays tool 'nope'" in capsys.readouterr().out
    assert client.calls == []


def test_existing_out_needs_force(repo, monkeypatch, capsys):
    (repo / "ep").mkdir()
    (repo / "ep" / "episode.yaml").write_text("old", encoding="utf-8")
    client = FakeClient(reply())
    patch_client(monkeypatch, client)
    assert main(args("--yes")) == 1
    assert "pass --force" in capsys.readouterr().out
    assert client.calls == []
    assert main(args("--yes", "--force")) == 0


def test_bad_ledger_exits_1(repo, monkeypatch, capsys):
    (repo / "ledger").mkdir()
    (repo / "ledger" / "spend.csv").write_text("garbage\n", encoding="utf-8")
    client = FakeClient(reply())
    patch_client(monkeypatch, client)
    assert main(args("--yes")) == 1
    assert capsys.readouterr().out.startswith("ledger:")
    assert client.calls == []


def test_bad_config_exits_1(repo, monkeypatch, capsys):
    (repo / "config" / "spend.toml").write_text("per_call_cap_usd = 'x'\n", encoding="utf-8")
    patch_client(monkeypatch, FakeClient(reply()))
    assert main(args("--yes")) == 1
    assert capsys.readouterr().out.startswith("config:")


class RaisingClient:
    def __init__(self, exc):
        self.beta = type("B", (), {"messages": self})()
        self.exc = exc

    def create(self, **kwargs):
        raise self.exc


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.APIConnectionError(request=_request()),
        anthropic.APIStatusError(
            "overloaded", response=httpx2.Response(529, request=_request()), body=None
        ),
    ],
)
def test_api_errors_exit_1_with_prefix(repo, monkeypatch, capsys, exc):
    patch_client(monkeypatch, RaisingClient(exc))
    assert main(args("--yes")) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"script: API error: {type(exc).__name__}: ")


def test_dry_run_prices_the_script_line(repo, monkeypatch, capsys):
    shutil.copytree(FIXTURES, repo / "ep")
    monkeypatch.setenv("OXEN_API_KEY", "k")
    prices = type(
        "P",
        (),
        dict(cost_per_image=0.5, cost_per_second=0.1, input_cost_per_token=None,
             output_cost_per_token=None),
    )()
    fake = type("C", (), {"get_prices": lambda self, m: prices})()
    assert main(["dry-run", "ep/episode.yaml"], client_factory=lambda k: fake) == 0
    assert "script: claude-opus-5-5 = $0.18" in capsys.readouterr().out
