"""Publication protocol value validation tests."""

import pytest

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.publication_domain import (
    validate_lease_seconds,
    validate_publisher_id,
    validate_vault_relative_path,
)


@pytest.mark.parametrize(
    "value",
    [
        "/absolute/path",
        "../escape",
        "safe/../escape",
        "safe//path",
        "safe/path/",
        "safe\\path",
        "C:/device/path",
        "safe/\x00path",
        "safe/\npath",
    ],
)
def test_vault_relative_path_rejects_noncanonical_or_escaping_values(value) -> None:
    with pytest.raises(InvalidJobRequest):
        validate_vault_relative_path(value)


def test_publication_domain_accepts_canonical_values() -> None:
    assert validate_vault_relative_path("Speech/2026/08/package") == (
        "Speech/2026/08/package"
    )
    assert validate_publisher_id("local_vault-01") == "local_vault-01"
    validate_lease_seconds(120)


@pytest.mark.parametrize("value", [29, 901, True, 120.0])
def test_publication_lease_duration_is_bounded_integer(value) -> None:
    with pytest.raises(InvalidJobRequest):
        validate_lease_seconds(value)
