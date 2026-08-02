"""Injectable credential verification and Vault authorization for the Worker API."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from speech_capture_worker.domain import SAFE_IDENTIFIER_PATTERN

MIN_BEARER_TOKEN_CHARACTERS = 32
MAX_BEARER_TOKEN_CHARACTERS = 512


@dataclass(frozen=True)
class ApiPrincipal:
    device_id: str
    allowed_vault_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(self.device_id):
            raise ValueError("device_id must be a safe identifier.")
        if not self.allowed_vault_ids:
            raise ValueError("An API principal must be authorized for at least one Vault.")
        if any(
            not SAFE_IDENTIFIER_PATTERN.fullmatch(vault_id)
            for vault_id in self.allowed_vault_ids
        ):
            raise ValueError("allowed_vault_ids contains an unsafe identifier.")

    def allows(self, vault_id: str) -> bool:
        return vault_id in self.allowed_vault_ids


@dataclass(frozen=True)
class ApiCredential:
    token_sha256: str
    principal: ApiPrincipal

    @classmethod
    def from_plaintext(cls, token: str, principal: ApiPrincipal) -> ApiCredential:
        _validate_plaintext_token(token)
        return cls(
            token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            principal=principal,
        )


class CredentialVerifier:
    """Verify opaque bearer tokens without retaining their plaintext values."""

    def __init__(self, credentials: tuple[ApiCredential, ...]) -> None:
        if not credentials:
            raise ValueError("At least one API credential is required.")
        digests = [credential.token_sha256 for credential in credentials]
        if len(digests) != len(set(digests)):
            raise ValueError("API credential token digests must be unique.")
        self._credentials = credentials

    def authenticate(self, token: str) -> ApiPrincipal | None:
        if not _plaintext_token_shape_is_valid(token):
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: ApiPrincipal | None = None
        for credential in self._credentials:
            if secrets.compare_digest(candidate, credential.token_sha256):
                matched = credential.principal
        return matched


def _validate_plaintext_token(token: str) -> None:
    if not _plaintext_token_shape_is_valid(token):
        raise ValueError(
            "Bearer tokens must contain 32 to 512 printable non-whitespace characters."
        )


def _plaintext_token_shape_is_valid(token: object) -> bool:
    return (
        isinstance(token, str)
        and MIN_BEARER_TOKEN_CHARACTERS <= len(token) <= MAX_BEARER_TOKEN_CHARACTERS
        and all(character.isprintable() and not character.isspace() for character in token)
    )
