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
from speech_capture_worker.meeting_field_repairs import (
    MAX_PACKET_CHARACTERS,
    MAX_PACKET_ESTIMATED_TOKENS,
    MAX_PACKET_SEGMENTS,
    MAX_REPAIR_CALLS,
    MAX_REPAIR_FIELD_CHARACTERS,
    MAX_REPAIR_OUTPUT_TOKENS,
    QUANTITATIVE_PROMOTION_REPAIR,
    SPEAKER_GROUNDING_REPAIR,
    TOPIC_DETAIL_REPAIR,
)

_BUNDLED_PROFILES = (
    Path(__file__).parents[1] / "src" / "speech_capture_worker" / "profile_bundles"
)
_FIELD_REPAIR_KEYS = (
    QUANTITATIVE_PROMOTION_REPAIR,
    SPEAKER_GROUNDING_REPAIR,
    TOPIC_DETAIL_REPAIR,
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


def _field_repair_policy() -> dict:
    repair_policy = {
        "model_role": "editor",
        "maximum_output_tokens": 1024,
        "maximum_field_characters": MAX_REPAIR_FIELD_CHARACTERS,
        "maximum_evidence_segments": MAX_PACKET_SEGMENTS,
        "maximum_evidence_characters": MAX_PACKET_CHARACTERS,
        "maximum_evidence_tokens": MAX_PACKET_ESTIMATED_TOKENS,
        "call_timeout_seconds": 120,
        "maximum_parser_retries": 1,
    }
    return {
        "maximum_calls": MAX_REPAIR_CALLS,
        "total_timeout_seconds": 180,
        "heartbeat_seconds": 10,
        "repairs": {key: dict(repair_policy) for key in _FIELD_REPAIR_KEYS},
    }


def _attach_field_repairs(root: Path) -> None:
    manifest = _read_manifest(root)
    for repair_key in _FIELD_REPAIR_KEYS:
        relative_path = f"{repair_key}.prompt.md"
        content = f"仅按证据修复 {repair_key}。\n".encode()
        (root / relative_path).write_bytes(content)
        manifest["prompts"]["named_repairs"][repair_key] = relative_path
        manifest["files"][relative_path] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    policy_path = root / "execution-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["field_repairs"] = _field_repair_policy()
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest["files"]["execution-policy.json"] = (
        f"sha256:{hashlib.sha256(policy_path.read_bytes()).hexdigest()}"
    )
    _write_manifest(root, manifest)


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


def test_existing_bundled_meeting_profile_remains_byte_and_hash_stable() -> None:
    root = _BUNDLED_PROFILES / "meeting" / "2026-08-29.1"

    bundle = load_profile_bundle(root)

    assert bundle.bundle_sha256 == (
        "sha256:903bff654e1c112209610f876b529abce34aa7ab279964b5927334bb32c59c6f"
    )
    assert hashlib.sha256((root / "profile.json").read_bytes()).hexdigest() == (
        "4b7d0a79c5035f11c0e6401f0c0717f249de7ea0aebf6b32124050053a376375"
    )
    assert "field_repairs" not in bundle.execution_policy


def test_loads_strict_extended_field_repair_contract(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")
    _attach_field_repairs(root)

    bundle = load_profile_bundle(root)

    field_repairs = bundle.execution_policy["field_repairs"]
    assert field_repairs["maximum_calls"] == 3
    assert set(field_repairs["repairs"]) == set(_FIELD_REPAIR_KEYS)
    for repair_key in _FIELD_REPAIR_KEYS:
        assert bundle.read_prompt("named_repairs", repair_name=repair_key).startswith(
            "仅按证据修复"
        )


def test_active_bundled_field_repair_profile_loads_with_exact_fallback() -> None:
    root = _BUNDLED_PROFILES / "meeting" / "2026-08-29.2"

    bundle = load_profile_bundle(root)

    assert bundle.profile_version == "2026-08-29.2"
    assert bundle.bundle_sha256 == (
        "sha256:640495ce7db7aa8c624be3ad3b37f1bc82d003b8edfd7cd18cee364c8243e3c0"
    )
    assert bundle.manifest["fallback_profile"]["profile_version"] == "2026-08-29.1"
    assert set(bundle.execution_policy["field_repairs"]["repairs"]) == set(
        _FIELD_REPAIR_KEYS
    )


@pytest.mark.parametrize(
    ("path", "unsafe_value"),
    [
        (("maximum_calls",), MAX_REPAIR_CALLS + 1),
        (("total_timeout_seconds",), 181),
        (("heartbeat_seconds",), 11),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_output_tokens"),
         MAX_REPAIR_OUTPUT_TOKENS + 1),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_field_characters"),
         MAX_REPAIR_FIELD_CHARACTERS + 1),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_evidence_segments"),
         MAX_PACKET_SEGMENTS + 1),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_evidence_characters"),
         MAX_PACKET_CHARACTERS + 1),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_evidence_tokens"),
         MAX_PACKET_ESTIMATED_TOKENS + 1),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "call_timeout_seconds"), 121),
        (("repairs", QUANTITATIVE_PROMOTION_REPAIR, "maximum_parser_retries"), 2),
    ],
)
def test_rejects_field_repair_policy_that_exceeds_worker_hard_limits(
    tmp_path: Path,
    path: tuple[str, ...],
    unsafe_value: int,
) -> None:
    root = _write_bundle(tmp_path / "meeting-profile")
    _attach_field_repairs(root)
    policy_path = root / "execution-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    target = policy["field_repairs"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = unsafe_value
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = _read_manifest(root)
    manifest["files"]["execution-policy.json"] = (
        f"sha256:{hashlib.sha256(policy_path.read_bytes()).hexdigest()}"
    )
    _write_manifest(root, manifest)

    with pytest.raises(ProfileBundleError, match="safe range"):
        load_profile_bundle(root)


def test_rejects_unknown_field_repair_and_prompt_policy_mismatch(tmp_path: Path) -> None:
    root = _write_bundle(tmp_path / "unknown-repair")
    _attach_field_repairs(root)
    policy_path = root / "execution-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["field_repairs"]["repairs"]["arbitrary_python_repair"] = policy[
        "field_repairs"
    ]["repairs"].pop(TOPIC_DETAIL_REPAIR)
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = _read_manifest(root)
    manifest["files"]["execution-policy.json"] = (
        f"sha256:{hashlib.sha256(policy_path.read_bytes()).hexdigest()}"
    )
    _write_manifest(root, manifest)
    with pytest.raises(ProfileBundleError, match="unknown repair"):
        load_profile_bundle(root)

    root = _write_bundle(tmp_path / "missing-prompt")
    _attach_field_repairs(root)
    manifest = _read_manifest(root)
    prompt_path = manifest["prompts"]["named_repairs"].pop(TOPIC_DETAIL_REPAIR)
    manifest["files"].pop(prompt_path)
    (root / prompt_path).unlink()
    _write_manifest(root, manifest)
    with pytest.raises(ProfileBundleError, match="must match exactly"):
        load_profile_bundle(root)
