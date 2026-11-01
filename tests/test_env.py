"""Tests for .env loading and key lookup."""

import pytest

from toolbox_tales.env import MissingKeyError, load_env, require_key


def test_skips_comments_and_blanks(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nA=1\n  B = two  \n")
    assert load_env(f) == {"A": "1", "B": "two"}


def test_strips_matching_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=\"x y\"\nB='z'\nC=\"mismatch'\n")
    assert load_env(f) == {"A": "x y", "B": "z", "C": "\"mismatch'"}


def test_missing_file_is_empty(tmp_path):
    assert load_env(tmp_path / "nope") == {}


def test_env_var_beats_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("OXEN_API_KEY=from-file\n")
    monkeypatch.setenv("OXEN_API_KEY", "from-env")
    assert require_key("OXEN_API_KEY", f) == "from-env"


def test_file_used_when_env_unset(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("OXEN_API_KEY=from-file\n")
    monkeypatch.delenv("OXEN_API_KEY", raising=False)
    assert require_key("OXEN_API_KEY", f) == "from-file"


def test_missing_key_message_is_exact(tmp_path, monkeypatch):
    monkeypatch.delenv("OXEN_API_KEY", raising=False)
    with pytest.raises(MissingKeyError) as exc:
        require_key("OXEN_API_KEY", tmp_path / ".env")
    assert str(exc.value) == "missing OXEN_API_KEY: set it in .env or the environment"
