"""Tests for the render plan (pure) and the ffmpeg runner."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from smoke_gate import require_smoke
from toolbox_tales.manifest import PANEL_PADDING_S, SHOT_ALLOWANCE_S
from toolbox_tales.render import (
    DEFAULT_LINE_S,
    Plan,
    RenderError,
    Segment,
    build_plan,
    plan_runtime,
    render,
)

FIXTURES = Path(__file__).parent / "fixtures"
HOSTILE = "a:b'c\\%{x}"


def ok_runner(calls=None):
    """A fake runner whose ffmpeg advertises drawtext and accepts every command."""

    def runner(args, **kwargs):
        if calls is not None:
            calls.append(list(args))
        out = b" T.. drawtext  V->V  Draw text.\n" if "-filters" in args else b""
        return subprocess.CompletedProcess(args, 0, out, b"")

    return runner


@pytest.fixture
def manifest(tmp_path):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return yaml.safe_load((tmp_path / "episode.yaml").read_text(encoding="utf-8"))


def test_shot_goes_after_panel_three(manifest, tmp_path):
    plan = build_plan(manifest, tmp_path)
    kinds = [s.kind for s in plan.segments]
    assert kinds == ["panel", "panel", "panel", "shot", "panel", "panel"]


def test_shot_slot_follows_after_panel(manifest, tmp_path):
    manifest["arrival_shot"]["after_panel"] = 1
    kinds = [s.kind for s in build_plan(manifest, tmp_path).segments]
    assert kinds.index("shot") == 1
    manifest["arrival_shot"]["after_panel"] = 5
    kinds = [s.kind for s in build_plan(manifest, tmp_path).segments]
    assert kinds.index("shot") == 5


def test_panel_durations_are_lines_plus_padding(manifest, tmp_path):
    plan = build_plan(manifest, tmp_path)
    panels = [s for s in plan.segments if s.kind == "panel"]
    assert [s.duration_s for s in panels] == [
        8.5 + PANEL_PADDING_S,
        9.0 + PANEL_PADDING_S,
        8.0 + PANEL_PADDING_S,
        9.0 + PANEL_PADDING_S,
        8.0 + PANEL_PADDING_S,
    ]


def test_line_without_duration_uses_default(manifest, tmp_path):
    del manifest["panels"][0]["lines"][0]["duration"]
    panel = build_plan(manifest, tmp_path).segments[0]
    assert panel.duration_s == DEFAULT_LINE_S + 4.5 + PANEL_PADDING_S
    assert DEFAULT_LINE_S == 3.0


def test_shot_defaults_to_allowance(manifest, tmp_path):
    shot = build_plan(manifest, tmp_path).segments[3]
    assert shot.duration_s == SHOT_ALLOWANCE_S


def test_shot_duration_field_overrides_allowance(manifest, tmp_path):
    manifest["arrival_shot"]["duration"] = 7.25
    assert build_plan(manifest, tmp_path).segments[3].duration_s == 7.25


def test_missing_image_is_placeholder_with_prompt(manifest, tmp_path):
    seg = build_plan(manifest, tmp_path).segments[0]
    assert seg.source is None
    assert seg.placeholder_text == manifest["panels"][0]["prompt"]
    assert seg.overlay == "The joint looks perfect"


def test_existing_image_is_source(manifest, tmp_path):
    image = tmp_path / manifest["panels"][0]["image"]
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    seg = build_plan(manifest, tmp_path).segments[0]
    assert seg.source == image


def test_shot_source_and_placeholder(manifest, tmp_path):
    shot = build_plan(manifest, tmp_path).segments[3]
    assert shot.source is None
    assert shot.placeholder_text == manifest["arrival_shot"]["prompt"]
    assert shot.overlay is None
    video = tmp_path / manifest["arrival_shot"]["video"]
    video.parent.mkdir(parents=True)
    video.write_bytes(b"mp4")
    assert build_plan(manifest, tmp_path).segments[3].source == video


def test_output_resolves_against_manifest_dir(manifest, tmp_path):
    plan = build_plan(manifest, tmp_path)
    assert plan.output == tmp_path / manifest["render"]
    assert plan.audio == ()
    assert (plan.width, plan.height, plan.fps) == (1280, 720, 24)


def test_build_plan_is_pure(manifest, tmp_path, monkeypatch):
    before = copy.deepcopy(manifest)

    def boom(*a, **k):
        raise AssertionError("subprocess called")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    build_plan(manifest, tmp_path)
    assert manifest == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cast.yaml", "episode.yaml"]


def test_plan_runtime_sums_segments(manifest, tmp_path):
    assert plan_runtime(build_plan(manifest, tmp_path)) == pytest.approx(50.0)


def hostile_plan(tmp_path: Path) -> Plan:
    return Plan(
        segments=(
            Segment("panel", None, 1.0, HOSTILE, "prompt " + HOSTILE),
            Segment("panel", None, 1.0, "plain", "other"),
        ),
        audio=(),
        output=tmp_path / "out" / "ep.mp4",
        width=320,
        height=180,
        fps=12,
    )


def test_text_never_inline_in_ffmpeg_args(tmp_path):
    calls = []
    inner = ok_runner()

    def runner(args, **kwargs):
        calls.append(list(args))
        text_files = [a for a in args if "textfile=" in a]
        for arg in text_files:
            for part in arg.split("textfile=")[1:]:
                path = part.split(":")[0].split(",")[0].strip("'")
                assert Path(path).exists()
        return inner(args, **kwargs)

    render(hostile_plan(tmp_path), runner=runner)
    assert calls
    flat = [a for call in calls for a in call]
    assert any("textfile=" in a for a in flat)
    for arg in flat:
        assert HOSTILE not in arg
        assert "%{x}" not in arg
        assert "a:b" not in arg


def test_render_error_carries_stderr_tail(tmp_path):
    inner = ok_runner()

    def runner(args, **kwargs):
        if "-filters" in args:
            return inner(args, **kwargs)
        return subprocess.CompletedProcess(args, 1, b"", b"boom: bad filter")

    with pytest.raises(RenderError, match="boom: bad filter"):
        render(hostile_plan(tmp_path), runner=runner)


def no_drawtext_runner(calls):
    def runner(args, **kwargs):
        calls.append(list(args))
        if "-filters" in args:
            return subprocess.CompletedProcess(args, 0, b" T.. scale  V->V  Scale.\n", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"No such filter: 'drawtext'")

    return runner


def test_render_without_drawtext_raises(tmp_path):
    plan = hostile_plan(tmp_path)
    with pytest.raises(RenderError, match="drawtext") as info:
        render(plan, runner=no_drawtext_runner([]))
    assert "install an ffmpeg built with freetype (drawtext)" in str(info.value)
    assert not plan.output.exists()


def test_no_text_fallback_run(tmp_path):
    calls = []
    with pytest.raises(RenderError):
        render(hostile_plan(tmp_path), runner=no_drawtext_runner(calls))
    assert calls == [calls[0]]
    assert "-filters" in calls[0]


def test_render_checks_drawtext_before_any_segment(tmp_path):
    calls = []
    render(hostile_plan(tmp_path), runner=ok_runner(calls))
    assert "-filters" in calls[0]
    assert "-hide_banner" in calls[0]
    assert all("-filters" not in c for c in calls[1:])


def _shot_duration(manifest, tmp_path, value):
    manifest["arrival_shot"]["duration"] = value
    return build_plan(manifest, tmp_path)


@pytest.mark.parametrize("value", [0, -3, 0.0, float("inf"), float("-inf"), float("nan")])
def test_plan_rejects_nonpositive_shot_duration(manifest, tmp_path, value):
    with pytest.raises(ValueError, match="shot: duration must be a positive number of seconds"):
        _shot_duration(manifest, tmp_path, value)


def test_non_numeric_shot_duration_falls_back_to_allowance(manifest, tmp_path):
    plan = _shot_duration(manifest, tmp_path, "5")
    assert plan.segments[3].duration_s == SHOT_ALLOWANCE_S


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_smoke_render_two_placeholders(tmp_path):
    require_smoke()
    plan = hostile_plan(tmp_path)
    render(plan)
    assert plan.output.exists()
    info = probe(plan.output)
    streams = {s["codec_type"]: s for s in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["video"]["pix_fmt"] == "yuv420p"
    assert "audio" in streams
    assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.2)
