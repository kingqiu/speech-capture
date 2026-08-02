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
from speech_capture_worker.domain import JobCreateRequest, JobState
from speech_capture_worker.errors import (
    CredentialRotationExpired,
    CredentialRotationInvalid,
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


def test_authorized_device_can_pair_list_and_revoke_only_within_its_vault_scope(
    tmp_path,
) -> None:
    database = tmp_path / "security.sqlite3"
    with (
        DeviceSecurityStore(database) as security,
        JobStore(tmp_path / "worker.sqlite3") as jobs,
    ):
        primary_session = security.create_pairing_session(
            device_id="laptop_primary",
            allowed_vault_ids=("vault_one", "vault_two"),
        )
        primary = security.confirm_pairing(
            session_id=primary_session.session_id,
            pairing_code=primary_session.pairing_code,
        )
        hidden_session = security.create_pairing_session(
            device_id="laptop_hidden",
            allowed_vault_ids=("vault_private",),
        )
        security.confirm_pairing(
            session_id=hidden_session.session_id,
            pairing_code=hidden_session.pairing_code,
        )
        client = TestClient(
            create_app(
                store=jobs,
                credential_verifier=security,
                device_security_store=security,
            )
        )
        headers = {"Authorization": f"Bearer {primary.bearer_token}"}

        denied = client.post(
            "/v1/pairing/sessions",
            json={
                "device_id": "phone_denied",
                "allowed_vault_ids": ["vault_private"],
            },
            headers=headers,
        )
        created = client.post(
            "/v1/pairing/sessions",
            json={
                "device_id": "phone_secondary",
                "allowed_vault_ids": ["vault_two"],
            },
            headers=headers,
        )
        confirmation = client.post(
            "/v1/pairing/confirm",
            json={
                "session_id": created.json()["session_id"],
                "pairing_code": created.json()["pairing_code"],
            },
        )
        secondary_token = confirmation.json()["bearer_token"]
        devices = client.get("/v1/devices", headers=headers)
        hidden_revocation = client.delete("/v1/devices/laptop_hidden", headers=headers)
        revocation = client.delete("/v1/devices/phone_secondary", headers=headers)
        rejected = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_two"},
            headers={"Authorization": f"Bearer {secondary_token}"},
        )

    assert denied.status_code == 403
    assert "vault_private" not in denied.text
    assert created.status_code == 200
    assert confirmation.status_code == 200
    assert devices.status_code == 200
    assert {device["device_id"] for device in devices.json()["devices"]} == {
        "laptop_primary",
        "phone_secondary",
    }
    assert "bearer_token" not in devices.text
    assert "token_sha256" not in devices.text
    assert hidden_revocation.status_code == 404
    assert "vault_private" not in hidden_revocation.text
    assert revocation.json() == {"device_id": "phone_secondary", "revoked": True}
    assert rejected.status_code == 401


def test_two_phase_rotation_keeps_old_token_until_replacement_is_activated(
    tmp_path,
) -> None:
    database = tmp_path / "security.sqlite3"
    with DeviceSecurityStore(database) as security:
        session = security.create_pairing_session(
            device_id="laptop_rotate",
            allowed_vault_ids=("vault_one",),
        )
        original = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        prepared = security.prepare_credential_rotation("laptop_rotate")

        assert security.authenticate(original.bearer_token) is not None
        assert security.authenticate(prepared.bearer_token) is None
        with pytest.raises(CredentialRotationInvalid):
            security.activate_credential_rotation(
                device_id="laptop_rotate",
                replacement_token="scw_wrong",
            )

    with DeviceSecurityStore(database) as restarted:
        assert restarted.authenticate(original.bearer_token) is not None
        activated = restarted.activate_credential_rotation(
            device_id="laptop_rotate",
            replacement_token=prepared.bearer_token,
        )
        replayed = restarted.activate_credential_rotation(
            device_id="laptop_rotate",
            replacement_token=prepared.bearer_token,
        )

        assert restarted.authenticate(original.bearer_token) is None
        replacement_principal = restarted.authenticate(prepared.bearer_token)

    assert activated == replayed
    assert activated.generation == 2
    assert replacement_principal is not None
    assert replacement_principal.allowed_vault_ids == {"vault_one"}
    database_text = database.read_bytes().decode("utf-8", errors="ignore")
    assert original.bearer_token not in database_text
    assert prepared.bearer_token not in database_text


def test_expired_or_replaced_rotation_never_revokes_the_active_token(tmp_path) -> None:
    database = tmp_path / "security.sqlite3"
    with DeviceSecurityStore(database) as security:
        session = security.create_pairing_session(
            device_id="laptop_expiring",
            allowed_vault_ids=("vault_one",),
        )
        original = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        replaced = security.prepare_credential_rotation("laptop_expiring")
        current = security.prepare_credential_rotation("laptop_expiring")

        with pytest.raises(CredentialRotationInvalid):
            security.activate_credential_rotation(
                device_id="laptop_expiring",
                replacement_token=replaced.bearer_token,
            )

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE credential_rotations SET expires_at = ? WHERE rotation_id = ?",
        ("2000-01-01T00:00:00+00:00", current.rotation_id),
    )
    connection.commit()
    connection.close()

    with DeviceSecurityStore(database) as restarted:
        with pytest.raises(CredentialRotationExpired):
            restarted.activate_credential_rotation(
                device_id="laptop_expiring",
                replacement_token=current.bearer_token,
            )
        assert restarted.authenticate(original.bearer_token) is not None


def test_rotation_api_survives_lost_activation_response_and_switches_authentication(
    tmp_path,
) -> None:
    with (
        DeviceSecurityStore(tmp_path / "security.sqlite3") as security,
        JobStore(tmp_path / "worker.sqlite3") as jobs,
    ):
        session = security.create_pairing_session(
            device_id="laptop_api_rotate",
            allowed_vault_ids=("vault_one",),
        )
        original = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        client = TestClient(
            create_app(
                store=jobs,
                credential_verifier=security,
                device_security_store=security,
            )
        )
        old_headers = {"Authorization": f"Bearer {original.bearer_token}"}
        hidden = client.post(
            "/v1/devices/another_device/credential-rotations",
            json={},
            headers=old_headers,
        )
        prepared = client.post(
            "/v1/devices/laptop_api_rotate/credential-rotations",
            json={},
            headers=old_headers,
        )
        replacement_token = prepared.json()["bearer_token"]
        replacement_headers = {"Authorization": f"Bearer {replacement_token}"}
        before_activation = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_one"},
            headers=replacement_headers,
        )
        activation = client.post(
            "/v1/device-credential-rotations/activate",
            json={"device_id": "laptop_api_rotate"},
            headers=replacement_headers,
        )
        replay = client.post(
            "/v1/device-credential-rotations/activate",
            json={"device_id": "laptop_api_rotate"},
            headers=replacement_headers,
        )
        old_rejected = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_one"},
            headers=old_headers,
        )
        replacement_accepted = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_one"},
            headers=replacement_headers,
        )

    assert hidden.status_code == 404
    assert prepared.status_code == 200
    assert before_activation.status_code == 401
    assert activation.status_code == 200
    assert replay.json() == activation.json()
    assert old_rejected.status_code == 401
    assert replacement_accepted.status_code == 200


def test_security_schema_one_is_migrated_without_losing_credentials(tmp_path) -> None:
    database = tmp_path / "security.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE pairing_sessions (
            session_id TEXT PRIMARY KEY,
            code_sha256 TEXT NOT NULL,
            device_id TEXT NOT NULL,
            allowed_vault_ids_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL CHECK (failed_attempts >= 0),
            consumed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE device_credentials (
            credential_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            token_sha256 TEXT NOT NULL UNIQUE,
            allowed_vault_ids_json TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation > 0),
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT,
            UNIQUE (device_id, generation)
        );
        CREATE UNIQUE INDEX device_credentials_active_device_idx
        ON device_credentials (device_id) WHERE revoked_at IS NULL;
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    with DeviceSecurityStore(database) as security:
        session = security.create_pairing_session(
            device_id="laptop_migrated",
            allowed_vault_ids=("vault_one",),
        )
        issued = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        prepared = security.prepare_credential_rotation("laptop_migrated")

        assert security.authenticate(issued.bearer_token) is not None
        assert prepared.generation == 2

    connection = sqlite3.connect(database)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    rotation_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credential_rotations'"
    ).fetchone()
    connection.close()
    assert version == 2
    assert rotation_table == ("credential_rotations",)


def test_diagnostics_and_api_job_errors_are_scope_bounded_and_content_free(tmp_path) -> None:
    private_error = "/Users/private/client-meeting.wav: secret transcript sentence"
    with (
        DeviceSecurityStore(tmp_path / "security.sqlite3") as security,
        JobStore(tmp_path / "worker.sqlite3") as jobs,
    ):
        session = security.create_pairing_session(
            device_id="laptop_diagnostics",
            allowed_vault_ids=("vault_one",),
        )
        issued = security.confirm_pairing(
            session_id=session.session_id,
            pairing_code=session.pairing_code,
        )
        visible, _ = jobs.create_job(
            JobCreateRequest(
                vault_id="vault_one",
                source_display_name="visible-source.m4a",
                source_sha256="a" * 64,
                source_size_bytes=100,
            ),
            idempotency_key="visible-job",
        )
        jobs.transition_job(visible.job_id, JobState.UPLOADING, expected_revision=0)
        jobs.transition_job(
            visible.job_id,
            JobState.FAILED,
            expected_revision=1,
            error_code="PRIVATE_BACKEND_FAILED",
            error_message=private_error,
        )
        jobs.create_job(
            JobCreateRequest(
                vault_id="vault_hidden",
                source_display_name="hidden-source.m4a",
                source_sha256="b" * 64,
                source_size_bytes=100,
            ),
            idempotency_key="hidden-job",
        )
        client = TestClient(
            create_app(
                store=jobs,
                credential_verifier=security,
                device_security_store=security,
            )
        )
        headers = {"Authorization": f"Bearer {issued.bearer_token}"}
        diagnostics = client.get("/v1/diagnostics/summary", headers=headers)
        jobs_response = client.get(
            "/v1/jobs",
            params={"vault_id": "vault_one"},
            headers=headers,
        )

    assert diagnostics.status_code == 200
    assert diagnostics.json() == {
        "worker_version": "0.1.0a0",
        "protocol_version": "1.0.0",
        "worker_database_ok": True,
        "security_database_ok": True,
        "authorized_vault_count": 1,
        "visible_device_count": 1,
        "visible_job_count": 1,
        "job_state_counts": {"failed": 1},
    }
    assert "vault_one" not in diagnostics.text
    assert "vault_hidden" not in diagnostics.text
    assert "source" not in diagnostics.text
    assert private_error not in diagnostics.text
    assert jobs_response.status_code == 200
    assert private_error not in jobs_response.text
    assert jobs_response.json()["jobs"][0]["last_error_message"] == (
        "The Worker could not complete this processing stage safely."
    )
