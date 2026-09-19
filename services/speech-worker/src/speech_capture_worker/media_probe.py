"""Safe FFprobe boundary for validating an assembled audio source."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from speech_capture_worker.errors import MediaProbeUnavailable, SourceUndecodable


@dataclass(frozen=True)
class MediaProbeResult:
    duration_seconds: float
    audio_stream_count: int
    format_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_audio_source(
    source_path: Path,
    *,
    ffprobe_binary: str = "ffprobe",
    timeout_seconds: float = 60,
) -> MediaProbeResult:
    """Require a positive-duration source with at least one decodable audio stream."""

    try:
        completed = subprocess.run(
            [
                ffprobe_binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,duration",
                "-of",
                "json",
                str(source_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise MediaProbeUnavailable(
            "FFprobe is not available on the Worker.",
            details={"recommended_action": "Install or repair the Worker media runtime."},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceUndecodable(
            "The source media check did not finish in time.",
            details={"recommended_action": "Retry or inspect the source file."},
        ) from exc
    except UnicodeError as exc:
        raise SourceUndecodable(
            "The source media metadata could not be decoded safely.",
            details={"recommended_action": "Check the source file and retry verification."},
        ) from exc

    if completed.returncode != 0:
        raise SourceUndecodable(
            "The source could not be decoded as supported media.",
            details={"recommended_action": "Check the source file and try another export."},
        )

    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        format_payload = payload.get("format", {})
        audio_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ]
        duration = _resolve_duration_seconds(format_payload, audio_streams)
        format_name = format_payload.get("format_name")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceUndecodable(
            "The source media metadata was incomplete or invalid.",
            details={"recommended_action": "Check the source file and retry verification."},
        ) from exc

    if not audio_streams or duration <= 0:
        raise SourceUndecodable(
            "The source does not contain a positive-duration audio stream.",
            details={"recommended_action": "Choose a file that contains playable audio."},
        )

    return MediaProbeResult(
        duration_seconds=duration,
        audio_stream_count=len(audio_streams),
        format_name=str(format_name) if format_name else None,
    )


def _resolve_duration_seconds(
    format_payload: dict[str, Any],
    audio_streams: list[dict[str, Any]],
) -> float:
    audio_durations = _positive_finite_durations(
        [stream.get("duration") for stream in audio_streams]
    )
    if audio_durations:
        return max(audio_durations)
    format_durations = _positive_finite_durations([format_payload.get("duration")])
    return max(format_durations, default=0.0)


def _positive_finite_durations(candidates: list[Any]) -> list[float]:
    durations: list[float] = []
    for candidate in candidates:
        if candidate in {None, "N/A"}:
            continue
        duration = float(candidate)
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    return durations
