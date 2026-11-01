"""Tests for the render subcommand."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from toolbox_tales import cli
from toolbox_tales.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def episode(tmp_path):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path / "episode.yaml"


@pytest.fixture
def no_ffmpeg_calls(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("ffmpeg invoked")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(cli.render_mod, "render", boom)
    return calls


def test_invalid_manifest_exits_1(episode, capsys, no_ffmpeg_calls):
    data = yaml.safe_load(episode.read_text(encoding="utf-8"))
    data["panels"] = data["panels"][:2]
    episode.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["render", str(episode)]) == 1
    assert capsys.readouterr().out.strip()
    assert not no_ffmpeg_calls


def test_short_runtime_refused_before_ffmpeg(episode, capsys, monkeypatch, no_ffmpeg_calls):
    monkeypatch.setattr(cli.render_mod, "plan_runtime", lambda plan: 30.0)
    assert main(["render", str(episode)]) == 1
    assert "runtime: 30.00 s outside 45-60 s" in capsys.readouterr().out
    assert not no_ffmpeg_calls


def test_long_runtime_refused_before_ffmpeg(episode, capsys, monkeypatch, no_ffmpeg_calls):
    monkeypatch.setattr(cli.render_mod, "plan_runtime", lambda plan: 60.5)
    assert main(["render", str(episode)]) == 1
    assert "runtime: 60.50 s outside 45-60 s" in capsys.readouterr().out
    assert not no_ffmpeg_calls


def test_missing_ffmpeg_exits_3(episode, capsys, monkeypatch, no_ffmpeg_calls):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert main(["render", str(episode)]) == 3
    assert capsys.readouterr().out.strip() == "ffmpeg not found: install it (brew install ffmpeg)"
    assert not no_ffmpeg_calls


def test_render_error_exits_1(episode, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fail(plan, ffmpeg="ffmpeg"):
        raise cli.render_mod.RenderError("ffmpeg failed: bad")

    monkeypatch.setattr(cli.render_mod, "render", fail)
    assert main(["render", str(episode)]) == 1
    assert "ffmpeg failed: bad" in capsys.readouterr().out


def test_fixture_manifest_renders(episode, capsys):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        if os.environ.get("CI"):
            pytest.fail("ffmpeg and ffprobe must be installed under CI")
        pytest.skip("ffmpeg not installed")
    assert main(["render", str(episode)]) == 0
    out = episode.parent / "media" / "fixture" / "episode.mp4"
    assert str(out) in capsys.readouterr().out
    assert out.exists()
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(out)],
            capture_output=True,
            check=True,
        ).stdout
    )
    assert float(info["format"]["duration"]) == pytest.approx(50.0, abs=0.2)
