import json

import pytest

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.recording_context import (
    apply_text_corrections,
    confirmed_term_corrections,
    normalize_recording_context,
)
from speech_capture_worker.structuring_execution import OllamaStructuringEngine


def test_free_form_recording_context_preserves_paragraphs_and_rejects_controls() -> None:
    assert normalize_recording_context("  第一段\n\n第二段\t补充  ") == (
        "第一段\n\n第二段\t补充"
    )
    assert normalize_recording_context(" \n ") is None
    with pytest.raises(InvalidJobRequest):
        normalize_recording_context("背景\x00内容")


def test_only_explicit_confirmed_terms_become_deterministic_corrections() -> None:
    texts = ["聚一堂正在转型。", "今天与聚一堂开会。", "提到聚聚一堂。"]

    assert confirmed_term_corrections("正确公司名是聚衣堂", texts) == {
        "聚一堂": "聚衣堂",
        "聚聚一堂": "聚衣堂",
    }
    assert confirmed_term_corrections("这是关于聚衣堂的会议", texts) == {}
    assert confirmed_term_corrections(
        "错误识别为聚一堂，正确应该是聚衣堂", ["只出现一次聚一堂。"]
    ) == {"聚一堂": "聚衣堂"}


def test_context_correction_changes_only_derived_values() -> None:
    value = {
        "document": {"title": "聚一堂会议"},
        "transcript_edits": ["聚一堂提供资料"],
        "segment_id": "seg_0001",
    }

    corrected, count = apply_text_corrections(value, {"聚一堂": "聚衣堂"})

    assert count == 2
    assert corrected["document"]["title"] == "聚衣堂会议"
    assert corrected["transcript_edits"] == ["聚衣堂提供资料"]
    assert value["document"]["title"] == "聚一堂会议"


def test_ollama_prompt_marks_recording_context_as_reference_not_evidence(monkeypatch) -> None:
    engine = OllamaStructuringEngine(model="qwen3:14b", editor_model="qwen3:8b")
    captured = {}

    def generate(prompt, **_):
        captured["prompt"] = prompt
        return json.dumps([{"segment_id": "seg_1", "text": "聚衣堂正在转型。"}])

    monkeypatch.setattr(engine, "_generate", generate)
    engine.set_recording_context("正确公司名是聚衣堂")

    result = engine.polish_transcript_batch(
        [{"segment_id": "seg_1", "text": "聚一堂正在转型。"}]
    )

    assert result[0]["text"] == "聚衣堂正在转型。"
    assert "正确公司名是聚衣堂" in captured["prompt"]
    assert "未经逐字稿独立证实" in captured["prompt"]
    assert "不能单独创建决定、待办、事实" in captured["prompt"]
