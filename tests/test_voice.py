"""Tests for the voice step: synthesis, skip/redo, failure semantics and write-back."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from smoke_gate import require_smoke

from toolbox_tales import voice as voice_mod
from toolbox_tales.manifest import load_and_validate
from toolbox_tales.render import build_plan, plan_runtime, render
from toolbox_tales.voice import VoiceError, VoiceResult, ffprobe_duration, voice_episode

FIXTURES = Path(__file__).parent / "fixtures"


def tone_seconds(text: str) -> float:
    """Fake speech length: encodes the text length."""
    return round(0.5 + 0.1 * len(text), 3)


class FakeSynth:
    """Records (text, voice) calls; writes a fake MP3, or a real tone when `real` is set."""

    def __init__(self, real: bool = False, fail_on: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.real = real
        self.fail_on = fail_on

    def __call__(self, text: str, voice: str, out_path: Path) -> None:
        self.calls.append((text, voice))
        if self.fail_on is not None and text == self.fail_on:
            raise RuntimeError("network down")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        seconds = tone_seconds(text)
        if not self.real:
            out_path.write_text(str(seconds), encoding="utf-8")
            return
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-c:a",
                "libmp3lame",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )


def fake_measure(path: Path) -> float:
    return float(path.read_text(encoding="utf-8"))


@pytest.fixture
def episode(tmp_path):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path / "episode.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def all_lines(data: dict) -> list[dict]:
    return [line for panel in data["panels"] for line in panel["lines"]]


def run(episode, synth, **kwargs) -> VoiceResult:
    return voice_episode(episode, synth=synth, measure=fake_measure, **kwargs)


def test_first_run_voices_every_line_and_writes_back(episode):
    synth = FakeSynth()
    result = run(episode, synth)
    lines = all_lines(load(episode))
    assert result == VoiceResult(voiced=len(lines), skipped=0, failures=())
    assert len(synth.calls) == len(lines)
    for line in lines:
        assert line["duration"] == tone_seconds(line["text"])
        assert (episode.parent / line["audio"]).exists()
    assert load(episode)["panels"][0]["lines"][0]["audio"] == ("media/voice/episode/p01-l01.mp3")
    assert [e for e in load_and_validate(episode) if not e.startswith("runtime:")] == []


def test_key_sidecar_is_sha256_of_voice_newline_text(episode):
    run(episode, FakeSynth())
    key = episode.parent / "media/voice/episode/p01-l01.key"
    expected = hashlib.sha256(
        ("en-US-BrianNeural" + "\n" + "Glue's set. Ship it.").encode("utf-8")
    ).hexdigest()
    assert key.read_text(encoding="utf-8").strip() == expected


def test_second_run_skips_everything(episode):
    run(episode, FakeSynth())
    synth = FakeSynth()
    result = run(episode, synth)
    assert synth.calls == []
    assert result.voiced == 0
    assert result.skipped == len(all_lines(load(episode)))


def test_skipped_line_remeasures_duration_from_existing_audio(episode):
    run(episode, FakeSynth())
    data = load(episode)
    data["panels"][0]["lines"][0]["duration"] = 99.0
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    synth = FakeSynth()
    run(episode, synth)
    assert synth.calls == []
    line = load(episode)["panels"][0]["lines"][0]
    assert line["duration"] == tone_seconds(line["text"])


def test_changed_text_resynthesizes_only_that_line(episode):
    run(episode, FakeSynth())
    data = load(episode)
    data["panels"][1]["lines"][0]["text"] = "A brand new sentence."
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    synth = FakeSynth()
    result = run(episode, synth)
    assert synth.calls == [("A brand new sentence.", "en-US-AvaNeural")]
    assert result.voiced == 1


def test_changed_voice_resynthesizes_only_that_characters_lines(episode):
    run(episode, FakeSynth())
    cast = episode.parent / "cast.yaml"
    sheet = yaml.safe_load(cast.read_text(encoding="utf-8"))
    for character in sheet["characters"]:
        if character["name"] == "Gauge":
            character["voice"] = "en-GB-RyanNeural"
    cast.write_text(yaml.safe_dump(sheet, sort_keys=False), encoding="utf-8")
    synth = FakeSynth()
    run(episode, synth)
    gauge_lines = [line["text"] for line in all_lines(load(episode)) if line["speaker"] == "Gauge"]
    assert sorted(t for t, _ in synth.calls) == sorted(gauge_lines)
    assert {v for _, v in synth.calls} == {"en-GB-RyanNeural"}


def test_redo_resynthesizes_everything(episode):
    run(episode, FakeSynth())
    synth = FakeSynth()
    result = run(episode, synth, redo=True)
    assert len(synth.calls) == len(all_lines(load(episode)))
    assert result.skipped == 0


def test_each_call_carries_the_speakers_voice(episode):
    synth = FakeSynth()
    run(episode, synth)
    voices = {
        "Teeth": "en-US-AndrewNeural",
        "Gauge": "en-US-AvaNeural",
        "Foreman": "en-US-BrianNeural",
    }
    expected = [(line["text"], voices[line["speaker"]]) for line in all_lines(load(episode))]
    assert synth.calls == expected


def test_failed_line_keeps_prior_state_and_others_update(episode):
    failing = "Did anyone actually check it?"
    before = load(episode)
    prior = before["panels"][0]["lines"][1]["duration"]
    synth = FakeSynth(fail_on=failing)
    result = run(episode, synth)
    after = load(episode)
    line = after["panels"][0]["lines"][1]
    assert line["duration"] == prior
    assert "audio" not in line
    assert result.failures and len(result.failures) == 1
    assert result.failures[0].startswith("panel 1 line 2: RuntimeError: network down")
    assert result.voiced == len(all_lines(after)) - 1
    first = after["panels"][0]["lines"][0]
    assert first["duration"] == tone_seconds(first["text"])
    assert "audio" in first


def test_measure_failure_is_a_line_failure(episode):
    def bad_measure(path: Path) -> float:
        raise VoiceError("garbage")

    result = voice_episode(episode, synth=FakeSynth(), measure=bad_measure)
    assert len(result.failures) == len(all_lines(load(episode)))
    assert result.failures[0].startswith("panel 1 line 1: VoiceError: garbage")


def test_missing_voice_is_a_failure_for_that_line(episode):
    cast = episode.parent / "cast.yaml"
    sheet = yaml.safe_load(cast.read_text(encoding="utf-8"))
    for character in sheet["characters"]:
        if character["name"] == "Teeth":
            character["voice"] = ""
    cast.write_text(yaml.safe_dump(sheet, sort_keys=False), encoding="utf-8")
    synth = FakeSynth()
    result = run(episode, synth)
    assert len(result.failures) == 2
    assert all("Teeth" in f for f in result.failures)
    assert all(voice != "" for _, voice in synth.calls)


def test_invalid_manifest_raises_voice_error(episode):
    data = load(episode)
    data["panels"] = data["panels"][:2]
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(VoiceError, match="panel-count"):
        run(episode, FakeSynth())


def test_stale_runtime_error_is_ignored(episode):
    data = load(episode)
    data["panels"][0]["lines"][0]["duration"] = 0.1
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert run(episode, FakeSynth()).failures == ()


def test_malformed_lines_never_raise(episode):
    data = load(episode)
    data["panels"][0]["lines"][0]["text"] = 42
    data["panels"][1] = "not a panel"
    episode.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    result = run(episode, FakeSynth())
    assert any(f.startswith("panel 1 line 1:") for f in result.failures)


def test_write_back_is_atomic(episode, monkeypatch):
    original = episode.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise yaml.YAMLError("disk on fire")

    monkeypatch.setattr(voice_mod.yaml, "safe_dump", boom)
    with pytest.raises(VoiceError):
        run(episode, FakeSynth())
    assert episode.read_text(encoding="utf-8") == original
    assert [p.name for p in episode.parent.iterdir() if p.suffix == ".tmp"] == []


def test_edge_tts_is_imported_lazily():
    source = Path(voice_mod.__file__).read_text(encoding="utf-8")
    top_level = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("edge_tts" in ln for ln in top_level)


def test_ffprobe_duration_parses_runner_output(tmp_path):
    def runner(args, **kwargs):
        assert args[0] == "ffprobe"
        return subprocess.CompletedProcess(args, 0, b"1.234000\n", b"")

    assert ffprobe_duration(tmp_path / "x.mp3", runner=runner) == pytest.approx(1.234)


def test_ffprobe_duration_garbage_raises(tmp_path):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, b"N/A\n", b"")

    with pytest.raises(VoiceError):
        ffprobe_duration(tmp_path / "x.mp3", runner=runner)


def test_integration_voice_then_render_audio_matches_plan(episode):
    require_smoke()
    result = voice_episode(episode, synth=FakeSynth(real=True))
    assert result.failures == ()
    data = load(episode)
    for line in all_lines(data):
        assert line["duration"] == pytest.approx(tone_seconds(line["text"]), abs=0.1)
    plan = build_plan(data, episode.parent)
    assert all(s.audio for s in plan.segments if s.kind == "panel")
    assert not any(s.audio for s in plan.segments if s.kind == "shot")
    render(plan)
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=nw=1:nk=1",
            str(plan.output),
        ],
        capture_output=True,
        check=True,
    )
    assert float(out.stdout.decode().split()[0]) == pytest.approx(plan_runtime(plan), abs=0.2)


def test_zero_byte_audio_with_matching_key_is_resynthesized(episode):
    run(episode, FakeSynth())
    first = episode.parent / "media/voice/episode/p01-l01.mp3"
    first.write_bytes(b"")

    def measure(path: Path) -> float:
        if path.stat().st_size == 0:
            raise VoiceError("ffprobe gave no duration")
        return fake_measure(path)

    synth = FakeSynth()
    result = voice_episode(episode, synth=synth, measure=measure)
    assert len(synth.calls) == 1
    assert result.failures == ()
    assert result.voiced == 1
    assert result.skipped == len(all_lines(load(episode))) - 1
    line = load(episode)["panels"][0]["lines"][0]
    assert line["duration"] == tone_seconds(line["text"])


def test_cache_miss_that_fails_again_is_recorded_once(episode):
    run(episode, FakeSynth())
    (episode.parent / "media/voice/episode/p01-l01.mp3").write_bytes(b"")

    def bad_measure(path: Path) -> float:
        if path.stat().st_size == 0:
            raise VoiceError("still broken")
        return fake_measure(path)

    class ZeroSynth(FakeSynth):
        def __call__(self, text, voice, out_path):
            self.calls.append((text, voice))
            out_path.write_bytes(b"")

    synth = ZeroSynth()
    result = voice_episode(episode, synth=synth, measure=bad_measure)
    assert len(synth.calls) == 1
    assert len(result.failures) == 1
    assert result.failures[0].startswith("panel 1 line 1: VoiceError: still broken")


def test_programming_error_in_bookkeeping_propagates(episode, monkeypatch):
    def boom(voice: str, text: str) -> str:
        raise RuntimeError("bug in voice_episode bookkeeping")

    monkeypatch.setattr(voice_mod, "line_key", boom)
    with pytest.raises(RuntimeError, match="bookkeeping"):
        run(episode, FakeSynth())


@pytest.mark.parametrize("name", ["..", ".", "sub/.."])
def test_unsafe_manifest_stem_is_rejected(name):
    # Path("..").stem == ".." and Path(".").stem == "" on every Python version.
    assert Path(name).stem in ("", "..")
    with pytest.raises(VoiceError, match="manifest name must not be"):
        voice_episode(Path(name), synth=FakeSynth(), measure=fake_measure)


def test_integration_voiced_segment_audio_is_not_silence(episode):
    require_smoke()
    result = voice_episode(episode, synth=FakeSynth(real=True))
    assert result.failures == ()
    plan = build_plan(load(episode), episode.parent)
    first = plan.segments[0]
    assert first.kind == "panel" and first.audio
    render(plan)
    probe = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-t",
            f"{first.duration_s:.3f}",
            "-i",
            str(plan.output),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    stderr = probe.stderr.decode("utf-8", errors="replace")
    marker = "mean_volume:"
    assert marker in stderr
    mean = float(stderr.split(marker)[1].split("dB")[0])
    assert mean > -60.0
