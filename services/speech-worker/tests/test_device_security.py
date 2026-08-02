"""Durable pairing and device credential lifecycle tests."""

import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from speech_capture_worker.api import create_app
from speech_capture_worker.device_security import (
    MAX_PAIRING_ATTEMPTS,
    DeviceSecurityStore,
)
from speech_capture_worker.errors import (
    DeviceAlreadyPaired,
    PairingCodeInvalid,
    PairingSessionExpired,
)
from speech_capture_worker.job_store import JobStore


def test_pairing_issues_token_once_and_authentication_survives_restart(tmp_path) -> None:
    database = tmp_path / "security.sqlite3"
    with DeviceSecurityStore(database) as security:
        session = security.create_pairing_session(
            device_id="laptop_primary",
            allowed_vault_ids=("vault_two", "vault_one"),
        )
        issued = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        principal = security.authenticate(issued.bearer_token)

        assert session.allowed_vault_ids == ("vault_one", "vault_two")
        assert issued.bearer_token.startswith("scw_")
        assert principal is not None
        assert principal.device_id == "laptop_primary"
        assert principal.allowed_vault_ids == {"vault_one", "vault_two"}
        with pytest.raises(PairingSessionExpired):
            security.confirm_pairing(
                session_id=session.session_id,
                pairing_code=session.pairing_code,
            )

    with DeviceSecurityStore(database) as restarted:
        restored = restarted.authenticate(issued.bearer_token)
        devices = restarted.list_devices()

    assert restored is not None
    assert restored.device_id == "laptop_primary"
    assert len(devices) == 1
    assert devices[0].last_used_at is not None
    assert issued.bearer_token not in database.read_bytes().decode("utf-8", errors="ignore")
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_wrong_pairing_codes_are_rate_limited_and_persisted(tmp_path) -> None:
    database = tmp_path / "security.sqlite3"
    with DeviceSecurityStore(database) as security:
        session = security.create_pairing_session(
            device_id="laptop_rate_limit",
            allowed_vault_ids=("vault_one",),
        )
        for _ in range(MAX_PAIRING_ATTEMPTS):
            with pytest.raises(PairingCodeInvalid):
                security.confirm_pairing(
                    session_id=session.session_id,
                    pairing_code="wrong-code",
                )
        with pytest.raises(PairingSessionExpired):
            security.confirm_pairing(
                session_id=session.session_id,
                pairing_code=session.pairing_code,
            )

    connection = sqlite3.connect(database)
    attempts = connection.execute(
        "SELECT failed_attempts FROM pairing_sessions WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()[0]
    connection.close()
    assert attempts == MAX_PAIRING_ATTEMPTS


def test_active_device_cannot_be_silently_repaired_and_revocation_is_immediate(tmp_path) -> None:
    with DeviceSecurityStore(tmp_path / "security.sqlite3") as security:
        first = security.create_pairing_session(
            device_id="laptop_primary",
            allowed_vault_ids=("vault_one",),
        )
        issued = security.confirm_pairing(
            session_id=first.session_id,
            pairing_code=first.pairing_code,
        )
        with pytest.raises(DeviceAlreadyPaired):
            security.create_pairing_session(
                device_id="laptop_primary",
                allowed_vault_ids=("vault_one",),
            )

        assert security.revoke_device("laptop_primary") is True
        assert security.authenticate(issued.bearer_token) is None
        assert security.revoke_device("laptop_primary") is False

        second = security.create_pairing_session(
            device_id="laptop_primary",
            allowed_vault_ids=("vault_two",),
        )
        reissued = security.confirm_pairing(
            session_id=second.session_id,
            pairing_code=second.pairing_code,
        )

    assert reissued.generation == 2
    assert reissued.bearer_token != issued.bearer_token


def test_pairing_confirmation_api_issues_a_working_digest_backed_credential(tmp_path) -> None:
    with (
        DeviceSecurityStore(tmp_path / "security.sqlite3") as security,
        JobStore(tmp_path / "worker.sqlite3") as jobs,
    ):
        session = security.create_pairing_session(
            device_id="laptop_api",
            allowed_vault_ids=("vault_one",),
        )
        client = TestClient(
            create_app(
                store=jobs,
                credential_verifier=security,
                device_security_store=security,
            )
        )
        confirmation = client.post(
            "/v1/pairing/confirm",
            json={
                "session_id": session.session_id,
                "pairing_code": session.pairing_code,
            },
        )
        token = confirmation.json()["bearer_token"]
        authorized = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_one"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert confirmation.status_code == 200
    assert confirmation.json()["device_id"] == "laptop_api"
    assert authorized.status_code == 200
    assert authorized.json() == {"jobs": []}
