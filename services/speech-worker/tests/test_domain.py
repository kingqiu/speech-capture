import pytest

from speech_capture_worker.domain import (
    ALLOWED_TRANSITIONS,
    RECOVERY_TARGETS,
    JobCreateRequest,
    JobState,
    ModelProfile,
    ensure_transition_allowed,
)
from speech_capture_worker.errors import InvalidJobRequest, InvalidTransition


def request(**overrides) -> JobCreateRequest:
    values = {
        "vault_id": "vault_primary",
        "source_display_name": "meeting.m4a",
        "source_sha256": "a" * 64,
        "source_size_bytes": 1024,
        "model_profile": ModelProfile.ACCURACY,
    }
    values.update(overrides)
    return JobCreateRequest(**values)


def test_every_state_has_an_explicit_transition_set() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(JobState)


def test_happy_path_without_diarization_is_allowed() -> None:
    path = [
        JobState.CREATED,
        JobState.UPLOADING,
        JobState.VERIFYING,
        JobState.QUEUED,
        JobState.PREPROCESSING,
        JobState.TRANSCRIBING,
        JobState.ALIGNING,
        JobState.STRUCTURING,
        JobState.QUALITY_CHECK,
        JobState.PROCESSED,
        JobState.PUBLISHING,
        JobState.PUBLISHED,
    ]

    for current, target in zip(path, path[1:]):
        ensure_transition_allowed(current, target)


def test_invalid_state_jump_is_rejected() -> None:
    with pytest.raises(InvalidTransition) as caught:
        ensure_transition_allowed(JobState.CREATED, JobState.PROCESSED)

    assert caught.value.code == "INVALID_JOB_TRANSITION"


def test_terminal_states_have_no_outgoing_transition() -> None:
    assert ALLOWED_TRANSITIONS[JobState.PUBLISHED] == frozenset()
    assert ALLOWED_TRANSITIONS[JobState.CANCELLED] == frozenset()


def test_recovery_targets_only_active_processing_states() -> None:
    assert RECOVERY_TARGETS[JobState.VERIFYING] is JobState.UPLOADING
    assert RECOVERY_TARGETS[JobState.TRANSCRIBING] is JobState.QUEUED
    assert RECOVERY_TARGETS[JobState.PUBLISHING] is JobState.PROCESSED
    assert JobState.PAUSED not in RECOVERY_TARGETS
    assert JobState.PROCESSED not in RECOVERY_TARGETS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vault_id", "../../vault"),
        ("source_display_name", "../meeting.m4a"),
        ("source_display_name", "folder/meeting.m4a"),
        ("source_display_name", "meeting\n.m4a"),
        ("source_sha256", "not-a-checksum"),
        ("source_size_bytes", 0),
        ("source_size_bytes", True),
        ("options", []),
        ("language_hint", "Chinese\nEnglish"),
        ("content_type_override", "meeting/../../other"),
    ],
)
def test_invalid_job_request_is_rejected(field, value) -> None:
    with pytest.raises(InvalidJobRequest):
        request(**{field: value}).validate()


def test_valid_job_request_passes_validation() -> None:
    request(
        language_hint="Chinese",
        content_type_override="meeting",
        options={"timestamps": True},
    ).validate()
