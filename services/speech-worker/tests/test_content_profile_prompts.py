"""B3 prompt-adapter tests for the bundled meeting profile."""

from __future__ import annotations

from dataclasses import replace

import pytest

from speech_capture_worker.content_profile_prompts import (
    ContentProfilePromptError,
    MeetingProfilePrompts,
    load_bundled_meeting_profile,
)


def test_bundled_meeting_profile_loads_all_registered_prompt_slots() -> None:
    bundle = load_bundled_meeting_profile()
    prompts = MeetingProfilePrompts.from_bundle(bundle)

    assert bundle.profile_id == "speech-capture/meeting"
    assert bundle.profile_version == "2026-08-29.2"
    assert bundle.manifest["fallback_profile"]["profile_version"] == "2026-08-29.1"
    assert "信息优先级不由发言长度或出现顺序决定" in prompts.extraction
    assert "输出顺序和阅读逻辑" in prompts.synthesis
    assert "质量复核必须检查会议主线" in prompts.quality_edit
    assert "只核对会议结果栏目" in prompts.meeting_outcomes
    assert "不得声称达成多项决定" in prompts.synthesis
    assert "质量编辑是保守修订" in prompts.quality_edit
    assert "meeting.quality.results_preserved" in bundle.validation_policy[
        "registered_validators"
    ]


def test_meeting_prompt_adapter_rejects_a_nonmeeting_bundle() -> None:
    meeting = load_bundled_meeting_profile()
    nonmeeting = replace(meeting, content_type="interview")

    with pytest.raises(ContentProfilePromptError, match="meeting bundle"):
        MeetingProfilePrompts.from_bundle(nonmeeting)
