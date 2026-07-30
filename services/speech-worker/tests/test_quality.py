import pytest

from speech_capture_worker.asr_probe import sanitize_progress_event
from speech_capture_worker.quality import (
    measure_character_error_rate,
    normalize_for_character_error_rate,
)


def test_normalization_ignores_case_width_spacing_and_punctuation() -> None:
    assert normalize_for_character_error_rate("Ｓｐｅｅｃｈ，捕捉！") == "speech捕捉"


def test_exact_normalized_match_has_zero_error() -> None:
    report = measure_character_error_rate(
        "Speech Capture，保留原始逐字稿。",
        "speech capture 保留原始逐字稿",
    )

    assert report.character_error_rate == 0.0
    assert report.distance == 0


def test_substitution_is_measured() -> None:
    report = measure_character_error_rate("模型转写", "原型撰写")

    assert report.distance == 2
    assert report.character_error_rate == 0.5


def test_deleted_english_sentence_raises_error_rate() -> None:
    report = measure_character_error_rate(
        "开始 Speech Capture keeps evidence 最后",
        "开始最后",
    )

    assert report.character_error_rate > 0.5


def test_empty_reference_is_rejected() -> None:
    with pytest.raises(ValueError):
        measure_character_error_rate("……", "anything")


def test_progress_log_drops_transcript_and_unknown_fields() -> None:
    safe = sanitize_progress_event(
        {
            "event": "chunk_completed",
            "chunk_index": 1,
            "text": "private transcript",
            "future_unknown_payload": "private by default",
        }
    )

    assert safe == {"event": "chunk_completed", "chunk_index": 1}
