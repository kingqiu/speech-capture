"""Credential verifier security-boundary tests."""

import pytest

from speech_capture_worker.api_auth import ApiCredential, ApiPrincipal, CredentialVerifier


def test_credentials_store_only_digest_and_authorize_explicit_vaults() -> None:
    token = "token-abcdefghijklmnopqrstuvwxyz0123456789"
    principal = ApiPrincipal(
        device_id="device_primary",
        allowed_vault_ids=frozenset({"vault_one", "vault_two"}),
    )
    credential = ApiCredential.from_plaintext(token, principal)
    verifier = CredentialVerifier((credential,))

    assert token not in repr(credential)
    assert len(credential.token_sha256) == 64
    assert verifier.authenticate(token) == principal
    assert verifier.authenticate(f"{token}x") is None
    assert principal.allows("vault_one") is True
    assert principal.allows("vault_other") is False


@pytest.mark.parametrize("token", ["short", "contains whitespace " * 3, "x" * 513])
def test_unsafe_plaintext_tokens_are_rejected(token: str) -> None:
    principal = ApiPrincipal(
        device_id="device_primary",
        allowed_vault_ids=frozenset({"vault_one"}),
    )

    with pytest.raises(ValueError, match="Bearer tokens"):
        ApiCredential.from_plaintext(token, principal)


def test_principals_require_safe_device_and_vault_identifiers() -> None:
    with pytest.raises(ValueError, match="device_id"):
        ApiPrincipal(
            device_id="../device",
            allowed_vault_ids=frozenset({"vault_one"}),
        )
    with pytest.raises(ValueError, match="vault"):
        ApiPrincipal(
            device_id="device_primary",
            allowed_vault_ids=frozenset({"../vault"}),
        )
