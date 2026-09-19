"""Installed model identity, completeness, and full-hash validation tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from speech_capture_worker.asr_probe import ALIGNER_MODEL_ID, SPEED_MODEL_ID
from speech_capture_worker.model_validation import validate_model_profile

REVISION = "a" * 40


def test_speed_profile_validates_huggingface_and_ollama_full_hashes(tmp_path) -> None:
    hf_cache = tmp_path / "huggingface"
    ollama_dir = tmp_path / "ollama"
    _create_huggingface_model(hf_cache, SPEED_MODEL_ID)
    _create_huggingface_model(hf_cache, ALIGNER_MODEL_ID, sharded=True)
    _create_ollama_model(ollama_dir, "qwen3:8b")

    report = validate_model_profile(
        "speed",
        huggingface_cache=hf_cache,
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    assert report.valid is True
    assert [model.state for model in report.models] == ["valid", "valid", "valid"]
    assert all(model.full_hash_verified for model in report.models)
    assert all(model.checked_file_count > 0 for model in report.models)
    assert all(model.checked_bytes > 0 for model in report.models)


def test_same_size_huggingface_blob_corruption_is_detected(tmp_path) -> None:
    hf_cache = tmp_path / "huggingface"
    ollama_dir = tmp_path / "ollama"
    weight_blob = _create_huggingface_model(hf_cache, SPEED_MODEL_ID)
    _create_huggingface_model(hf_cache, ALIGNER_MODEL_ID)
    _create_ollama_model(ollama_dir, "qwen3:8b")
    payload = weight_blob.read_bytes()
    weight_blob.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])

    report = validate_model_profile(
        "speed",
        huggingface_cache=hf_cache,
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    model = report.models[0]
    assert report.valid is False
    assert model.state == "invalid"
    assert "MODEL_FILE_HASH_MISMATCH" in model.issue_codes
    assert model.full_hash_verified is False


def test_existing_cache_without_revision_metadata_is_not_treated_as_valid(
    tmp_path,
) -> None:
    repository = tmp_path / "huggingface" / "models--Qwen--Qwen3-ASR-0.6B"
    (repository / "refs").mkdir(parents=True)
    (repository / "refs" / "main").write_text(REVISION, encoding="utf-8")

    ollama_dir = tmp_path / "ollama"
    report = validate_model_profile(
        "speed",
        huggingface_cache=tmp_path / "huggingface",
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    assert report.valid is False
    assert report.models[0].issue_codes == ("MODEL_METADATA_MISSING",)


def test_unapproved_huggingface_revision_is_rejected_before_file_validation(
    tmp_path,
) -> None:
    hf_cache = tmp_path / "huggingface"
    ollama_dir = tmp_path / "ollama"
    repository = _repository(hf_cache, SPEED_MODEL_ID)
    _create_huggingface_model(hf_cache, SPEED_MODEL_ID)
    (repository / "refs" / "main").write_text("d" * 40, encoding="utf-8")

    report = validate_model_profile(
        "speed",
        huggingface_cache=hf_cache,
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    assert report.valid is False
    assert report.models[0].revision == "d" * 40
    assert report.models[0].issue_codes == ("MODEL_REVISION_UNAPPROVED",)


def test_huggingface_snapshot_link_cannot_escape_model_repository(tmp_path) -> None:
    hf_cache = tmp_path / "huggingface"
    repository = _repository(hf_cache, SPEED_MODEL_ID)
    _create_huggingface_model(hf_cache, SPEED_MODEL_ID)
    outside = tmp_path / "outside.json"
    outside.write_text('{"model_type":"qwen3_asr"}', encoding="utf-8")
    config_link = repository / "snapshots" / REVISION / "config.json"
    config_link.unlink()
    config_link.symlink_to(outside)

    ollama_dir = tmp_path / "ollama"
    report = validate_model_profile(
        "speed",
        huggingface_cache=hf_cache,
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    assert report.valid is False
    assert "MODEL_FILE_LINK_INVALID" in report.models[0].issue_codes


def test_same_size_ollama_blob_corruption_is_detected(tmp_path) -> None:
    hf_cache = tmp_path / "huggingface"
    ollama_dir = tmp_path / "ollama"
    _create_huggingface_model(hf_cache, SPEED_MODEL_ID)
    _create_huggingface_model(hf_cache, ALIGNER_MODEL_ID)
    blob = _create_ollama_model(ollama_dir, "qwen3:8b")
    payload = blob.read_bytes()
    blob.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))

    report = validate_model_profile(
        "speed",
        huggingface_cache=hf_cache,
        ollama_models_dir=ollama_dir,
        approved_revisions=_approved_revisions(ollama_dir),
    )

    assert report.valid is False
    assert report.models[2].issue_codes == ("MODEL_FILE_HASH_MISMATCH",)


def _create_huggingface_model(
    cache: Path,
    model_id: str,
    *,
    sharded: bool = False,
) -> Path:
    repository = _repository(cache, model_id)
    snapshot = repository / "snapshots" / REVISION
    blobs = repository / "blobs"
    (repository / "refs").mkdir(parents=True)
    snapshot.mkdir(parents=True)
    blobs.mkdir(parents=True)
    (repository / "refs" / "main").write_text(REVISION, encoding="utf-8")
    files: dict[str, dict[str, object]] = {}

    small_files = {
        "config.json": json.dumps({"model_type": "qwen3_asr"}).encode(),
        "preprocessor_config.json": b"{}",
        "tokenizer_config.json": b"{}",
        "vocab.json": b"{}",
        "merges.txt": b"a b\n",
    }
    for name, content in small_files.items():
        _add_git_blob(snapshot, blobs, files, name, content)

    weight_content = _safetensors_payload()
    if sharded:
        shard_names = (
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
        )
        index = {
            "weight_map": {
                "first": shard_names[0],
                "second": shard_names[1],
            }
        }
        _add_git_blob(
            snapshot,
            blobs,
            files,
            "model.safetensors.index.json",
            json.dumps(index).encode(),
        )
        first_blob = None
        for name in shard_names:
            blob = _add_lfs_blob(snapshot, blobs, files, name, weight_content)
            first_blob = first_blob or blob
        assert first_blob is not None
        weight_blob = first_blob
    else:
        weight_blob = _add_lfs_blob(
            snapshot,
            blobs,
            files,
            "model.safetensors",
            weight_content,
        )
    trees = repository / "trees"
    trees.mkdir()
    (trees / f"{REVISION}.json").write_text(
        json.dumps({"format_version": 1, "files": files}),
        encoding="utf-8",
    )
    return weight_blob


def _create_ollama_model(models_dir: Path, model_id: str) -> Path:
    name, tag = model_id.split(":", 1)
    blobs = models_dir / "blobs"
    blobs.mkdir(parents=True)
    descriptors = []
    first_blob = None
    for content in (b"config", b"weights"):
        digest = hashlib.sha256(content).hexdigest()
        blob = blobs / f"sha256-{digest}"
        blob.write_bytes(content)
        first_blob = first_blob or blob
        descriptors.append({"digest": f"sha256:{digest}", "size": len(content)})
    manifest = {
        "schemaVersion": 2,
        "config": descriptors[0],
        "layers": descriptors[1:],
    }
    manifest_path = (
        models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert first_blob is not None
    return first_blob


def _add_git_blob(snapshot, blobs, files, name, content) -> Path:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(content)}\0".encode())
    digest.update(content)
    blob_id = digest.hexdigest()
    blob = blobs / blob_id
    blob.write_bytes(content)
    (snapshot / name).symlink_to(Path("../..") / "blobs" / blob_id)
    files[name] = {"size": len(content), "blob_id": blob_id}
    return blob


def _add_lfs_blob(snapshot, blobs, files, name, content) -> Path:
    digest = hashlib.sha256(content).hexdigest()
    blob = blobs / digest
    blob.write_bytes(content)
    (snapshot / name).symlink_to(Path("../..") / "blobs" / digest)
    files[name] = {
        "size": len(content),
        "blob_id": "b" * 40,
        "lfs_sha256": digest,
        "lfs_size": len(content),
    }
    return blob


def _safetensors_payload() -> bytes:
    header = json.dumps(
        {"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}},
        separators=(",", ":"),
    ).encode()
    return len(header).to_bytes(8, "little") + header + b"\0\0\0\0"


def _repository(cache: Path, model_id: str) -> Path:
    return cache / f"models--{model_id.replace('/', '--')}"


def _approved_revisions(ollama_dir: Path) -> dict[str, str]:
    manifest = (
        ollama_dir
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / "qwen3"
        / "8b"
    )
    ollama_revision = (
        hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.is_file()
        else "c" * 64
    )
    return {
        SPEED_MODEL_ID: REVISION,
        ALIGNER_MODEL_ID: REVISION,
        "qwen3:8b": ollama_revision,
    }
