"""Conservative pre-download model disk budget tests."""

import math

import pytest

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.model_budget import (
    DOWNLOAD_HEADROOM_FRACTION,
    GIB,
    calculate_model_download_budget,
)


def test_accuracy_budget_preserves_reserve_and_adds_download_headroom() -> None:
    budget = calculate_model_download_budget(
        "accuracy",
        disk_total_bytes=500 * GIB,
        disk_free_bytes=100 * GIB,
    )

    assert budget.disk_reserve_bytes == 50 * GIB
    assert budget.missing_download_bytes == sum(
        item.expected_download_bytes for item in budget.items
    )
    assert budget.download_headroom_bytes == math.ceil(
        budget.missing_download_bytes * DOWNLOAD_HEADROOM_FRACTION
    )
    assert budget.required_before_download_bytes == (
        budget.disk_reserve_bytes
        + budget.missing_download_bytes
        + budget.download_headroom_bytes
    )
    assert budget.can_download is True
    assert budget.shortfall_bytes == 0
    assert budget.estimate_only is True


def test_present_models_are_removed_from_remaining_download_budget() -> None:
    without_cache = calculate_model_download_budget(
        "speed",
        disk_total_bytes=200 * GIB,
        disk_free_bytes=60 * GIB,
    )
    with_cache = calculate_model_download_budget(
        "speed",
        disk_total_bytes=200 * GIB,
        disk_free_bytes=60 * GIB,
        present={"asr_speed": True, "aligner": True, "ollama_editor": False},
    )

    assert with_cache.catalog_download_bytes == without_cache.catalog_download_bytes
    assert with_cache.missing_download_bytes == 5_200_000_000
    assert [item.present for item in with_cache.items] == [True, True, False]


def test_budget_blocks_before_download_and_reports_exact_shortfall() -> None:
    budget = calculate_model_download_budget(
        "all",
        disk_total_bytes=200 * GIB,
        disk_free_bytes=25 * GIB,
    )

    assert budget.disk_reserve_bytes == 20 * GIB
    assert budget.can_download is False
    assert budget.shortfall_bytes == (
        budget.required_before_download_bytes - budget.disk_free_bytes
    )
    assert budget.projected_free_after_bytes < budget.disk_reserve_bytes


@pytest.mark.parametrize(
    ("profile", "total", "free"),
    [
        ("unknown", 100 * GIB, 50 * GIB),
        ("accuracy", 0, 0),
        ("speed", 100 * GIB, 101 * GIB),
    ],
)
def test_invalid_budget_requests_fail_closed(profile, total, free) -> None:
    with pytest.raises(InvalidJobRequest):
        calculate_model_download_budget(
            profile,
            disk_total_bytes=total,
            disk_free_bytes=free,
        )
