import pytest

from speech_capture_worker.domain import ModelProfile, ResourceStatus
from speech_capture_worker.errors import InvalidJobRequest, ResourceBlocked
from speech_capture_worker.resources import (
    GIB,
    DiskSnapshot,
    MemorySnapshot,
    ResourcePolicy,
    estimate_job_disk_bytes,
    require_resource_preflight,
)


def disk(*, total_gib=256, free_gib=100):
    return DiskSnapshot(total_bytes=total_gib * GIB, free_bytes=free_gib * GIB)


def memory(*, total_gib=32, available_gib=20, used_percent=40.0, swap_gib=0):
    return MemorySnapshot(
        total_bytes=total_gib * GIB,
        available_bytes=available_gib * GIB,
        used_percent=used_percent,
        swap_used_bytes=swap_gib * GIB,
    )


def test_ready_preflight() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(),
        memory=memory(),
        estimated_required_bytes=5 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert report.status is ResourceStatus.READY
    assert report.can_start is True
    assert report.issues == ()


def test_disk_below_reserve_blocks() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(free_gib=25),
        memory=memory(),
        estimated_required_bytes=5 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert report.status is ResourceStatus.BLOCKED
    assert {issue.code for issue in report.issues} == {"DISK_RESERVE_TOO_LOW"}


def test_disk_near_reserve_warns() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(free_gib=32),
        memory=memory(),
        estimated_required_bytes=2 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert report.status is ResourceStatus.WARNING
    assert "DISK_RESERVE_WARNING" in {issue.code for issue in report.issues}


def test_current_memory_pressure_can_block() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(),
        memory=memory(available_gib=1, used_percent=97.0),
        estimated_required_bytes=1 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert report.status is ResourceStatus.BLOCKED
    assert "MEMORY_PRESSURE_BLOCKED" in {issue.code for issue in report.issues}


def test_accuracy_profile_can_block_on_total_memory() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(),
        memory=memory(total_gib=12, available_gib=8),
        estimated_required_bytes=1 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert "MODEL_PROFILE_MEMORY_TOO_LOW" in {issue.code for issue in report.issues}


def test_speed_profile_accepts_same_total_memory() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(),
        memory=memory(total_gib=12, available_gib=8),
        estimated_required_bytes=1 * GIB,
        model_profile=ModelProfile.SPEED,
    )

    assert report.status is ResourceStatus.READY


def test_memory_and_swap_warning_are_visible() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(),
        memory=memory(available_gib=5, used_percent=86.0, swap_gib=5),
        estimated_required_bytes=1 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    assert report.status is ResourceStatus.WARNING
    assert {issue.code for issue in report.issues} == {
        "MEMORY_PRESSURE_WARNING",
        "SWAP_USAGE_WARNING",
    }


def test_resource_blocked_error_contains_actions() -> None:
    report = ResourcePolicy().evaluate(
        disk=disk(free_gib=20),
        memory=memory(),
        estimated_required_bytes=1 * GIB,
        model_profile=ModelProfile.ACCURACY,
    )

    with pytest.raises(ResourceBlocked) as caught:
        require_resource_preflight(report)

    assert caught.value.details["issues"][0]["action"]


def test_disk_estimate_includes_staging_pcm_work_and_artifacts() -> None:
    source_size = 100 * 1024**2
    estimated = estimate_job_disk_bytes(source_size_bytes=source_size, duration_sec=3600)

    assert estimated > source_size + 3600 * 16_000 * 2


@pytest.mark.parametrize(
    ("source_size_bytes", "duration_sec"),
    [(0, 60.0), (True, 60.0), (1024, 0.0), (1024, float("nan"))],
)
def test_invalid_disk_estimate_input_is_rejected(source_size_bytes, duration_sec) -> None:
    with pytest.raises(InvalidJobRequest):
        estimate_job_disk_bytes(
            source_size_bytes=source_size_bytes,
            duration_sec=duration_sec,
        )
