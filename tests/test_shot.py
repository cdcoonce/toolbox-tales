"""Tests for the arrival shot, with a scripted fake transport. No network, no real sleeping."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from smoke_gate import require_smoke

from toolbox_tales.ledger import Ledger
from toolbox_tales.manifest import load_and_validate
from toolbox_tales.oxen import OxenClient
from toolbox_tales.render import build_plan
from toolbox_tales.shot import ShotError, make_shot
from toolbox_tales.spend import Estimate, load_config

FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
PREFERRED = "kling-video-o3-omni"
FALLBACK = "kling-video-v2-5-turbo-pro-image-to-video"
SPEND = (
    "per_call_cap_usd = 3.0\ntotal_cap_usd = 25.0\n"
    f'[models]\nimage = "flux-2-dev"\nvideo = "{PREFERRED}"\n[script]\nmodel = ""\n'
)
DOWNLOAD_URL = "https://signed.example/result.mp4"


def queued(gid="gen-1"):
    return 200, json.dumps({"generations": [{"generation_id": gid, "status": "queued"}]}).encode()


def state(status, **kw):
    body = {"generation_id": "gen-1", "status": status, **kw}
    return 200, json.dumps(body).encode()


class Fake:
    """Scripted transport. `queue` holds enqueue replies, `gets` the GET /ai/queue/<id> replies."""

    def __init__(self, preferred=0.1, fallback=0.07, enqueue=None, gets=None, download=None):
        self.calls = []  # (method, url, headers, body)
        self.prices = {PREFERRED: preferred, FALLBACK: fallback}
        self.enqueue = list(enqueue if enqueue is not None else [queued()])
        self.gets = list(
            gets if gets is not None else [state("succeeded", result_url=DOWNLOAD_URL)]
        )
        self.download = (200, MP4) if download is None else download

    def of(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]

    @property
    def enqueues(self):
        return self.of("POST", "/ai/queue")

    @property
    def price_calls(self):
        return [c for c in self.calls if "/ai/models/" in c[1]]

    def __call__(self, request):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), request.full_url, dict(request.headers), body))
        url = request.full_url
        if "/ai/models/" in url:
            model = url.rsplit("/", 1)[1]
            price = self.prices[model]
            return 200, json.dumps({} if price is None else {"cost_per_second": price}).encode()
        if url.endswith("/ai/queue"):
            return self.enqueue.pop(0)
        if "/ai/queue/" in url:
            item = self.gets.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if url == DOWNLOAD_URL:
            return self.download
        raise AssertionError(f"unexpected request {url}")


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def setup(tmp_path, spend=SPEND, shot_section="", cast_edit=None, start_frame=True):
    for name in ("episode.yaml", "cast.yaml"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    if cast_edit:
        cast = yaml.safe_load((tmp_path / "cast.yaml").read_text())
        cast_edit(cast)
        (tmp_path / "cast.yaml").write_text(yaml.safe_dump(cast))
    if start_frame:
        frame = tmp_path / "media/fixture/p3.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(PNG)
    cfg = tmp_path / "spend.toml"
    cfg.write_text(spend + shot_section)
    return tmp_path / "episode.yaml", cfg


def edit_manifest(path, fn):
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def shot_of(path):
    return yaml.safe_load(path.read_text())["arrival_shot"]


def go(tmp_path, fake, confirmed=True, clock=None, measure=None, **kw):
    manifest, cfg = setup(tmp_path, **kw.pop("setup", {}))
    return go_made(tmp_path, manifest, cfg, fake, confirmed, clock, measure)


def go_made(tmp_path, manifest, cfg, fake, confirmed=True, clock=None, measure=None):
    clock = clock or Clock()
    ledger = Ledger(tmp_path / "ledger.csv")
    result = make_shot(
        manifest,
        OxenClient("key9", transport=fake),
        confirmed=confirmed,
        ledger=ledger,
        config=load_config(cfg),
        config_path=cfg,
        sleep=clock.sleep,
        clock=clock,
        measure=measure or (lambda p: 5.04321),
    )
    return result, ledger, clock


def rows(ledger):
    return ledger.path.read_text().splitlines()[1:] if ledger.path.exists() else []


# --- model choice ---------------------------------------------------------------------------


def test_preferred_chosen_when_three_shots_fit(tmp_path):
    fake = Fake(preferred=0.1)  # 0.5 * 3 = 1.5 <= 25
    result, _, _ = go(tmp_path, fake)
    assert fake.enqueues[0][3]["model"] == PREFERRED
    assert result.model == PREFERRED
    assert shot_of(tmp_path / "episode.yaml")["model"] == PREFERRED


def test_fallback_when_preferred_too_expensive_for_three(tmp_path):
    fake = Fake(preferred=1.5)  # 7.5 * 3 = 22.5 fits 25 ...
    manifest, cfg = setup(tmp_path)
    ledger = Ledger(tmp_path / "ledger.csv")
    ledger.append(
        Estimate("x", "video", 1, 5.0),
        "seed",
        5.0,
    )  # remaining 20 < 22.5
    clock = Clock()
    result = make_shot(
        manifest,
        OxenClient("key9", transport=fake),
        confirmed=True,
        ledger=ledger,
        config=load_config(cfg),
        config_path=cfg,
        sleep=clock.sleep,
        clock=clock,
        measure=lambda p: 5.0,
    )
    assert fake.enqueues[0][3]["model"] == FALLBACK
    assert result.model == FALLBACK
    assert shot_of(manifest)["model"] == FALLBACK


def test_fallback_when_preferred_unpriced(tmp_path):
    fake = Fake(preferred=None)
    go(tmp_path, fake)
    assert fake.enqueues[0][3]["model"] == FALLBACK
    assert len(fake.price_calls) == 2


def test_one_price_call_when_preferred_fits(tmp_path):
    fake = Fake()
    go(tmp_path, fake)
    assert len(fake.price_calls) == 1


def test_both_too_expensive_chooses_preferred_and_authorize_decides(tmp_path):
    fake = Fake(preferred=0.5, fallback=0.5)  # 2.5 per shot, 7.5 for three; remaining 25 fits...
    manifest, cfg = setup(
        tmp_path, spend=SPEND.replace("total_cap_usd = 25.0", "total_cap_usd = 5.0")
    )
    clock = Clock()
    result = make_shot(
        manifest,
        OxenClient("key9", transport=fake),
        confirmed=True,
        ledger=Ledger(tmp_path / "ledger.csv"),
        config=load_config(cfg),
        config_path=cfg,
        sleep=clock.sleep,
        clock=clock,
        measure=lambda p: 5.0,
    )
    assert result.status == "succeeded"
    assert result.model == PREFERRED


# --- params ---------------------------------------------------------------------------------


def with_reference(tmp_path):
    (tmp_path / "teeth.png").write_bytes(PNG)

    def edit(cast):
        cast["characters"][0]["reference_image"] = "teeth.png"

    return edit


def test_preferred_params_without_reference(tmp_path):
    fake = Fake()
    go(tmp_path, fake)
    body = fake.enqueues[0][3]
    assert body["generate_audio"] is False
    assert body["resolution"] == "720p"
    assert body["duration"] == 5
    assert body["aspect_ratio"] == "16:9"
    assert body["input_image"].startswith("data:image/png;base64,")
    assert base64.b64decode(body["input_image"].split(",", 1)[1]) == PNG
    assert "input_images" not in body
    assert (
        body["prompt"]
        == yaml.safe_load((tmp_path / "episode.yaml").read_text())["arrival_shot"]["prompt"]
    )


def test_preferred_params_with_reference(tmp_path):
    manifest, cfg = setup(tmp_path, cast_edit=with_reference(tmp_path))
    fake = Fake()
    go_made(tmp_path, manifest, cfg, fake)
    body = fake.enqueues[0][3]
    assert body["input_images"] == [f"data:image/png;base64,{base64.b64encode(PNG).decode()}"]
    assert body["prompt"].endswith(" Teeth is @Image1.")
    assert body["generate_audio"] is False


def test_fallback_params(tmp_path):
    manifest, cfg = setup(tmp_path, cast_edit=with_reference(tmp_path))
    fake = Fake(preferred=None)
    go_made(tmp_path, manifest, cfg, fake)
    body = fake.enqueues[0][3]
    assert body["model"] == FALLBACK
    for absent in ("input_images", "resolution", "generate_audio"):
        assert absent not in body
    assert body["input_image"].startswith("data:image/png;base64,")
    assert body["duration"] == 5
    assert body["aspect_ratio"] == "16:9"
    assert not body["prompt"].endswith("@Image1.")


def test_bad_reference_fails_before_any_request(tmp_path):
    (tmp_path / "teeth.gif").write_bytes(PNG)

    def edit(cast):
        cast["characters"][0]["reference_image"] = "teeth.gif"

    manifest, cfg = setup(tmp_path, cast_edit=edit)
    fake = Fake()
    with pytest.raises(ShotError, match="shot: reference for Teeth"):
        go_made(tmp_path, manifest, cfg, fake)
    assert fake.calls == []


def test_missing_reference_file_fails(tmp_path):
    def edit(cast):
        cast["characters"][0]["reference_image"] = "nope.png"

    manifest, cfg = setup(tmp_path, cast_edit=edit)
    fake = Fake()
    with pytest.raises(ShotError, match="shot: reference for Teeth"):
        go_made(tmp_path, manifest, cfg, fake)
    assert fake.calls == []


def test_start_frame_mime_comes_from_bytes(tmp_path):
    manifest, cfg = setup(tmp_path)
    (tmp_path / "media/fixture/p3.png").write_bytes(JPEG)
    fake = Fake()
    go_made(tmp_path, manifest, cfg, fake)
    body = fake.enqueues[0][3]
    assert body["input_image"].startswith("data:image/jpeg;base64,")


def test_start_frame_junk_bytes_refused_before_any_request(tmp_path):
    manifest, cfg = setup(tmp_path)
    (tmp_path / "media/fixture/p3.png").write_bytes(b"not an image at all")
    fake = Fake()
    with pytest.raises(ShotError, match="p3.png is not a PNG, JPEG or WebP image"):
        go_made(tmp_path, manifest, cfg, fake)
    assert fake.calls == []
    assert not (tmp_path / "ledger.csv").exists()


def test_missing_start_frame_is_error_with_zero_requests(tmp_path):
    fake = Fake()
    with pytest.raises(ShotError, match="start frame .*p3.png is missing; run panels first"):
        go(tmp_path, fake, setup={"start_frame": False})
    assert fake.calls == []


def test_config_defaults_and_wrong_type(tmp_path):
    manifest, cfg = setup(tmp_path, shot_section='[shot]\nseconds = "five"\n')
    with pytest.raises(ShotError, match="config: "):
        go_made(tmp_path, manifest, cfg, Fake())


def test_config_overrides_seconds(tmp_path):
    fake = Fake()
    go(tmp_path, fake, setup={"shot_section": "[shot]\nseconds = 8\n"})
    assert fake.enqueues[0][3]["duration"] == 8


# --- spend ----------------------------------------------------------------------------------


def test_without_confirmation_refused_no_enqueue(tmp_path):
    manifest, cfg = setup(tmp_path)
    before = manifest.read_bytes()
    fake = Fake()
    result, ledger, _ = go_made(tmp_path, manifest, cfg, fake, confirmed=False)
    assert result.status == "spend-refused"
    assert "unconfirmed" in result.message
    assert fake.enqueues == []
    assert manifest.read_bytes() == before
    assert rows(ledger) == []


# --- queue handling -------------------------------------------------------------------------


def test_succeeds_after_two_processing_polls(tmp_path):
    fake = Fake(
        gets=[
            state("queued"),
            state("processing"),
            state("succeeded", result_url=DOWNLOAD_URL),
        ]
    )
    result, ledger, clock = go(tmp_path, fake)
    assert result.status == "succeeded"
    assert result.generation_id == "gen-1"
    assert result.duration_s == 5.043
    assert len(fake.enqueues) == 1
    r = rows(ledger)
    assert len(r) == 1 and r[0].endswith(",queue:gen-1")
    assert clock.sleeps == [15, 15]
    video = tmp_path / "media/fixture/arrival.mp4"
    assert video.read_bytes() == MP4
    shot = shot_of(tmp_path / "episode.yaml")
    assert shot["duration"] == 5.043
    assert "generation_id" not in shot
    assert shot["model"] == PREFERRED
    assert not list(video.parent.glob("*.tmp"))


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled(tmp_path, status):
    fake = Fake(gets=[state("processing"), state(status, error_message="boom")])
    result, ledger, _ = go(tmp_path, fake)
    assert result.status == status
    assert "boom" in result.message
    assert len(rows(ledger)) == 1
    assert not (tmp_path / "media/fixture/arrival.mp4").exists()
    shot = shot_of(tmp_path / "episode.yaml")
    assert "generation_id" not in shot
    assert shot["model"] == PREFERRED


def test_timeout_keeps_id(tmp_path):
    fake = Fake(gets=[state("processing")] * 200)
    result, ledger, clock = go(tmp_path, fake)
    assert result.status == "timeout"
    assert "1200" in result.message and "rerun to resume" in result.message
    assert clock.now >= 1200
    assert len(rows(ledger)) == 1
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"


def test_unknown_status_is_failed_and_keeps_id(tmp_path):
    fake = Fake(gets=[state("weird")])
    result, _, _ = go(tmp_path, fake)
    assert result.status == "failed"
    assert "weird" in result.message
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"


def test_resume_polls_without_resubmitting(tmp_path):
    manifest, cfg = setup(tmp_path)
    edit_manifest(
        manifest,
        lambda d: d["arrival_shot"].update(generation_id="gen-1", model=PREFERRED),
    )
    fake = Fake(gets=[state("processing"), state("succeeded", result_url=DOWNLOAD_URL)])
    result, ledger, _ = go_made(tmp_path, manifest, cfg, fake)
    assert result.status == "succeeded"
    assert fake.enqueues == []
    assert fake.price_calls == []
    assert rows(ledger) == []
    assert "generation_id" not in shot_of(manifest)


def test_interrupted_poll_leaves_id_in_manifest(tmp_path):
    from toolbox_tales.oxen import OxenError

    fake = Fake(gets=[OxenError("network down")])
    result, ledger, _ = go(tmp_path, fake)
    assert result.status == "failed"
    assert "network down" in result.message
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"
    assert len(rows(ledger)) == 1


class _Interrupt(BaseException):
    """Not caught by `_poll` (it only handles OxenError), like a Ctrl-C mid-poll."""


def test_id_and_model_in_manifest_before_polling_starts(tmp_path):
    fake = Fake(gets=[_Interrupt("stop")])
    with pytest.raises(_Interrupt):
        go(tmp_path, fake)
    shot = shot_of(tmp_path / "episode.yaml")
    assert shot["generation_id"] == "gen-1"
    assert shot["model"] == PREFERRED
    assert len(fake.enqueues) == 1


def test_start_frame_path_escape_is_error(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (tmp_path / "escape.png").write_bytes(PNG)
    manifest, cfg = setup(outer)
    edit_manifest(manifest, lambda d: d["panels"][2].update(image="../escape.png"))
    fake = Fake()
    with pytest.raises(ShotError, match="start frame path escapes the manifest directory"):
        go_made(outer, manifest, cfg, fake)
    assert fake.calls == []


@pytest.mark.parametrize("bad", [7, "", None], ids=["int", "empty", "null"])
def test_corrupt_generation_id_is_error_zero_spend(tmp_path, bad):
    manifest, cfg = setup(tmp_path)
    edit_manifest(manifest, lambda d: d["arrival_shot"].update(generation_id=bad))
    before = manifest.read_bytes()
    fake = Fake()
    with pytest.raises(
        ShotError,
        match="shot: generation_id must be a non-empty string; remove it to resubmit",
    ):
        go_made(tmp_path, manifest, cfg, fake)
    assert fake.calls == []
    assert not (tmp_path / "ledger.csv").exists() or rows(Ledger(tmp_path / "ledger.csv")) == []
    assert manifest.read_bytes() == before


def test_poll_404_is_failed_keeps_id(tmp_path):
    fake = Fake(gets=[(404, b"x")])
    result, _, _ = go(tmp_path, fake)
    assert result.status == "failed"
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"


def test_enqueue_non_2xx_is_failed_without_ledger(tmp_path):
    manifest, cfg = setup(tmp_path)
    before = manifest.read_bytes()
    fake = Fake(enqueue=[(500, b"x")])
    result, ledger, _ = go_made(tmp_path, manifest, cfg, fake)
    assert result.status == "failed"
    assert rows(ledger) == []
    assert manifest.read_bytes() == before


def test_non_mp4_bytes_failed_no_file_id_kept(tmp_path):
    fake = Fake(download=(200, b"<html>not a video</html>"))
    result, _, _ = go(tmp_path, fake)
    assert result.status == "failed"
    assert "not an MP4" in result.message
    assert not (tmp_path / "media/fixture/arrival.mp4").exists()
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"


def test_missing_result_url_failed_keeps_id(tmp_path):
    fake = Fake(gets=[state("succeeded", result_url=None)])
    result, _, _ = go(tmp_path, fake)
    assert result.status == "failed"
    assert shot_of(tmp_path / "episode.yaml")["generation_id"] == "gen-1"


def test_measure_error_failed_file_stays(tmp_path):
    from toolbox_tales.voice import VoiceError

    def bad(path):
        raise VoiceError("no duration")

    manifest, cfg = setup(tmp_path)
    result, _, _ = go_made(tmp_path, manifest, cfg, Fake(), measure=bad)
    assert result.status == "failed"
    assert (tmp_path / "media/fixture/arrival.mp4").read_bytes() == MP4


def test_auth_headers(tmp_path):
    fake = Fake()
    go(tmp_path, fake)
    enqueue = fake.enqueues[0][2]
    get = fake.of("GET", "/ai/queue/gen-1")[0][2]
    download = fake.of("GET", "/result.mp4")[0][2]
    assert enqueue["Authorization"] == "Bearer key9"
    assert get["Authorization"] == "Bearer key9"
    assert "Authorization" not in download


# --- existing / paths -----------------------------------------------------------------------


def test_existing_video_makes_no_requests(tmp_path):
    manifest, cfg = setup(tmp_path)
    video = tmp_path / "media/fixture/arrival.mp4"
    video.write_bytes(MP4)
    fake = Fake()
    result, ledger, _ = go_made(tmp_path, manifest, cfg, fake)
    assert result.status == "exists"
    assert fake.calls == []
    assert rows(ledger) == []


def test_video_path_escape_is_error(tmp_path):
    manifest, cfg = setup(tmp_path)
    edit_manifest(manifest, lambda d: d["arrival_shot"].update(video="../x.mp4"))
    fake = Fake()
    with pytest.raises(ShotError, match="video path escapes the manifest directory"):
        go_made(tmp_path, manifest, cfg, fake)
    assert fake.calls == []


def test_missing_video_path_is_error(tmp_path):
    manifest, cfg = setup(tmp_path)
    edit_manifest(manifest, lambda d: d["arrival_shot"].pop("video"))
    with pytest.raises(ShotError, match="shot: no video path"):
        go_made(tmp_path, manifest, cfg, Fake())


def test_invalid_manifest_is_error(tmp_path):
    manifest, cfg = setup(tmp_path)
    edit_manifest(manifest, lambda d: d["arrival_shot"].update(after_panel=99))
    with pytest.raises(ShotError, match="after_panel"):
        go_made(tmp_path, manifest, cfg, Fake())


# --- render integration ---------------------------------------------------------------------


def test_render_places_shot_with_measured_length(tmp_path):
    require_smoke()
    manifest, cfg = setup(tmp_path)
    video = tmp_path / "media/fixture/arrival.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=d=1:s=64x64:r=10",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    real = video.read_bytes()
    fake = Fake(download=(200, real))
    video.unlink()
    from toolbox_tales.voice import ffprobe_duration

    result, _, _ = go_made(tmp_path, manifest, cfg, fake, measure=ffprobe_duration)
    assert result.status == "succeeded"
    assert 0.9 < result.duration_s < 1.2
    data = yaml.safe_load(manifest.read_text())
    assert not [e for e in load_and_validate(manifest) if not e.startswith("runtime:")]
    for n in range(1, 6):
        (tmp_path / f"media/fixture/p{n}.png").write_bytes(PNG)
    plan = build_plan(data, tmp_path)
    kinds = [s.kind for s in plan.segments]
    assert kinds.index("shot") == data["arrival_shot"]["after_panel"]
    shot = plan.segments[data["arrival_shot"]["after_panel"]]
    assert shot.source == video
    assert shot.duration_s == data["arrival_shot"]["duration"] == result.duration_s
