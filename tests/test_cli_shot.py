"""Tests for the shot subcommand, with a fake transport and no real sleeping."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from toolbox_tales import cli, shot
from toolbox_tales.cli import main
from toolbox_tales.oxen import OxenClient

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
URL = "https://signed.example/r.mp4"


class Fake:
    def __init__(self, statuses=("succeeded",), enqueue_status=200):
        self.statuses = list(statuses)
        self.enqueue_status = enqueue_status
        self.enqueues = 0
        self.total = 0

    def __call__(self, request):
        self.total += 1
        url = request.full_url
        if "/ai/models/" in url:
            return 200, json.dumps({"cost_per_second": 0.1}).encode()
        if url.endswith("/ai/queue"):
            self.enqueues += 1
            if self.enqueue_status != 200:
                return self.enqueue_status, b"x"
            return 200, json.dumps({"generations": [{"generation_id": "g9"}]}).encode()
        if "/ai/queue/" in url:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            body = {"status": status, "result_url": URL, "error_message": "boom"}
            return 200, json.dumps(body).encode()
        return 200, MP4


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "spend.toml").write_text(
        "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
        '[models]\nimage = "flux-2-dev"\nvideo = "kling-video-o3-omni"\n[script]\nmodel = ""\n'
    )
    ep = tmp_path / "ep"
    ep.mkdir()
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, ep / name)
    (ep / "media/fixture").mkdir(parents=True)
    (ep / "media/fixture/p3.png").write_bytes(PNG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OXEN_API_KEY", "k")
    monkeypatch.setattr(shot.time, "sleep", lambda s: None)
    monkeypatch.setattr(shot, "ffprobe_duration", lambda p: 5.0)
    return tmp_path


def factory(fake):
    return lambda key: OxenClient(key, transport=fake)


def run(fake, *extra):
    return main(["shot", "ep/episode.yaml", *extra], client_factory=factory(fake))


def test_success_exit_0(repo, capsys):
    assert run(Fake(), "--yes") == 0
    out = capsys.readouterr().out
    assert "submitted g9 (kling-video-o3-omni)" in out
    assert "succeeded " in out and "(5.0 s, kling-video-o3-omni)" in out
    assert (repo / "ep/media/fixture/arrival.mp4").exists()


def test_exists_exit_0(repo, capsys):
    (repo / "ep/media/fixture/arrival.mp4").write_bytes(MP4)
    fake = Fake()
    assert run(fake, "--yes") == 0
    assert capsys.readouterr().out.startswith("exists ")
    assert fake.total == 0


def test_without_yes_exit_4(repo, capsys):
    fake = Fake()
    assert run(fake) == 4
    assert "spend: unconfirmed" in capsys.readouterr().out
    assert fake.enqueues == 0


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_cancelled_exit_1(repo, capsys, status):
    assert run(Fake(statuses=(status,)), "--yes") == 1
    assert f"{status}: boom" in capsys.readouterr().out


def test_timeout_exit_1(repo, capsys, monkeypatch):
    ticks = iter(range(0, 100000, 400))
    monkeypatch.setattr(shot.time, "monotonic", lambda: next(ticks))
    assert run(Fake(statuses=("processing",)), "--yes") == 1
    assert "timeout: " in capsys.readouterr().out


def test_enqueue_failure_exit_1(repo, capsys):
    assert run(Fake(enqueue_status=500), "--yes") == 1
    assert "failed: " in capsys.readouterr().out


def test_missing_start_frame_exit_1(repo, capsys):
    (repo / "ep/media/fixture/p3.png").unlink()
    fake = Fake()
    assert run(fake, "--yes") == 1
    assert "run panels first" in capsys.readouterr().out
    assert fake.total == 0


def test_missing_key_exit_2(repo, monkeypatch):
    monkeypatch.setattr(
        cli, "require_key", lambda name: (_ for _ in ()).throw(cli.MissingKeyError("no key"))
    )
    assert run(Fake(), "--yes") == 2


def test_bad_ledger_exit_1(repo, capsys):
    (repo / "ledger").mkdir()
    (repo / "ledger/spend.csv").write_text("garbage\n")
    assert run(Fake(), "--yes") == 1
    assert "ledger:" in capsys.readouterr().out


def test_manifest_records_model(repo):
    run(Fake(), "--yes")
    data = yaml.safe_load((repo / "ep/episode.yaml").read_text())
    assert data["arrival_shot"]["model"] == "kling-video-o3-omni"
