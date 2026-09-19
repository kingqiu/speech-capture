"""Content-free integrity validation for locally installed model artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.model_budget import (
    ModelCatalogItem,
    ModelProfileName,
    model_catalog_for_profile,
)

REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_BLOB_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HF_REQUIRED_FILES = (
    "config.json",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
)
APPROVED_MODEL_REVISIONS = {
    "Qwen/Qwen3-ASR-1.7B": "7278e1e70fe206f11671096ffdd38061171dd6e5",
    "Qwen/Qwen3-ASR-0.6B": "5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
    "Qwen/Qwen3-ForcedAligner-0.6B": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7",
    "qwen3:14b": "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8",
    "qwen3:8b": "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41",
}


@dataclass(frozen=True)
class ModelFileValidation:
    key: str
    model_id: str
    provider: str
    state: str
    revision: str | None
    checked_file_count: int
    checked_bytes: int
    full_hash_verified: bool
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class ModelValidationReport:
    profile: ModelProfileName
    valid: bool
    models: tuple[ModelFileValidation, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_model_profile(
    profile: ModelProfileName,
    *,
    huggingface_cache: Path | None = None,
    ollama_models_dir: Path | None = None,
    home: Path | None = None,
    approved_revisions: dict[str, str] | None = None,
) -> ModelValidationReport:
    if profile not in {"accuracy", "speed", "all"}:
        raise InvalidJobRequest("The model validation profile is not supported.")
    user_home = (home or Path.home()).expanduser().resolve()
    hf_cache = (
        huggingface_cache.expanduser().resolve()
        if huggingface_cache is not None
        else _default_huggingface_cache(user_home)
    )
    ollama_dir = (
        ollama_models_dir.expanduser().resolve()
        if ollama_models_dir is not None
        else _default_ollama_models_dir(user_home)
    )
    approvals = (
        approved_revisions
        if approved_revisions is not None
        else APPROVED_MODEL_REVISIONS
    )
    models = tuple(
        _validate_catalog_item(
            item,
            hf_cache=hf_cache,
            ollama_dir=ollama_dir,
            approved_revision=approvals.get(item.model_id),
        )
        for item in model_catalog_for_profile(profile)
    )
    return ModelValidationReport(
        profile=profile,
        valid=all(model.state == "valid" for model in models),
        models=models,
    )


def _validate_catalog_item(
    item: ModelCatalogItem,
    *,
    hf_cache: Path,
    ollama_dir: Path,
    approved_revision: str | None,
) -> ModelFileValidation:
    if approved_revision is None:
        return _result(item, "invalid", issues=("MODEL_REVISION_UNAPPROVED",))
    if item.provider == "mlx":
        return _validate_huggingface_model(item, hf_cache, approved_revision)
    if item.provider == "ollama":
        return _validate_ollama_model(item, ollama_dir, approved_revision)
    return _result(item, "invalid", issues=("MODEL_PROVIDER_UNSUPPORTED",))


def _validate_huggingface_model(
    item: ModelCatalogItem,
    cache: Path,
    approved_revision: str,
) -> ModelFileValidation:
    repository = cache / f"models--{item.model_id.replace('/', '--')}"
    ref_path = repository / "refs" / "main"
    try:
        if ref_path.is_symlink():
            return _result(item, "invalid", issues=("MODEL_REF_INVALID",))
        revision = ref_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return _result(item, "missing", issues=("MODEL_NOT_INSTALLED",))
    if REVISION_PATTERN.fullmatch(revision) is None:
        return _result(item, "invalid", issues=("MODEL_REF_INVALID",))
    if revision != approved_revision:
        return _result(
            item,
            "invalid",
            revision=revision,
            issues=("MODEL_REVISION_UNAPPROVED",),
        )

    snapshot = repository / "snapshots" / revision
    tree_path = repository / "trees" / f"{revision}.json"
    try:
        if tree_path.is_symlink():
            return _result(
                item,
                "invalid",
                revision=revision,
                issues=("MODEL_METADATA_INVALID",),
            )
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _result(
            item,
            "invalid",
            revision=revision,
            issues=("MODEL_METADATA_MISSING",),
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _result(
            item,
            "invalid",
            revision=revision,
            issues=("MODEL_METADATA_INVALID",),
        )
    files = tree.get("files") if isinstance(tree, dict) else None
    if (
        not isinstance(tree, dict)
        or tree.get("format_version") != 1
        or not isinstance(files, dict)
    ):
        return _result(
            item,
            "invalid",
            revision=revision,
            issues=("MODEL_METADATA_INVALID",),
        )

    required = list(HF_REQUIRED_FILES)
    issues: list[str] = []
    index_name = "model.safetensors.index.json"
    if index_name in files and (snapshot / index_name).exists():
        required.append(index_name)
    else:
        required.append("model.safetensors")

    checked_files = 0
    checked_bytes = 0
    seen: set[str] = set()
    validated_names: set[str] = set()

    def validate_required(name: str) -> None:
        nonlocal checked_files, checked_bytes
        if name in seen:
            return
        seen.add(name)
        metadata = files.get(name)
        if not isinstance(metadata, dict):
            issues.append("MODEL_REQUIRED_FILE_MISSING")
            return
        file_result = _validate_huggingface_file(
            repository,
            snapshot,
            name,
            metadata,
        )
        if isinstance(file_result, str):
            issues.append(file_result)
            return
        checked_files += 1
        checked_bytes += file_result
        validated_names.add(name)

    for name in required:
        validate_required(name)

    if index_name in validated_names:
        index_payload = _read_json(snapshot / index_name)
        weight_map = (
            index_payload.get("weight_map") if isinstance(index_payload, dict) else None
        )
        if (
            not isinstance(weight_map, dict)
            or not weight_map
            or not all(isinstance(name, str) for name in weight_map.values())
        ):
            issues.append("MODEL_WEIGHTS_INDEX_INVALID")
        else:
            for shard_name in sorted(set(weight_map.values())):
                validate_required(shard_name)

    config = (
        _read_json(snapshot / "config.json")
        if "config.json" in validated_names
        else None
    )
    if not isinstance(config, dict) or config.get("model_type") != "qwen3_asr":
        issues.append("MODEL_CONFIG_INVALID")
    for name in validated_names:
        if name.endswith(".safetensors") and not _valid_safetensors(snapshot / name):
            issues.append("MODEL_SAFETENSORS_INVALID")

    issue_codes = tuple(sorted(set(issues)))
    return _result(
        item,
        "invalid" if issue_codes else "valid",
        revision=revision,
        checked_files=checked_files,
        checked_bytes=checked_bytes,
        full_hash_verified=not issue_codes,
        issues=issue_codes,
    )


def _validate_huggingface_file(
    repository: Path,
    snapshot: Path,
    name: str,
    metadata: dict[str, Any],
) -> int | str:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        return "MODEL_METADATA_INVALID"
    path = snapshot.joinpath(*relative.parts)
    expected_size = metadata.get("size")
    blob_id = metadata.get("blob_id")
    lfs_sha256 = metadata.get("lfs_sha256")
    lfs_size = metadata.get("lfs_size")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
        or not isinstance(blob_id, str)
        or GIT_BLOB_PATTERN.fullmatch(blob_id) is None
        or (
            lfs_sha256 is not None
            and (
                not isinstance(lfs_sha256, str)
                or SHA256_PATTERN.fullmatch(lfs_sha256) is None
                or lfs_size != expected_size
            )
        )
    ):
        return "MODEL_METADATA_INVALID"
    try:
        if not path.is_symlink():
            return "MODEL_FILE_LINK_INVALID"
        resolved = path.resolve(strict=True)
        blobs = (repository / "blobs").resolve(strict=True)
        if resolved.parent != blobs:
            return "MODEL_FILE_LINK_INVALID"
        expected_name = lfs_sha256 or blob_id
        if resolved.name != expected_name or not resolved.is_file():
            return "MODEL_FILE_LINK_INVALID"
        if resolved.stat().st_size != expected_size:
            return "MODEL_FILE_SIZE_MISMATCH"
        if lfs_sha256 is not None:
            actual_digest = _hash_file(resolved, "sha256")
            expected_digest = lfs_sha256
        else:
            actual_digest = _git_blob_hash(resolved, expected_size)
            expected_digest = blob_id
        if actual_digest != expected_digest:
            return "MODEL_FILE_HASH_MISMATCH"
    except OSError:
        return "MODEL_REQUIRED_FILE_MISSING"
    return expected_size


def _validate_ollama_model(
    item: ModelCatalogItem,
    models_dir: Path,
    approved_revision: str,
) -> ModelFileValidation:
    try:
        name, tag = item.model_id.split(":", 1)
    except ValueError:
        return _result(item, "invalid", issues=("OLLAMA_MODEL_ID_INVALID",))
    manifest_path = (
        models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag
    )
    try:
        if manifest_path.is_symlink():
            return _result(item, "invalid", issues=("OLLAMA_MANIFEST_INVALID",))
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except FileNotFoundError:
        return _result(item, "missing", issues=("MODEL_NOT_INSTALLED",))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return _result(item, "invalid", issues=("OLLAMA_MANIFEST_INVALID",))
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        return _result(item, "invalid", issues=("OLLAMA_MANIFEST_INVALID",))
    revision = hashlib.sha256(manifest_bytes).hexdigest()
    if revision != approved_revision:
        return _result(
            item,
            "invalid",
            revision=revision,
            issues=("MODEL_REVISION_UNAPPROVED",),
        )
    descriptors = [manifest.get("config")]
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        return _result(item, "invalid", issues=("OLLAMA_MANIFEST_INVALID",))
    descriptors.extend(layers)

    issues: list[str] = []
    checked_files = 0
    checked_bytes = 0
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            issues.append("OLLAMA_MANIFEST_INVALID")
            continue
        digest = descriptor.get("digest")
        expected_size = descriptor.get("size")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
        ):
            issues.append("OLLAMA_MANIFEST_INVALID")
            continue
        digest_hex = digest.removeprefix("sha256:")
        blob = models_dir / "blobs" / f"sha256-{digest_hex}"
        try:
            if blob.is_symlink() or not blob.is_file():
                issues.append("MODEL_REQUIRED_FILE_MISSING")
                continue
            if blob.stat().st_size != expected_size:
                issues.append("MODEL_FILE_SIZE_MISMATCH")
                continue
            if _hash_file(blob, "sha256") != digest_hex:
                issues.append("MODEL_FILE_HASH_MISMATCH")
                continue
        except OSError:
            issues.append("MODEL_REQUIRED_FILE_MISSING")
            continue
        checked_files += 1
        checked_bytes += expected_size

    issue_codes = tuple(sorted(set(issues)))
    return _result(
        item,
        "invalid" if issue_codes else "valid",
        revision=revision,
        checked_files=checked_files,
        checked_bytes=checked_bytes,
        full_hash_verified=not issue_codes,
        issues=issue_codes,
    )


def _result(
    item: ModelCatalogItem,
    state: str,
    *,
    revision: str | None = None,
    checked_files: int = 0,
    checked_bytes: int = 0,
    full_hash_verified: bool = False,
    issues: tuple[str, ...] = (),
) -> ModelFileValidation:
    return ModelFileValidation(
        key=item.key,
        model_id=item.model_id,
        provider=item.provider,
        state=state,
        revision=revision,
        checked_file_count=checked_files,
        checked_bytes=checked_bytes,
        full_hash_verified=full_hash_verified,
        issue_codes=issues,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _valid_safetensors(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            prefix = stream.read(8)
            if len(prefix) != 8:
                return False
            header_size = int.from_bytes(prefix, "little")
            if header_size <= 1 or header_size > size - 8:
                return False
            header = json.loads(stream.read(header_size))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(header, dict) and any(key != "__metadata__" for key in header)


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _file_identity(before) != _file_identity(after):
        raise OSError("Model file changed during validation.")
    return digest.hexdigest()


def _git_blob_hash(path: Path, size: int) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _file_identity(before) != _file_identity(after):
        raise OSError("Model file changed during validation.")
    return digest.hexdigest()


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _default_huggingface_cache(home: Path) -> Path:
    override = os.environ.get("HUGGINGFACE_HUB_CACHE")
    return Path(override).expanduser().resolve() if override else home / ".cache/huggingface/hub"


def _default_ollama_models_dir(home: Path) -> Path:
    override = os.environ.get("OLLAMA_MODELS")
    return Path(override).expanduser().resolve() if override else home / ".ollama/models"
