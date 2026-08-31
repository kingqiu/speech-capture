"""Content-free atomic diagnostic bundle tests."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile

import pytest

from speech_capture_worker.diagnostic_bundle import build_diagnostic_bundle
from speech_capture_worker.errors import InvalidJobRequest


def test_bundle_has_fixed_content_free_entries_and_verified_manifest(tmp_path) -> None:
    output = tmp_path / "worker-diagnostics.zip"

    result = build_diagnostic_bundle(
        output,
        status=_payload({"schema_version": "1.0.0", "issue_codes": ()}),
        activation=_payload(
            {
                "schema_version": "1.0.0",
                "generation": 1,
                "active": {"profile": "accuracy"},
                "rollback": None,
            }
        ),
        validation=_payload(
            {
                "profile": "all",
                "valid": True,
                "models": ({"model_id": "qwen3:8b", "state": "valid"},),
            }
        ),
        private_markers=(str(tmp_path), "private-customer"),
    )

    assert result.created is True
    assert result.entry_count == 5
    assert result.bundle_bytes == output.stat().st_size
    assert result.bundle_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "environment.json",
            "manager-status.json",
            "model-activation.json",
            "model-validation.json",
            "manifest.json",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["entries"]:
            content = archive.read(entry["name"])
            assert entry["bytes"] == len(content)
            assert entry["sha256"] == hashlib.sha256(content).hexdigest()
            assert stat.S_IMODE(archive.getinfo(entry["name"]).external_attr >> 16) == 0o600
        serialized = b"".join(archive.read(name) for name in archive.namelist())
    assert str(tmp_path).encode() not in serialized
    assert b"private-customer" not in serialized


def test_bundle_refuses_to_overwrite_existing_output(tmp_path) -> None:
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep-me")

    with pytest.raises(InvalidJobRequest, match="already exists"):
        build_diagnostic_bundle(
            output,
            status=_payload({}),
            activation=_payload({}),
            validation=_payload({}),
        )

    assert output.read_bytes() == b"keep-me"


@pytest.mark.parametrize(
    "private_payload",
    [
        {"path": "/safe-looking"},
        {"safe": "/Users/private/customer.wav"},
        {"safe": "contains-private-customer-name"},
    ],
)
def test_bundle_fails_closed_when_payload_contains_private_data(
    tmp_path,
    private_payload,
) -> None:
    output = tmp_path / "blocked.zip"

    with pytest.raises(InvalidJobRequest):
        build_diagnostic_bundle(
            output,
            status=_payload(private_payload),
            activation=_payload({}),
            validation=_payload({}),
            private_markers=("private-customer",),
        )

    assert not output.exists()


def test_bundle_requires_a_new_absolute_zip_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(InvalidJobRequest, match="absolute .zip"):
        build_diagnostic_bundle(
            tmp_path / "not-a-zip.txt",
            status=_payload({}),
            activation=_payload({}),
            validation=_payload({}),
        )


class _payload:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value
