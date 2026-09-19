import json
import subprocess

import pytest

from speech_capture_worker.errors import MediaProbeUnavailable, SourceUndecodable
from speech_capture_worker.media_probe import probe_audio_source


def completed_process(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def test_probe_accepts_positive_duration_audio(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: completed_process(
            {
                "format": {"duration": "12.500", "format_name": "mov,mp4,m4a"},
                "streams": [
                    {"codec_type": "video", "duration": "12.5"},
                    {"codec_type": "audio", "duration": "12.4"},
                ],
            }
        ),
    )

    result = probe_audio_source(tmp_path / "private-recording.m4a")

    assert result.duration_seconds == 12.4
    assert result.audio_stream_count == 1
    assert result.format_name == "mov,mp4,m4a"


@pytest.mark.parametrize(
    "payload",
    [
        {"format": {"duration": "10"}, "streams": [{"codec_type": "video"}]},
        {"format": {"duration": "0"}, "streams": [{"codec_type": "audio"}]},
        {"format": {"duration": "Infinity"}, "streams": [{"codec_type": "audio"}]},
        {"format": {"duration": "NaN"}, "streams": [{"codec_type": "audio"}]},
        {"format": {}, "streams": [{"codec_type": "audio", "duration": "N/A"}]},
    ],
)
def test_probe_rejects_missing_or_zero_duration_audio(monkeypatch, tmp_path, payload) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: completed_process(payload),
    )

    with pytest.raises(SourceUndecodable):
        probe_audio_source(tmp_path / "private-recording.m4a")


def test_probe_failure_does_not_expose_path_or_ffprobe_stderr(monkeypatch, tmp_path) -> None:
    private_path = tmp_path / "private-recording.m4a"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: completed_process(
            {},
            returncode=1,
            stderr=f"decoder failed at {private_path}",
        ),
    )

    with pytest.raises(SourceUndecodable) as caught:
        probe_audio_source(private_path)

    assert str(private_path) not in caught.value.message
    assert str(private_path) not in str(caught.value.details)


def test_missing_ffprobe_has_actionable_stable_error(monkeypatch, tmp_path) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(MediaProbeUnavailable) as caught:
        probe_audio_source(tmp_path / "private-recording.m4a")

    assert caught.value.code == "MEDIA_PROBE_UNAVAILABLE"
    assert "recommended_action" in caught.value.details


def test_probe_timeout_is_reported_without_source_details(monkeypatch, tmp_path) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=60)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(SourceUndecodable) as caught:
        probe_audio_source(tmp_path / "private-recording.m4a")

    assert caught.value.code == "SOURCE_UNDECODABLE"
