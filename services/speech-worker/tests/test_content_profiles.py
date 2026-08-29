"""Phase B1 tests for strict, runtime-disconnected content profile loading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from speech_capture_worker.content_profiles import (
    ProfileBundleError,
    ProfileBundleRegistry,
    compute_bundle_sha256,
    load_profile_bundle,
)


def _write_bundle(
    root: Path,
    *,
    profile_version: str = "2026-08-28.synthetic.1",
    prompt_text: str = "只提取有直接证据的合成会议事实。\n",
) -> Path:
    root.mkdir()
    payloads = {
        "extract.prompt.md": prompt_text.encode(),
        "synthesize.prompt.md": "根据公开合成证据生成结构化候选。\n".encode(),
        "document-policy.json": json.dumps(
            {
                "required_nonempty": ["title", "objective", "summary", "timeline_sections"],
                "allowed_empty": ["decisions", "actions", "risks", "open_questions"],
                "body_source": "topics",
                "field_limits": {"highlights": 8, "topics": 10, "speaker_summaries": 16},
            },
            ensure_ascii=False,
        ).encode(),
        "execution-policy.json": json.dumps(
            {
                "roles": {
                    "classification": "editor",
                    "extraction": "editor",
                    "synthesis": "primary",
                    "quality_edit": "editor",
                },
                "batch_target_tokens": 4800,
                "maximum_quality_passes": 1,
                "enabled_registered_repairs": [],
            }
        ).encode(),
        "validation-policy.json": json.dumps(
            {
                "registered_validators": [
                    "meeting.context.sufficient",
                    "meeting.decision.confirmed",
                    "meeting.action.evidence_complete",
                    "meeting.categories.nonduplicated",
                ],
                "thresholds": {
                    "minimum_context_facets": 2,
                    "single_context_minimum_characters": 80,
                },
            }
        ).encode(),
        "renderer.json": json.dumps(
            {
                "renderer_version": "1.0.0",
                "document_schema_version": "1.0.0",
                "sections": [
                    {"field": "objective", "heading": "会议目标", "when": "nonempty"},
                    {"field": "summary", "heading": "内容总结", "when": "always"},
                    {"field": "topics", "heading": "主要讨论与结论", "when": "nonempty"},
                ],
                "timeline_output": "separate_markdown",
                "evidence_output": "separate_markdown",
            },
            ensure_ascii=False,
        ).encode(),
        "fixtures/manifest.json": json.dumps(
            {
                "fixture_schema_version": "1.0.0",
                "fixtures": [
                    {
                        "id": "synthetic-meeting-baseline",
                        "description": "不含真实用户、公司或音频内容的公开合成基线",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode(),
    }
    for relative_path, content in payloads.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    file_hashes = {
        path: f"sha256:{hashlib.sha256(content).hexdigest()}"
        for path, content in payloads.items()
    }
    manifest = {
        "bundle_schema_version": "1.0.0",
        "profile_id": "speech-capture/meeting",
        "profile_version": profile_version,
        "content_type": "meeting",
        "document_schema": {"id": "speech-capture/structured-note", "version": "1.0.0"},
        "engine_compatibility": {"minimum": "0.1.0a0", "maximum_exclusive": "0.2.0"},
        "prompts": {
            "extraction": "extract.prompt.md",
            "synthesis": "synthesize.prompt.md",
            "coverage_repair": None,
            "quality_edit": None,
            "named_repairs": {},
        },
        "document_policy": "document-policy.json",
        "execution_policy": "execution-policy.json",
        "validation_policy": "validation-policy.json",
        "renderer": "renderer.json",
        "fixtures_manifest": "fixtures/manifest.json",
        "fallback_profile": {
            "profile_id": "speech-capture/meeting",
            "profile_version": "builtin-2026-08-27.1",
        },
        "files": file_hashes,
        "bundle_sha256": "sha256:" + "0" * 64,
    }
    manifest["bundle_sha256"] = compute_bundle_sha256(manifest)
    (root / "profile.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _read_manifest(root: Path) -> dict:
    return json.loads((root / "profile.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict) -> None:
    manifest["bundle_sha256"] = compute_bundle_sha256(manifest)
    (root / "profile.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_loads_complete_bundle_and_reads_prompts_as_inert_text(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")

    bundle = load_profile_bundle(root)

    assert bundle.profile_id == "speech-capture/meeting"
    assert bundle.content_type == "meeting"
    assert bundle.document_schema_version == "1.0.0"
    assert bundle.renderer_version == "1.0.0"
    assert bundle.read_prompt("extraction") == "只提取有直接证据的合成会议事实。\n"
    assert bundle.read_prompt("quality_edit") is None


def test_rejects_unknown_manifest_field(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")
    manifest = _read_manifest(root)
    manifest["remote_url"] = "https://example.invalid/profile"
    _write_manifest(root, manifest)

    with pytest.raises(ProfileBundleError, match="unknown=.*remote_url"):
        load_profile_bundle(root)


def test_rejects_unlisted_file_and_checksum_mismatch(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")
    (root / "unlisted.txt").write_text("not declared", encoding="utf-8")

    with pytest.raises(ProfileBundleError, match="unlisted"):
        load_profile_bundle(root)

    (root / "unlisted.txt").unlink()
    (root / "extract.prompt.md").write_text("changed after signing", encoding="utf-8")
    with pytest.raises(ProfileBundleError, match="Checksum mismatch"):
        load_profile_bundle(root)


def test_rejects_path_escape_and_unknown_registered_capability(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")
    manifest = _read_manifest(root)
    manifest["prompts"]["extraction"] = "../outside.prompt.md"
    _write_manifest(root, manifest)
    with pytest.raises(ProfileBundleError, match="escapes"):
        load_profile_bundle(root)

    root = _write_bundle(tmp_path / "meeting-profile-2")
    policy_path = root / "validation-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["registered_validators"].append("arbitrary.python.validator")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = _read_manifest(root)
    manifest["files"]["validation-policy.json"] = (
        f"sha256:{hashlib.sha256(policy_path.read_bytes()).hexdigest()}"
    )
    _write_manifest(root, manifest)
    with pytest.raises(ProfileBundleError, match="unknown validator"):
        load_profile_bundle(root)


def test_rejects_incompatible_worker_version(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")

    with pytest.raises(ProfileBundleError, match="incompatible"):
        load_profile_bundle(root, engine_version="0.2.0")


def test_registry_requires_exact_hash_and_preserves_last_known_good(tmp_path: Path) -> None:
    first = load_profile_bundle(_write_bundle(tmp_path / "first"))
    second = load_profile_bundle(
        _write_bundle(tmp_path / "second", prompt_text="第二份同版本但不同内容。\n")
    )
    registry = ProfileBundleRegistry()
    registry.register_validated(first)
    registry.mark_last_known_good(first.reference)

    assert registry.resolve(first.reference) is first
    assert registry.last_known_good("meeting") is first
    with pytest.raises(ProfileBundleError, match="different hashes"):
        registry.register_validated(second)
