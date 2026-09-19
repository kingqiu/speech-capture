from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from speech_capture_worker.api import create_app
from speech_capture_worker.api_auth import ApiCredential, ApiPrincipal, CredentialVerifier
from speech_capture_worker.client_release_store import ClientReleaseStore

TOKEN = "client-release-test-token-abcdefghijklmnopqrstuvwxyz"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def _verifier() -> CredentialVerifier:
    principal = ApiPrincipal(
        device_id="device_client_release_test",
        allowed_vault_ids=frozenset({"vault_primary"}),
    )
    return CredentialVerifier((ApiCredential.from_plaintext(TOKEN, principal),))


def _write_release(root: Path, version: str = "0.1.25") -> tuple[Path, bytes]:
    root.mkdir(parents=True)
    files = {
        "main.js": b"compiled-client-release",
        "manifest.json": json.dumps(
            {
                "id": "speech-capture",
                "version": version,
                "minAppVersion": "1.11.4",
                "isDesktopOnly": True,
            },
            separators=(",", ":"),
        ).encode(),
        "styles.css": b".speech-capture{display:block}",
    }
    archive_name = f"speech-capture-{version}-alpha.zip"
    archive_path = root / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(f"speech-capture/{name}", content)
    archive_bytes = archive_path.read_bytes()
    installer_name = f"install-speech-capture-{version}.zsh"
    installer_path = root / installer_name
    installer_path.write_bytes(b"#!/bin/zsh\nexit 0\n")
    manifest = {
        "schema_version": 1,
        "plugin": {
            "id": "speech-capture",
            "version": version,
            "min_app_version": "1.11.4",
            "desktop_only": True,
        },
        "archive": {
            "filename": archive_name,
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "size_bytes": len(archive_bytes),
            "entries": [f"speech-capture/{name}" for name in files],
        },
        "installer": {
            "filename": installer_name,
            "sha256": hashlib.sha256(installer_path.read_bytes()).hexdigest(),
            "size_bytes": installer_path.stat().st_size,
        },
        "files": {
            name: {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
            for name, content in files.items()
        },
    }
    manifest_path = root / f"speech-capture-{version}-release.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, archive_bytes


def _client(tmp_path: Path) -> tuple[TestClient, ClientReleaseStore, bytes]:
    releases = ClientReleaseStore(tmp_path / "worker" / "client-releases")
    manifest_path, archive_bytes = _write_release(tmp_path / "source")
    releases.import_release(manifest_path)
    return (
        TestClient(
            create_app(
                client_release_store=releases,
                credential_verifier=_verifier(),
            )
        ),
        releases,
        archive_bytes,
    )


def test_release_metadata_requires_authenticated_paired_device(tmp_path: Path) -> None:
    client, _releases, _archive = _client(tmp_path)
    path = "/v1/client-releases/speech-capture/latest"

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401

    response = client.get(path, headers=AUTHORIZATION)
    assert response.status_code == 200
    payload = response.json()
    assert payload["plugin_id"] == "speech-capture"
    assert payload["version"] == "0.1.25"
    assert payload["archive"]["filename"] == "speech-capture-0.1.25-alpha.zip"
    serialized = response.text.lower()
    assert "/users/" not in serialized
    assert "client-releases" not in serialized
    assert "installer" not in serialized


def test_release_archive_is_verified_and_downloaded_with_integrity_headers(
    tmp_path: Path,
) -> None:
    client, _releases, archive_bytes = _client(tmp_path)
    response = client.get(
        "/v1/client-releases/speech-capture/0.1.25/archive",
        headers=AUTHORIZATION,
    )

    digest = hashlib.sha256(archive_bytes).hexdigest()
    assert response.status_code == 200
    assert response.content == archive_bytes
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-content-sha256"] == digest
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["cache-control"] == "private, immutable"

    unchanged = client.get(
        "/v1/client-releases/speech-capture/0.1.25/archive",
        headers={**AUTHORIZATION, "If-None-Match": f'"{digest}"'},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == f'"{digest}"'


def test_missing_store_release_and_tampered_release_fail_closed(tmp_path: Path) -> None:
    no_store = TestClient(create_app(credential_verifier=_verifier()))
    unavailable = no_store.get(
        "/v1/client-releases/speech-capture/latest",
        headers=AUTHORIZATION,
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "CLIENT_RELEASE_STORE_NOT_CONFIGURED"

    empty = ClientReleaseStore(tmp_path / "empty")
    empty_client = TestClient(
        create_app(client_release_store=empty, credential_verifier=_verifier())
    )
    missing = empty_client.get(
        "/v1/client-releases/speech-capture/latest",
        headers=AUTHORIZATION,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CLIENT_RELEASE_NOT_FOUND"

    client, releases, _archive = _client(tmp_path / "tampered")
    release = releases.get_release("0.1.25")
    release.archive_path.write_bytes(release.archive_path.read_bytes() + b"tampered")
    rejected = client.get(
        "/v1/client-releases/speech-capture/0.1.25/archive",
        headers=AUTHORIZATION,
    )
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "CLIENT_RELEASE_UNAVAILABLE"
    assert str(release.archive_path) not in rejected.text


def test_release_api_has_no_remote_write_operation() -> None:
    schema = create_app().openapi()
    release_paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if path.startswith("/v1/client-releases/")
    }

    assert release_paths
    assert all(set(methods) == {"get"} for methods in release_paths.values())
    assert all(
        operation["security"] == [{"BearerAuth": []}]
        for methods in release_paths.values()
        for operation in methods.values()
    )
