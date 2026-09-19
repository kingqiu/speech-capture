from __future__ import annotations

import hashlib
import inspect
import json
import zipfile
from pathlib import Path

import pytest

import speech_capture_worker.client_release_store as release_store_module
from speech_capture_worker.client_release_store import ClientReleaseError, ClientReleaseStore


def _write_release(root: Path, version: str = "0.1.25", marker: bytes = b"one") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "main.js": b"compiled-plugin-" + marker,
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
    installer_name = f"install-speech-capture-{version}.zsh"
    installer_path = root / installer_name
    installer_path.write_bytes(b"#!/bin/zsh\nexit 0\n" + marker)
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
            "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "size_bytes": archive_path.stat().st_size,
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
    return manifest_path


def test_import_is_verified_immutable_and_latest_is_semantic(tmp_path: Path) -> None:
    store = ClientReleaseStore(tmp_path / "worker" / "client-releases")
    first_manifest = _write_release(tmp_path / "source-a", "0.1.9")
    latest_manifest = _write_release(tmp_path / "source-b", "0.1.25")

    first = store.import_release(first_manifest)
    latest = store.import_release(latest_manifest)
    repeated = store.import_release(latest_manifest)

    assert first.version == "0.1.9"
    assert latest.version == "0.1.25"
    assert repeated == latest
    assert store.latest_release() == latest
    assert latest.archive_path.parent == (
        tmp_path / "worker" / "client-releases" / "speech-capture" / "0.1.25"
    )
    assert not (tmp_path / "worker" / "artifacts").exists()


def test_tampered_archive_is_rejected_without_creating_release(tmp_path: Path) -> None:
    manifest_path = _write_release(tmp_path / "source")
    archive_path = manifest_path.parent / "speech-capture-0.1.25-alpha.zip"
    archive_path.write_bytes(archive_path.read_bytes() + b"tampered")
    store = ClientReleaseStore(tmp_path / "releases")

    with pytest.raises(ClientReleaseError, match="size|checksum"):
        store.import_release(manifest_path)

    assert store.latest_release() is None


def test_unknown_private_field_and_path_traversal_are_rejected(tmp_path: Path) -> None:
    manifest_path = _write_release(tmp_path / "private-field")
    payload = json.loads(manifest_path.read_text("utf-8"))
    payload["vault_path"] = "/private/example"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    store = ClientReleaseStore(tmp_path / "releases")

    with pytest.raises(ClientReleaseError, match="missing or unknown"):
        store.import_release(manifest_path)

    traversal_manifest = _write_release(tmp_path / "traversal")
    payload = json.loads(traversal_manifest.read_text("utf-8"))
    payload["archive"]["filename"] = "../speech-capture-0.1.25-alpha.zip"
    traversal_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClientReleaseError, match="basename"):
        store.import_release(traversal_manifest)


def test_existing_version_cannot_be_replaced_with_different_bytes(tmp_path: Path) -> None:
    store = ClientReleaseStore(tmp_path / "releases")
    store.import_release(_write_release(tmp_path / "source-a", marker=b"first"))

    with pytest.raises(ClientReleaseError, match="immutable"):
        store.import_release(_write_release(tmp_path / "source-b", marker=b"second"))

    assert store.get_release("0.1.25").main_sha256 == hashlib.sha256(
        b"compiled-plugin-first"
    ).hexdigest()


def test_zip_with_extra_entry_or_identity_drift_is_rejected(tmp_path: Path) -> None:
    extra_manifest = _write_release(tmp_path / "extra")
    archive_path = extra_manifest.parent / "speech-capture-0.1.25-alpha.zip"
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("speech-capture/private-note.md", b"private")
    payload = json.loads(extra_manifest.read_text("utf-8"))
    payload["archive"]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    payload["archive"]["size_bytes"] = archive_path.stat().st_size
    extra_manifest.write_text(json.dumps(payload), encoding="utf-8")
    store = ClientReleaseStore(tmp_path / "releases")
    with pytest.raises(ClientReleaseError, match="fixed plugin whitelist"):
        store.import_release(extra_manifest)

    identity_manifest = _write_release(tmp_path / "identity")
    payload = json.loads(identity_manifest.read_text("utf-8"))
    payload["plugin"]["id"] = "different-plugin"
    identity_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ClientReleaseError, match="identity"):
        store.import_release(identity_manifest)


def test_release_store_has_no_job_or_publication_dependency() -> None:
    source = inspect.getsource(release_store_module)
    assert "job_store" not in source
    assert "vault_publication" not in source
    assert "artifact_generation" not in source
