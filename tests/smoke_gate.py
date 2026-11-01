"""Skip/fail rule shared by the real-ffmpeg smoke tests.

Locally a missing ffmpeg, ffprobe or drawtext filter skips the smoke tests. Under CI the
same conditions fail them, so a CI image without drawtext cannot go green silently.
"""

import os
import shutil
import subprocess

import pytest


def decide(*, has_ffmpeg: bool, has_ffprobe: bool, has_drawtext: bool, ci: bool) -> str:
    """Return "run", "skip" or "fail"."""
    if has_ffmpeg and has_ffprobe and has_drawtext:
        return "run"
    return "fail" if ci else "skip"


def _has_drawtext(ffmpeg: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, check=False
        )
    except OSError:
        return False
    return b"drawtext" in result.stdout


def require_smoke() -> None:
    ffmpeg = shutil.which("ffmpeg")
    verdict = decide(
        has_ffmpeg=ffmpeg is not None,
        has_ffprobe=shutil.which("ffprobe") is not None,
        has_drawtext=ffmpeg is not None and _has_drawtext(ffmpeg),
        ci=bool(os.environ.get("CI")),
    )
    message = "ffmpeg, ffprobe and the drawtext filter are required for the smoke tests"
    if verdict == "fail":
        pytest.fail(message)
    if verdict == "skip":
        pytest.skip(message)
