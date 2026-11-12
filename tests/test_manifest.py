import copy
from pathlib import Path

import pytest
import yaml

from toolbox_tales import manifest as m
from toolbox_tales.cli import main
from toolbox_tales.manifest import load_and_validate, validate

FIXTURES = Path(__file__).parent / "fixtures"
EPISODE = FIXTURES / "episode.yaml"


@pytest.fixture
def cast():
    return yaml.safe_load((FIXTURES / "cast.yaml").read_text())


@pytest.fixture
def ep():
    return yaml.safe_load(EPISODE.read_text())


def codes(errors):
    return [e.split(":", 1)[0] for e in errors]


def set_durations(ep, total_lines_s):
    """Spread total_lines_s over all lines so runtime = total + 2.5 + 5.0."""
    lines = [ln for p in ep["panels"] for ln in p["lines"]]
    for ln in lines:
        ln["duration"] = total_lines_s / len(lines)


def test_constants():
    assert (m.MIN_PANELS, m.MAX_PANELS, m.OVERLAY_MAX) == (4, 6, 60)
    assert (m.PANEL_PADDING_S, m.SHOT_ALLOWANCE_S) == (0.5, 5.0)
    assert (m.RUNTIME_MIN_S, m.RUNTIME_MAX_S) == (45.0, 60.0)


def test_fixture_is_valid(ep, cast):
    assert validate(ep, cast) == []


# panel-count
def test_panel_count_3_fails(ep, cast):
    ep["panels"] = ep["panels"][:3]
    ep["arrival_shot"]["after_panel"] = 1
    for p in ep["panels"]:
        for ln in p["lines"]:
            ln.pop("duration")
    errs = validate(ep, cast)
    assert codes(errs) == ["panel-count"]


def test_panel_count_7_fails(ep, cast):
    ep["panels"] = ep["panels"] + copy.deepcopy(ep["panels"][:2])
    for p in ep["panels"]:
        for ln in p["lines"]:
            ln.pop("duration")
    assert codes(validate(ep, cast)) == ["panel-count"]


def test_panel_count_4_and_6_pass(ep, cast):
    for p in ep["panels"]:
        for ln in p["lines"]:
            ln.pop("duration")
    four = copy.deepcopy(ep)
    four["panels"] = four["panels"][:4]
    assert validate(four, cast) == []
    six = copy.deepcopy(ep)
    six["panels"] = six["panels"] + copy.deepcopy(six["panels"][:1])
    assert validate(six, cast) == []


# unknown-speaker
def test_unknown_speaker_one_error_per_line(ep, cast):
    ep["panels"][0]["lines"][0]["speaker"] = "Ghost"
    ep["panels"][1]["lines"][0]["speaker"] = "Ghost"
    errs = validate(ep, cast)
    assert codes(errs) == ["unknown-speaker", "unknown-speaker"]
    assert all("Ghost" in e for e in errs)


def test_unknown_speaker_unhashable_does_not_raise(ep, cast):
    ep["panels"][0]["lines"][0]["speaker"] = ["Teeth"]
    assert "unknown-speaker" in codes(validate(ep, cast))


# unknown-cast
def test_unknown_cast_entry(ep, cast):
    ep["cast"].append("Nobody")
    errs = validate(ep, cast)
    assert codes(errs) == ["unknown-cast"]
    assert "Nobody" in errs[0]


def test_unknown_cast_arrival_reference(ep, cast):
    ep["arrival_shot"]["reference"] = "Nobody"
    assert codes(validate(ep, cast)) == ["unknown-cast"]


def test_unknown_cast_unhashable_entry(ep, cast):
    ep["cast"].append({"a": 1})
    assert codes(validate(ep, cast)) == ["unknown-cast"]


# overlay-length
def test_overlay_length_60_passes(ep, cast):
    ep["panels"][0]["overlay"] = "x" * 60
    assert validate(ep, cast) == []


def test_overlay_length_61_fails(ep, cast):
    ep["panels"][0]["overlay"] = "x" * 61
    assert codes(validate(ep, cast)) == ["overlay-length"]


# tool-overlay
def test_tool_overlay_missing_fails(ep, cast):
    ep["panels"][2]["overlay"] = "Enter the hero"
    assert codes(validate(ep, cast)) == ["tool-overlay"]


def test_tool_overlay_case_insensitive(ep, cast):
    ep["panels"][2]["overlay"] = "ENTER DETECTOR-TEETH-CHECK"
    assert validate(ep, cast) == []


# arrival-shot
def test_arrival_shot_missing_fails(ep, cast):
    del ep["arrival_shot"]
    assert codes(validate(ep, cast)) == ["arrival-shot"]


def test_arrival_shot_list_of_shots_fails(ep, cast):
    ep["arrival_shot"] = [ep["arrival_shot"]]
    assert codes(validate(ep, cast)) == ["arrival-shot"]


def test_arrival_shot_after_panel_zero_fails(ep, cast):
    ep["arrival_shot"]["after_panel"] = 0
    assert codes(validate(ep, cast)) == ["arrival-shot"]


def test_arrival_shot_after_panel_len_plus_one_fails(ep, cast):
    ep["arrival_shot"]["after_panel"] = len(ep["panels"]) + 1
    assert codes(validate(ep, cast)) == ["arrival-shot"]


def test_arrival_shot_after_panel_len_passes(ep, cast):
    ep["arrival_shot"]["after_panel"] = len(ep["panels"])
    assert validate(ep, cast) == []


@pytest.mark.parametrize("bad", ["3", 3.0, True, None, [3]])
def test_arrival_shot_after_panel_non_int_fails(ep, cast, bad):
    ep["arrival_shot"]["after_panel"] = bad
    assert codes(validate(ep, cast)) == ["arrival-shot"]


# runtime
@pytest.mark.parametrize(
    ("lines_s", "ok"),
    [(37.4, False), (37.5, True), (52.5, True), (52.6, False)],
)
def test_runtime_bounds(ep, cast, lines_s, ok):
    # runtime = lines + 0.5 * 5 panels + 5.0 shot -> 44.9, 45.0, 60.0, 60.1
    set_durations(ep, lines_s)
    errs = validate(ep, cast)
    assert (errs == []) if ok else (codes(errs) == ["runtime"])


def test_runtime_missing_duration_skips_rule(ep, cast):
    set_durations(ep, 1.0)
    del ep["panels"][0]["lines"][0]["duration"]
    assert validate(ep, cast) == []


def test_runtime_string_duration_is_error(ep, cast):
    ep["panels"][0]["lines"][0]["duration"] = "4.0"
    assert codes(validate(ep, cast)) == ["runtime"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_runtime_non_finite_or_bool_duration_is_error(ep, cast, bad):
    ep["panels"][0]["lines"][0]["duration"] = bad
    assert codes(validate(ep, cast)) == ["runtime"]


# robustness
@pytest.mark.parametrize("junk", [None, 5, "x", [], {"panels": 5}, {"panels": [5, None]}])
def test_malformed_manifest_never_raises(cast, junk):
    errs = validate(junk, cast)
    assert isinstance(errs, list)
    assert all(isinstance(e, str) and ": " in e for e in errs)


@pytest.mark.parametrize(
    "junk", [None, 5, [], {"characters": 5}, {"characters": [5, {"name": []}]}]
)
def test_malformed_cast_sheet_never_raises(ep, junk):
    assert isinstance(validate(ep, junk), list)


# load_and_validate + CLI
def test_load_and_validate_fixture():
    assert load_and_validate(EPISODE) == []


def test_load_missing_file(tmp_path):
    errs = load_and_validate(tmp_path / "nope.yaml")
    assert errs and errs[0].startswith("load: ")


def test_load_yaml_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("a: [unclosed\n")
    assert load_and_validate(p)[0].startswith("load: ")


def test_load_missing_cast_sheet(tmp_path, ep):
    ep["cast_sheet"] = "absent.yaml"
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(ep))
    assert load_and_validate(p)[0].startswith("load: ")


def test_load_non_mapping_manifest(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("- 1\n- 2\n")
    assert load_and_validate(p)[0].startswith("load: ")


def test_cli_fixture_valid(capsys):
    assert main(["validate", str(EPISODE)]) == 0
    assert capsys.readouterr().out == "valid\n"


def test_cli_invalid_manifest_exits_1(tmp_path, capsys, ep):
    ep["panels"][0]["overlay"] = "x" * 61
    ep["panels"][1]["lines"][0]["speaker"] = "Ghost"
    ep["cast_sheet"] = str(FIXTURES / "cast.yaml")
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(ep))
    assert main(["validate", str(p)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert sorted(ln.split(":")[0] for ln in lines) == ["overlay-length", "unknown-speaker"]


def test_cli_missing_file_exits_1_with_load(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope.yaml")]) == 1
    assert capsys.readouterr().out.startswith("load: ")


def test_committed_episode_1_is_valid():
    root = Path(__file__).resolve().parent.parent
    path = root / "episodes" / "ep01" / "episode.yaml"
    assert load_and_validate(path) == []
    assert yaml.safe_load(path.read_text())["tool"] == "detector-teeth-check"
