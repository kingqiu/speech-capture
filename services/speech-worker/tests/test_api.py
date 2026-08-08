"""Minimal public Worker API and OpenAPI contract tests."""

from fastapi.testclient import TestClient

from speech_capture_worker import __version__
from speech_capture_worker.api import app
from speech_capture_worker.artifact_generation import ARTIFACT_SCHEMA_VERSION
from speech_capture_worker.protocol_contract import PROTOCOL_VERSION


def test_health_and_capabilities_expose_no_private_runtime_data() -> None:
    client = TestClient(app)

    health = client.get("/v1/health")
    capabilities = client.get("/v1/capabilities")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "worker_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
    }
    assert capabilities.status_code == 200
    payload = capabilities.json()
    assert payload["protocol"] == {
        "minimum": PROTOCOL_VERSION,
        "maximum": PROTOCOL_VERSION,
    }
    assert payload["artifact_schema"] == {
        "minimum": ARTIFACT_SCHEMA_VERSION,
        "maximum": ARTIFACT_SCHEMA_VERSION,
    }
    serialized = capabilities.text.lower()
    assert "source_display_name" not in serialized
    assert "/users/" not in serialized
    assert "test-data-private" not in serialized
    assert ".wav" not in serialized
    assert "audio_path" not in serialized


def test_capability_negotiation_returns_compatible_and_incompatible_results() -> None:
    client = TestClient(app)
    compatible = client.post(
        "/v1/capabilities/negotiate",
        json={
            "protocol": {"minimum": "1.0.0", "maximum": "1.0.0"},
            "artifact_schema": {
                "minimum": ARTIFACT_SCHEMA_VERSION,
                "maximum": ARTIFACT_SCHEMA_VERSION,
            },
            "required_features": ["publication_leases"],
        },
    )
    incompatible = client.post(
        "/v1/capabilities/negotiate",
        json={
            "protocol": {"minimum": "2.0.0", "maximum": "2.0.0"},
            "artifact_schema": {
                "minimum": ARTIFACT_SCHEMA_VERSION,
                "maximum": ARTIFACT_SCHEMA_VERSION,
            },
        },
    )

    assert compatible.status_code == 200
    assert compatible.json()["compatible"] is True
    assert incompatible.status_code == 200
    assert incompatible.json() == {
        "compatible": False,
        "protocol_version": None,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "missing_features": [],
        "issues": ["protocol_version_incompatible"],
    }


def test_negotiation_reports_unknown_future_capability_as_missing() -> None:
    response = TestClient(app).post(
        "/v1/capabilities/negotiate",
        json={
            "protocol": {"minimum": PROTOCOL_VERSION, "maximum": PROTOCOL_VERSION},
            "artifact_schema": {
                "minimum": ARTIFACT_SCHEMA_VERSION,
                "maximum": ARTIFACT_SCHEMA_VERSION,
            },
            "required_features": ["future_capability"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "compatible": False,
        "protocol_version": PROTOCOL_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "missing_features": ["future_capability"],
        "issues": ["required_capability_unavailable"],
    }


def test_openapi_is_versioned_strict_and_has_stable_operation_ids() -> None:
    schema = app.openapi()

    assert schema["openapi"].startswith("3.1.")
    assert schema["info"]["version"] == PROTOCOL_VERSION
    assert set(schema["paths"]) == {
        "/v1/capabilities",
        "/v1/capabilities/negotiate",
        "/v1/device-credential-rotations/activate",
        "/v1/devices",
        "/v1/devices/{device_id}",
        "/v1/devices/{device_id}/credential-rotations",
        "/v1/diagnostics/summary",
        "/v1/health",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/artifacts",
        "/v1/jobs/{job_id}/artifacts/{artifact_name}",
        "/v1/jobs/{job_id}/cancel",
        "/v1/jobs/{job_id}/corrections",
        "/v1/jobs/{job_id}/events",
        "/v1/jobs/{job_id}/pause",
        "/v1/jobs/{job_id}/resume",
        "/v1/jobs/{job_id}/retry",
        "/v1/jobs/{job_id}/segment-review",
        "/v1/jobs/{job_id}/speaker-display-name",
        "/v1/jobs/{job_id}/snapshot",
        "/v1/pairing/confirm",
        "/v1/pairing/sessions",
        "/v1/readiness",
        "/v1/jobs/{job_id}/review-audio",
        "/v1/jobs/{job_id}/review-audio/content",
        "/v1/uploads",
        "/v1/uploads/{upload_id}",
        "/v1/uploads/{upload_id}/complete",
        "/v1/uploads/{upload_id}/parts/{part_number}",
    }
    assert schema["paths"]["/v1/health"]["get"]["operationId"] == "getHealth"
    assert (
        schema["paths"]["/v1/capabilities"]["get"]["operationId"]
        == "getCapabilities"
    )
    assert (
        schema["paths"]["/v1/capabilities/negotiate"]["post"]["operationId"]
        == "negotiateCapabilities"
    )
    assert schema["paths"]["/v1/pairing/confirm"]["post"]["operationId"] == "confirmPairing"
    public_paths = {
        "/v1/health",
        "/v1/capabilities",
        "/v1/capabilities/negotiate",
        "/v1/pairing/confirm",
    }
    private_operations = {
        operation["operationId"]
        for path, methods in schema["paths"].items()
        if path not in public_paths
        for operation in methods.values()
    }
    assert private_operations == {
        "completeUpload",
        "activateDeviceCredentialRotation",
        "cancelJob",
        "createJob",
        "createPairingSession",
        "createUpload",
        "downloadJobArtifact",
        "getJob",
        "getDiagnosticsSummary",
        "getJobSnapshot",
        "getJobUpdates",
        "getJobReviewAudio",
        "getUpload",
        "getWorkerReadiness",
        "listJobArtifacts",
        "listJobCorrections",
        "listJobs",
        "listDevices",
        "pauseJob",
        "prepareDeviceCredentialRotation",
        "putUploadPart",
        "resumeJob",
        "revokeDevice",
        "retryJob",
        "reviewJobTranscriptSegment",
        "renameJobSpeakerDisplayName",
        "streamJobReviewAudio",
    }
    for path, methods in schema["paths"].items():
        if path in public_paths:
            continue
        for operation in methods.values():
            assert operation["security"] == [{"BearerAuth": []}]
    for name, component in schema["components"]["schemas"].items():
        if name.endswith(("RequestSchema", "VersionRangeSchema", "Response", "Envelope")):
            assert component["additionalProperties"] is False
