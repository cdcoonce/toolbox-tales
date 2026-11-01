"""The smoke-test skip rule: skip locally, fail under CI."""

import itertools

import pytest
from smoke_gate import decide


def test_all_present_runs_with_or_without_ci():
    for ci in (False, True):
        assert decide(has_ffmpeg=True, has_ffprobe=True, has_drawtext=True, ci=ci) == "run"


@pytest.mark.parametrize(
    ("has_ffmpeg", "has_ffprobe", "has_drawtext"),
    [c for c in itertools.product([True, False], repeat=3) if not all(c)],
)
def test_missing_piece_skips_locally_and_fails_in_ci(has_ffmpeg, has_ffprobe, has_drawtext):
    kwargs = {"has_ffmpeg": has_ffmpeg, "has_ffprobe": has_ffprobe, "has_drawtext": has_drawtext}
    assert decide(ci=False, **kwargs) == "skip"
    assert decide(ci=True, **kwargs) == "fail"


def test_require_smoke_fails_under_ci_without_drawtext(monkeypatch, tmp_path):
    import smoke_gate

    shim = tmp_path / "ffmpeg"
    shim.write_text("#!/bin/sh\necho ' T.. scale V->V Scale.'\n")
    shim.chmod(0o755)
    probe = tmp_path / "ffprobe"
    probe.write_text("#!/bin/sh\n")
    probe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CI", "1")
    with pytest.raises(pytest.fail.Exception):
        smoke_gate.require_smoke()
    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        smoke_gate.require_smoke()
