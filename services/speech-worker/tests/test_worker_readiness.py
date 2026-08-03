"""Content-free Worker readiness collection and authenticated API tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from speech_capture_worker.api import create_app
from speech_capture_worker.api_auth import ApiCredential, ApiPrincipal, CredentialVerifier
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.resources import DiskSnapshot, MemorySnapshot
from speech_capture_worker.worker_readiness import collect_worker_readiness

GIB = 1024**3
TOKEN = "readiness-token-abcdefghijklmnopqrstuvwxyz0123456789"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def _verifier() -> CredentialVerifier:
    principal = ApiPrincipal(
        device_id="device_readiness",
        allowed_vault_ids=frozenset({"vault_primary"}),
    )
    return CredentialVerifier((ApiCredential.from_plaintext(TOKEN, principal),))


def _snapshot(tmp_path, **overrides):
    parameters = {
        "worker_database_ok": True,
        "security_database_ok": True,
        "endpoint_mode": "private_tls",
        "tls_enabled": True,
        "disk": DiskSnapshot(total_bytes=500 * GIB, free_bytes=200 * GIB),
        "memory": MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=20 * GIB,
            used_percent=35.0,
            swap_used_bytes=0,
        ),
        "storage_ready": True,
        "ffmpeg_available": True,
        "ffprobe_available": True,
        "ollama_reachable": True,
        "active_model_profile": "all",
        "inspect_activation": False,
    }
    parameters.update(overrides)
    return collect_worker_readiness(tmp_path / "runtime", **parameters)


def test_ready_snapshot_contains_only_actionable_content_free_facts(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    payload = snapshot.to_dict()
    serialized = json.dumps(payload, sort_keys=True).lower()

    assert snapshot.state == "ready"
    assert [profile.state for profile in snapshot.profiles] == ["ready", "ready"]
    assert all(profile.can_start for profile in snapshot.profiles)
    assert payload["endpoint_mode"] == "private_tls"
    assert payload["disk_reserve_bytes"] == 50 * GIB
    assert "/users/" not in serialized
    assert "runtime" not in serialized
    assert ".wav" not in serialized
    assert "source_display_name" not in serialized


def test_warning_and_blocked_readiness_are_profile_specific(tmp_path) -> None:
    warning = _snapshot(
        tmp_path,
        active_model_profile="accuracy",
        memory=MemorySnapshot(
            total_bytes=32 * GIB,
            available_bytes=4 * GIB,
            used_percent=80.0,
            swap_used_bytes=0,
        ),
    )
    blocked = _snapshot(
        tmp_path,
        worker_database_ok=False,
        ffmpeg_available=False,
    )

    assert warning.state == "warning"
    assert warning.profiles[0].model_profile == "accuracy"
    assert warning.profiles[0].can_start is True
    assert warning.profiles[0].state == "warning"
    assert warning.profiles[1].can_start is False
    assert "SPEED_PROFILE_NOT_ACTIVE" in warning.profiles[1].issue_codes
    assert blocked.state == "blocked"
    assert blocked.profiles[0].can_start is False
    assert "WORKER_DATABASE_UNAVAILABLE" in blocked.issue_codes
    assert "FFMPEG_UNAVAILABLE" in blocked.issue_codes


def test_readiness_api_is_authenticated_and_uses_injected_snapshot(tmp_path) -> None:
    snapshot = _snapshot(tmp_path)
    with JobStore(tmp_path / "worker.sqlite3") as store:
        client = TestClient(
            create_app(
                store=store,
                credential_verifier=_verifier(),
                readiness_provider=lambda: snapshot,
            )
        )
        unauthorized = client.get("/v1/readiness")
        response = client.get("/v1/readiness", headers=AUTHORIZATION)

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == json.loads(json.dumps(snapshot.to_dict()))
