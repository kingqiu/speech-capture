"""Fail-closed import check for model backends used by the frozen runtime."""

from __future__ import annotations


def verify_runtime_model_dependencies() -> dict[str, object]:
    """Import every dynamically loaded model entry point without loading weights."""
    from mlx_qwen3_asr import ForcedAligner, Session
    from pyannote.audio import Pipeline
    from pyannote.audio.pipelines import SpeakerDiarization, VoiceActivityDetection

    return {
        "model_runtime_ready": True,
        "components": {
            "asr": Session.__name__,
            "forced_alignment": ForcedAligner.__name__,
            "pyannote_pipeline": Pipeline.__name__,
            "speaker_diarization": SpeakerDiarization.__name__,
            "voice_activity_detection": VoiceActivityDetection.__name__,
        },
    }
