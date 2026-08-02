"""Read-only, integrity-checked access to generated Worker artifact packages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from speech_capture_worker.artifact_generation import (
    ARTIFACT_CHECKPOINT_KEY,
    ARTIFACT_FILES,
    ARTIFACT_MANIFEST,
    ARTIFACT_STAGE,
)
from speech_capture_worker.errors import ArtifactNotFound, ArtifactVerificationFailed
from speech_capture_worker.job_store import JobStore

PUBLISHED_ARTIFACT_FILES = (*ARTIFACT_FILES, ARTIFACT_MANIFEST)


@dataclass(frozen=True)
class ArtifactDescriptor:
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    download_path: str


@dataclass(frozen=True)
class ArtifactPackage:
    job_id: str
    speech_id: str
    manifest_sha256: str
    directory: Path
    artifacts: tuple[ArtifactDescriptor, ...]

    def path_for(self, artifact_name: str) -> Path:
        if artifact_name not in PUBLISHED_ARTIFACT_FILES:
            raise ArtifactNotFound("The requested artifact does not exist.")
        path = self.directory / artifact_name
        if not path.is_file() or path.is_symlink():
            raise ArtifactVerificationFailed("The requested artifact failed verification.")
        return path


def load_artifact_package(store: JobStore, job_id: str) -> ArtifactPackage:
    checkpoints = store.list_checkpoints(job_id, stage=ARTIFACT_STAGE)
    checkpoint = next(
        (
            candidate
            for candidate in checkpoints
            if candidate.checkpoint_key == ARTIFACT_CHECKPOINT_KEY
        ),
        None,
    )
    if checkpoint is None:
        raise ArtifactNotFound("Artifacts are not available for this job.")
    payload = checkpoint.payload
    relative_path = payload.get("package_relative_path")
    if not isinstance(relative_path, str):
        raise ArtifactVerificationFailed("The artifact checkpoint is incomplete.")
    expected_path = store.jobs_directory / job_id / ARTIFACT_STAGE
    candidate_path = store.data_directory / relative_path
    if expected_path.is_symlink() or candidate_path.is_symlink():
        raise ArtifactVerificationFailed("The artifact package location is invalid.")
    expected_directory = expected_path.resolve()
    directory = candidate_path.resolve()
    if (
        directory != expected_directory
        or not directory.is_dir()
    ):
        raise ArtifactVerificationFailed("The artifact package location is invalid.")

    manifest_path = _verified_regular_file(directory / ARTIFACT_MANIFEST)
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ArtifactVerificationFailed("The artifact manifest is unreadable.") from exc
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != payload.get("manifest_sha256"):
        raise ArtifactVerificationFailed("The artifact manifest checksum does not match.")
    try:
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationFailed("The artifact manifest is unreadable.") from exc
    expected_hashes = payload.get("files")
    if (
        not isinstance(manifest, dict)
        or manifest.get("job_id") != job_id
        or not isinstance(manifest.get("speech_id"), str)
        or manifest.get("files") != expected_hashes
        or not isinstance(expected_hashes, dict)
        or set(expected_hashes) != set(ARTIFACT_FILES)
    ):
        raise ArtifactVerificationFailed("The artifact manifest is incompatible.")

    descriptors: list[ArtifactDescriptor] = []
    for name in PUBLISHED_ARTIFACT_FILES:
        path = _verified_regular_file(directory / name)
        digest = _file_sha256(path)
        expected_digest = (
            manifest_sha256 if name == ARTIFACT_MANIFEST else expected_hashes.get(name)
        )
        if digest != expected_digest:
            raise ArtifactVerificationFailed("An artifact checksum does not match.")
        descriptors.append(
            ArtifactDescriptor(
                name=name,
                media_type=("text/markdown" if name.endswith(".md") else "application/json"),
                size_bytes=path.stat().st_size,
                sha256=digest,
                download_path=f"/v1/jobs/{job_id}/artifacts/{name}",
            )
        )
    return ArtifactPackage(
        job_id=job_id,
        speech_id=str(manifest["speech_id"]),
        manifest_sha256=manifest_sha256,
        directory=directory,
        artifacts=tuple(descriptors),
    )


def _verified_regular_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ArtifactVerificationFailed("An artifact file is missing or unsafe.")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ArtifactVerificationFailed("An artifact file could not be read.") from exc
    return digest.hexdigest()
