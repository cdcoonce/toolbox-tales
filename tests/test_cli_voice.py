"""Tests for the voice subcommand."""

import functools
import shutil
from pathlib import Path

import pytest
import yaml
from test_voice import FakeSynth, fake_measure

from toolbox_tales import cli
from toolbox_tales.cli import main
from toolbox_tales.voice import VoiceError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def episode(tmp_path):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path / "episode.yaml"


def patch_voice(monkeypatch, synth):
    real = cli.voice_mod.voice_episode
    monkeypatch.setattr(
        cli.voice_mod,
        "voice_episode",
        functools.partial(real, synth=synth, measure=fake_measure),
    )


def test_voice_success_prints_summary(episode, monkeypatch, capsys):
    patch_voice(monkeypatch, FakeSynth())
    assert main(["voice", str(episode)]) == 0
    assert capsys.readouterr().out.strip() == "voiced 8, skipped 0"
    assert main(["voice", str(episode)]) == 0
    assert capsys.readouterr().out.strip() == "voiced 0, skipped 8"


def test_voice_redo_flag(episode, monkeypatch, capsys):
    patch_voice(monkeypatch, FakeSynth())
    main(["voice", str(episode)])
    capsys.readouterr()
    assert main(["voice", str(episode), "--redo"]) == 0
    assert capsys.readouterr().out.strip() == "voiced 8, skipped 0"


def test_voice_failure_exits_1_and_names_the_line(episode, monkeypatch, capsys):
    patch_voice(monkeypatch, FakeSynth(fail_on="Did anyone actually check it?"))
    assert main(["voice", str(episode)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "voiced 7, skipped 0"
    assert out[1].startswith("voice: panel 1 line 2: RuntimeError")


def test_voice_invalid_manifest_exits_1_without_traceback(episode, capsys):
    data = yaml.safe_load(episode.read_text(encoding="utf-8"))
    data["panels"] = data["panels"][:2]
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert main(["voice", str(episode)]) == 1
    assert capsys.readouterr().out.startswith("voice: ")


def test_voice_error_from_module_exits_1(episode, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise VoiceError("ffprobe said garbage")

    monkeypatch.setattr(cli.voice_mod, "voice_episode", boom)
    assert main(["voice", str(episode)]) == 1
    assert capsys.readouterr().out.strip() == "voice: ffprobe said garbage"


def test_voice_unsafe_manifest_stem_exits_1(capsys):
    assert main(["voice", ".."]) == 1
    assert capsys.readouterr().out.strip() == "voice: manifest name must not be '.' or '..'"
