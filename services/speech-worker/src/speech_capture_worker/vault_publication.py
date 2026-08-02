"""Lease-backed atomic publication of verified Worker artifacts into a Vault."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from speech_capture_worker.artifact_generation import (
    ARTIFACT_CHECKPOINT_KEY,
    ARTIFACT_FILES,
    ARTIFACT_MANIFEST,
    ARTIFACT_STAGE,
    SPEECH_RECORD,
)
from speech_capture_worker.domain import JobRecord, JobState
from speech_capture_worker.errors import (
    InvalidJobRequest,
    PublicationConflict,
    PublicationVerificationFailed,
    WorkerCoreError,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.publication_domain import (
    DEFAULT_PUBLICATION_LEASE_SECONDS,
    PublicationLeaseRecord,
    PublicationReceiptRecord,
    validate_lease_seconds,
    validate_publisher_id,
    validate_vault_relative_path,
)

DEFAULT_VAULT_OUTPUT_ROOT = "Work/Speech Notes"
PUBLISHED_PACKAGE_FILES = (*ARTIFACT_FILES, ARTIFACT_MANIFEST)


class VaultPublicationOutcome(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"


@dataclass(frozen=True)
class VaultPublicationResult:
    outcome: VaultPublicationOutcome
    job: JobRecord
    receipt: PublicationReceiptRecord
    target_relative_path: str
    manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "job": self.job.to_dict(),
            "receipt": self.receipt.to_dict(),
            "target_relative_path": self.target_relative_path,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class VerifiedArtifactPackage:
    package_dir: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    speech_id: str
    title: str
    recording_date: str | None


class VaultPublisher:
    """Publish one complete package without exposing half-written Vault state."""

    def __init__(
        self,
        store: JobStore,
        *,
        vault_root: Path,
        publisher_id: str,
        output_root: str = DEFAULT_VAULT_OUTPUT_ROOT,
        lease_seconds: int = DEFAULT_PUBLICATION_LEASE_SECONDS,
    ) -> None:
        if vault_root.is_symlink() or not vault_root.is_dir():
            raise InvalidJobRequest("vault_root must be an existing non-symlink directory.")
        self.store = store
        self.vault_root = vault_root.resolve()
        self.publisher_id = validate_publisher_id(publisher_id)
        self.output_root = validate_vault_relative_path(output_root)
        validate_lease_seconds(lease_seconds)
        self.lease_seconds = lease_seconds
        vault_stat = self.vault_root.stat()
        self._vault_identity = (vault_stat.st_dev, vault_stat.st_ino)

    def publish(self, job_id: str, *, expected_revision: int) -> VaultPublicationResult:
        package = self._verify_worker_package(job_id)
        target_relative_path = _target_relative_path(
            output_root=self.output_root,
            speech_id=package.speech_id,
            title=package.title,
            recording_date=package.recording_date,
        )
        receipt = self.store.get_publication_receipt(job_id)
        current = self.store.get_job(job_id)
        if receipt is not None:
            if (
                current.state is not JobState.PUBLISHED
                or receipt.target_relative_path != target_relative_path
                or receipt.manifest_sha256 != package.manifest_sha256
            ):
                raise PublicationConflict(
                    "The existing publication acknowledgement does not match this package."
                )
            target = self._resolve_target(receipt.target_relative_path)
            self._verify_published_directory(
                target,
                manifest=package.manifest,
                manifest_sha256=package.manifest_sha256,
            )
            return VaultPublicationResult(
                outcome=VaultPublicationOutcome.ALREADY_PUBLISHED,
                job=current,
                receipt=receipt,
                target_relative_path=target_relative_path,
                manifest_sha256=package.manifest_sha256,
            )

        target = self._resolve_target(target_relative_path)
        lease, _, _ = self.store.claim_publication(
            job_id,
            publisher_id=self.publisher_id,
            target_relative_path=target_relative_path,
            manifest_sha256=package.manifest_sha256,
            expected_revision=expected_revision,
            lease_seconds=self.lease_seconds,
        )
        try:
            if target.exists() or target.is_symlink():
                self._verify_published_directory(
                    target,
                    manifest=package.manifest,
                    manifest_sha256=package.manifest_sha256,
                )
            else:
                self._write_atomic_package(
                    package,
                    target=target,
                    lease=lease,
                )
            receipt, published, _ = self.store.acknowledge_publication(
                job_id,
                lease_id=lease.lease_id,
                publisher_id=self.publisher_id,
                manifest_sha256=package.manifest_sha256,
            )
            return VaultPublicationResult(
                outcome=VaultPublicationOutcome.PUBLISHED,
                job=published,
                receipt=receipt,
                target_relative_path=target_relative_path,
                manifest_sha256=package.manifest_sha256,
            )
        except OSError as exc:
            self._release_after_failure(job_id, lease=lease)
            raise PublicationVerificationFailed(
                "The Vault package could not be written durably."
            ) from exc
        except Exception:
            self._release_after_failure(job_id, lease=lease)
            raise

    def _verify_worker_package(self, job_id: str) -> VerifiedArtifactPackage:
        checkpoint = next(
            (
                item
                for item in self.store.list_checkpoints(job_id, stage=ARTIFACT_STAGE)
                if item.checkpoint_key == ARTIFACT_CHECKPOINT_KEY
            ),
            None,
        )
        if checkpoint is None:
            raise PublicationVerificationFailed(
                "Publication requires a generated artifact checkpoint."
            )
        package_dir = self.store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE)
        if package_dir.is_symlink() or not package_dir.is_dir():
            raise PublicationVerificationFailed("The Worker artifact package is unavailable.")
        manifest_path = package_dir / ARTIFACT_MANIFEST
        manifest_bytes = _read_regular_file(manifest_path)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha256 != checkpoint.payload.get("manifest_sha256"):
            raise PublicationVerificationFailed(
                "The Worker artifact manifest failed checkpoint verification."
            )
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationVerificationFailed(
                "The Worker artifact manifest is invalid JSON."
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("job_id") != job_id
            or not isinstance(manifest.get("speech_id"), str)
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != set(ARTIFACT_FILES)
            or manifest["files"] != checkpoint.payload.get("files")
        ):
            raise PublicationVerificationFailed("The Worker artifact manifest is incompatible.")
        for name in ARTIFACT_FILES:
            content = _read_regular_file(package_dir / name)
            if hashlib.sha256(content).hexdigest() != manifest["files"].get(name):
                raise PublicationVerificationFailed(
                    f"The Worker artifact {name} failed checksum verification."
                )
        try:
            speech_record = json.loads(_read_regular_file(package_dir / SPEECH_RECORD))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationVerificationFailed("speech-record.json is invalid.") from exc
        if (
            not isinstance(speech_record, dict)
            or speech_record.get("job_id") != job_id
            or speech_record.get("speech_id") != manifest["speech_id"]
        ):
            raise PublicationVerificationFailed("speech-record.json does not match the manifest.")
        title = _package_title(speech_record)
        recording_date = _recording_date(speech_record)
        return VerifiedArtifactPackage(
            package_dir=package_dir,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            speech_id=manifest["speech_id"],
            title=title,
            recording_date=recording_date,
        )

    def _write_atomic_package(
        self,
        package: VerifiedArtifactPackage,
        *,
        target: Path,
        lease: PublicationLeaseRecord,
    ) -> None:
        self._ensure_vault_identity()
        self._ensure_safe_parent(target.parent)
        temporary = target.with_name(f".{target.name}.{lease.lease_id}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise PublicationConflict("The publication temporary directory already exists.")
        temporary.mkdir(mode=0o700)
        try:
            for name in PUBLISHED_PACKAGE_FILES:
                _write_new_private_file(
                    temporary / name,
                    _read_regular_file(package.package_dir / name),
                )
            _fsync_directory(temporary)
            self._verify_published_directory(
                temporary,
                manifest=package.manifest,
                manifest_sha256=package.manifest_sha256,
            )
            self.store.renew_publication_lease(
                lease.job_id,
                lease_id=lease.lease_id,
                publisher_id=self.publisher_id,
                lease_seconds=self.lease_seconds,
            )
            self._ensure_vault_identity()
            if target.exists() or target.is_symlink():
                raise PublicationConflict(
                    "The Vault target appeared while publication was in progress."
                )
            os.rename(temporary, target)
            _fsync_directory(target.parent)
            self._verify_published_directory(
                target,
                manifest=package.manifest,
                manifest_sha256=package.manifest_sha256,
            )
        finally:
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)

    def _verify_published_directory(
        self,
        directory: Path,
        *,
        manifest: dict[str, Any],
        manifest_sha256: str,
    ) -> None:
        self._ensure_vault_identity()
        if directory.is_symlink() or not directory.is_dir():
            raise PublicationConflict("The Vault target is not a safe package directory.")
        try:
            names = {item.name for item in directory.iterdir()}
        except OSError as exc:
            raise PublicationVerificationFailed(
                "The published package could not be inspected."
            ) from exc
        if names != set(PUBLISHED_PACKAGE_FILES):
            raise PublicationConflict(
                "The Vault target contains user changes or unexpected files."
            )
        manifest_content = _read_regular_file(directory / ARTIFACT_MANIFEST)
        if hashlib.sha256(manifest_content).hexdigest() != manifest_sha256:
            raise PublicationConflict("The Vault target manifest does not match this package.")
        for name in ARTIFACT_FILES:
            content = _read_regular_file(directory / name)
            if hashlib.sha256(content).hexdigest() != manifest["files"].get(name):
                raise PublicationConflict(
                    f"The Vault target {name} contains user changes or a sync conflict."
                )

    def _resolve_target(self, relative_path: str) -> Path:
        validate_vault_relative_path(relative_path)
        target = self.vault_root.joinpath(*PurePosixPath(relative_path).parts)
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(self.vault_root):
            raise PublicationConflict("The Vault target resolves outside the configured Vault.")
        return target

    def _ensure_safe_parent(self, parent: Path) -> None:
        relative = parent.relative_to(self.vault_root)
        current = self.vault_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise PublicationConflict("The Vault output path contains a symbolic link.")
            if current.exists() and not current.is_dir():
                raise PublicationConflict("The Vault output path contains a non-directory.")
            current.mkdir(exist_ok=True, mode=0o700)
            if current.stat().st_dev != self._vault_identity[0]:
                raise PublicationConflict("The Vault output path changed mount devices.")
        if parent.resolve() != parent or not parent.resolve().is_relative_to(self.vault_root):
            raise PublicationConflict("The Vault output path escaped its configured root.")

    def _ensure_vault_identity(self) -> None:
        if self.vault_root.is_symlink() or not self.vault_root.is_dir():
            raise PublicationConflict("The configured Vault root is unavailable.")
        stat = self.vault_root.stat()
        if (stat.st_dev, stat.st_ino) != self._vault_identity:
            raise PublicationConflict("The configured Vault mount changed during publication.")

    def _release_after_failure(
        self,
        job_id: str,
        *,
        lease: PublicationLeaseRecord,
    ) -> None:
        try:
            current = self.store.get_job(job_id)
            if current.state is JobState.PUBLISHING:
                self.store.release_publication_lease(
                    job_id,
                    lease_id=lease.lease_id,
                    publisher_id=self.publisher_id,
                    reason_code="publication_failed",
                )
        except WorkerCoreError:
            pass


def _target_relative_path(
    *,
    output_root: str,
    speech_id: str,
    title: str,
    recording_date: str | None,
) -> str:
    slug = _safe_slug(title)
    directory_name = f"{recording_date + '-' if recording_date else ''}{slug}--{speech_id}"
    if recording_date:
        year, month, _ = recording_date.split("-")
        path = PurePosixPath(output_root, year, month, directory_name)
    else:
        path = PurePosixPath(output_root, "Undated", directory_name)
    return validate_vault_relative_path(path.as_posix())


def _safe_slug(value: str) -> str:
    cleaned = re.sub(r"[\\/:\x00]+", "-", value.strip())
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip(" .-")
    return (cleaned[:80].rstrip(" .-") or "untitled")


def _package_title(record: dict[str, Any]) -> str:
    document = record.get("document")
    if isinstance(document, dict) and isinstance(document.get("title"), str):
        title = document["title"].strip()
        if title:
            return title
    source = record.get("source")
    if isinstance(source, dict) and isinstance(source.get("display_name"), str):
        return Path(source["display_name"]).stem or "untitled"
    return "untitled"


def _recording_date(record: dict[str, Any]) -> str | None:
    dates = record.get("dates")
    value = dates.get("recording_date") if isinstance(dates, dict) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise PublicationVerificationFailed("The recording date is invalid.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PublicationVerificationFailed("The recording date is invalid.") from exc
    if parsed.isoformat() != value:
        raise PublicationVerificationFailed("The recording date is invalid.")
    return value


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicationVerificationFailed("A publication file could not be read.") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PublicationVerificationFailed(
                "A publication file is missing or unsafe."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    except OSError as exc:
        raise PublicationVerificationFailed("A publication file could not be read.") from exc
    finally:
        os.close(descriptor)


def _write_new_private_file(path: Path, content: bytes) -> None:
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(file_descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
