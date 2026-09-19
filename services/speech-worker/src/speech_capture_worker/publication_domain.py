"""Durable publication lease and acknowledgement domain types."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from speech_capture_worker.domain import SAFE_IDENTIFIER_PATTERN, SHA256_PATTERN
from speech_capture_worker.errors import InvalidJobRequest

MIN_PUBLICATION_LEASE_SECONDS = 30
MAX_PUBLICATION_LEASE_SECONDS = 900
DEFAULT_PUBLICATION_LEASE_SECONDS = 120
MAX_VAULT_RELATIVE_PATH_CHARACTERS = 1024


class PublicationLeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    RECOVERED = "recovered"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True)
class PublicationLeaseRecord:
    sequence: int
    lease_id: str
    job_id: str
    generation: int
    publisher_id: str
    target_relative_path: str
    manifest_sha256: str
    state: PublicationLeaseState
    expires_at: str
    created_at: str
    updated_at: str
    completed_at: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PublicationReceiptRecord:
    job_id: str
    lease_id: str
    publisher_id: str
    target_relative_path: str
    manifest_sha256: str
    published_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_publication_lease_request(
    *,
    publisher_id: str,
    target_relative_path: str,
    manifest_sha256: str,
    lease_seconds: int,
) -> None:
    validate_publisher_id(publisher_id)
    validate_vault_relative_path(target_relative_path)
    if not isinstance(manifest_sha256, str) or not SHA256_PATTERN.fullmatch(manifest_sha256):
        raise InvalidJobRequest("manifest_sha256 must be lowercase SHA-256.")
    validate_lease_seconds(lease_seconds)


def validate_publisher_id(value: str) -> str:
    if not isinstance(value, str) or not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
        raise InvalidJobRequest("publisher_id contains unsupported characters.")
    return value


def validate_lease_seconds(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < MIN_PUBLICATION_LEASE_SECONDS
        or value > MAX_PUBLICATION_LEASE_SECONDS
    ):
        raise InvalidJobRequest("publication lease seconds must be between 30 and 900.")


def validate_vault_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_VAULT_RELATIVE_PATH_CHARACTERS
        or "\x00" in value
        or "\\" in value
        or any(not character.isprintable() for character in value)
    ):
        raise InvalidJobRequest("Vault output path must be a safe relative POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidJobRequest("Vault output path must not escape its configured root.")
    if any(re.fullmatch(r"[A-Za-z]:", part) for part in path.parts):
        raise InvalidJobRequest("Vault output path must not contain a device path.")
    normalized = path.as_posix()
    if normalized != value:
        raise InvalidJobRequest("Vault output path must already be normalized.")
    return normalized
