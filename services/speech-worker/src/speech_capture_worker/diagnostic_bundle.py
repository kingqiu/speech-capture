"""Private-content-free diagnostic bundle creation for Worker Manager support."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.errors import DiagnosticBundleFailed, InvalidJobRequest
from speech_capture_worker.manager_status import ManagerStatusSnapshot
from speech_capture_worker.model_activation import ModelActivationState
from speech_capture_worker.model_validation import ModelValidationReport
from speech_capture_worker.protocol_contract import PROTOCOL_VERSION

DIAGNOSTIC_SCHEMA_VERSION = "1.0.0"
FIXED_ZIP_TIMESTAMP = (2026, 8, 2, 0, 0, 0)
ENTRY_NAMES = (
    "environment.json",
    "manager-status.json",
    "model-activation.json",
    "model-validation.json",
)
SENSITIVE_KEYS = frozenset(
    {
        "audio",
        "content",
        "context",
        "credential",
        "details",
        "file_name",
        "filename",
        "note",
        "path",
        "prompt",
        "source_display_name",
        "text",
        "token",
        "transcript",
        "vault_id",
    }
)


@dataclass(frozen=True)
class DiagnosticBundleResult:
    schema_version: str
    created: bool
    entry_count: int
    bundle_bytes: int
    bundle_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_diagnostic_bundle(
    output: Path,
    *,
    status: ManagerStatusSnapshot,
    activation: ModelActivationState,
    validation: ModelValidationReport,
    private_markers: tuple[str, ...] = (),
) -> DiagnosticBundleResult:
    target = _validated_new_output(output)
    payloads = {
        "environment.json": _environment_payload(),
        "manager-status.json": status.to_dict(),
        "model-activation.json": activation.to_dict(),
        "model-validation.json": validation.to_dict(),
    }
    for payload in payloads.values():
        _assert_content_free(payload, private_markers=private_markers)
    entries = {
        name: _canonical_json(payload)
        for name, payload in payloads.items()
    }
    manifest = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "entries": [
            {
                "name": name,
                "bytes": len(entries[name]),
                "sha256": hashlib.sha256(entries[name]).hexdigest(),
            }
            for name in ENTRY_NAMES
        ],
    }
    entries["manifest.json"] = _canonical_json(manifest)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    bundle_bytes = 0
    bundle_sha256 = ""
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(
                stream,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for name in (*ENTRY_NAMES, "manifest.json"):
                    info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, entries[name])
            stream.flush()
            os.fsync(stream.fileno())
        bundle_bytes = temporary.stat().st_size
        bundle_sha256 = _file_sha256(temporary)
        os.link(temporary, target, follow_symlinks=False)
        temporary.unlink()
        directory_descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError as exc:
        _remove_temporary(temporary)
        raise InvalidJobRequest(
            "The diagnostic bundle output already exists; choose a new file."
        ) from exc
    except OSError as exc:
        _remove_temporary(temporary)
        raise DiagnosticBundleFailed(
            "The diagnostic bundle could not be committed safely."
        ) from exc
    return DiagnosticBundleResult(
        schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        created=True,
        entry_count=len(entries),
        bundle_bytes=bundle_bytes,
        bundle_sha256=bundle_sha256,
    )


def _validated_new_output(output: Path) -> Path:
    expanded = output.expanduser()
    if not expanded.is_absolute() or expanded.suffix.lower() != ".zip":
        raise InvalidJobRequest(
            "The diagnostic bundle output must be a new absolute .zip file."
        )
    try:
        parent = expanded.parent.resolve(strict=True)
    except OSError as exc:
        raise InvalidJobRequest(
            "The diagnostic bundle output directory does not exist."
        ) from exc
    target = parent / expanded.name
    if target.exists() or target.is_symlink():
        raise InvalidJobRequest(
            "The diagnostic bundle output already exists; choose a new file."
        )
    return target


def _environment_payload() -> dict[str, Any]:
    packages = {}
    for name in ("speech-capture-worker", "fastapi", "mlx-qwen3-asr", "ollama"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "byte_order": sys.byteorder,
        "packages": packages,
    }


def _assert_content_free(
    value: Any,
    *,
    private_markers: tuple[str, ...],
    key: str | None = None,
) -> None:
    if key is not None and key.lower() in SENSITIVE_KEYS:
        raise InvalidJobRequest("The diagnostic payload contains a private field.")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise InvalidJobRequest("The diagnostic payload contains an invalid field.")
            _assert_content_free(
                child_value,
                private_markers=private_markers,
                key=child_key,
            )
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_content_free(child, private_markers=private_markers)
        return
    if isinstance(value, str):
        if value.startswith(("/Users/", "/private/", "/Volumes/")):
            raise InvalidJobRequest("The diagnostic payload contains a private path.")
        if any(marker and marker in value for marker in private_markers):
            raise InvalidJobRequest("The diagnostic payload contains private data.")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise InvalidJobRequest("The diagnostic payload contains an unsupported value.")


def _canonical_json(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()
