"""Phase B2 synthetic-only shadow dual-read tests."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

import speech_capture_worker.shadow_profile_evaluation as shadow_module
from speech_capture_worker.content_profile_prompts import (
    MeetingProfilePrompts,
    load_bundled_meeting_profile,
)
from speech_capture_worker.content_profiles import (
    ProfileReference,
    compute_bundle_sha256,
    load_profile_bundle,
)
from speech_capture_worker.errors import StructuringFailed
from speech_capture_worker.shadow_profile_evaluation import (
    BUILTIN_MEETING_SECTION_ORDER,
    ShadowEvidenceSegment,
    ShadowProfileEvaluationError,
    SyntheticEvidenceBundle,
    evaluate_meeting_profile_shadow,
)
from speech_capture_worker.structuring_execution import ContentType, _validate_document

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "content-profile-b1" / "current-meeting-document.json"
)
BUILTIN_PROFILE = ProfileReference(
    profile_id="speech-capture/meeting",
    profile_version="builtin-2026-08-27.1",
    bundle_sha256="sha256:" + "a" * 64,
)


def _write_profile_bundle(
    root: Path,
    *,
    section_order: tuple[str, ...] = BUILTIN_MEETING_SECTION_ORDER,
) -> Path:
    root.mkdir()
    payloads = {
        "extract.prompt.md": "只提取公开合成会议中有直接证据的事实。\n".encode(),
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
                    {
                        "field": field,
                        "heading": f"合成栏目 {index}",
                        "when": "always" if field == "summary" else "nonempty",
                    }
                    for index, field in enumerate(section_order)
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
                        "id": "synthetic-meeting-shadow",
                        "description": "不含真实用户、公司、音频或笔记内容的公开合成基线",
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
    manifest = {
        "bundle_schema_version": "1.0.0",
        "profile_id": "speech-capture/meeting",
        "profile_version": "2026-08-28.synthetic.2",
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
            "profile_id": BUILTIN_PROFILE.profile_id,
            "profile_version": BUILTIN_PROFILE.profile_version,
        },
        "files": {
            path: f"sha256:{hashlib.sha256(content).hexdigest()}"
            for path, content in payloads.items()
        },
        "bundle_sha256": "sha256:" + "0" * 64,
    }
    manifest["bundle_sha256"] = compute_bundle_sha256(manifest)
    (root / "profile.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _evidence() -> SyntheticEvidenceBundle:
    return SyntheticEvidenceBundle(
        bundle_id="public-synthetic-meeting-b2",
        content_type="meeting",
        segments=(
            ShadowEvidenceSegment(
                segment_id="seg_0001",
                text=(
                    "今天会议目标是确认合成数据治理规则。本记录只包含虚构内容，"
                    "测试团队将公开合成基线作为验证依据。"
                ),
                speaker_id="speaker_01",
                start_ms=0,
                end_ms=5_000,
            ),
            ShadowEvidenceSegment(
                segment_id="seg_0002",
                text=(
                    "大家确认现有内嵌路径继续作为默认路径，决定 B1 不接入运行时。"
                    "测试团队负责完成严格 loader 和 adapter 测试。"
                ),
                speaker_id="speaker_01",
                start_ms=5_000,
                end_ms=10_000,
            ),
        ),
        recording_context={"background": "公开合成会议，只用于 B2 影子验证。"},
    )


def _raw_document() -> dict:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    document.pop("chapters")
    document["timeline_sections"] = [
        {
            "title": "确认范围",
            "summary": "确认只使用公开合成数据。",
            "details": [],
            "start_segment_id": "seg_0001",
            "end_segment_id": "seg_0001",
        },
        {
            "title": "确定边界",
            "summary": "确定默认路径和测试责任。",
            "details": [],
            "start_segment_id": "seg_0002",
            "end_segment_id": "seg_0002",
        },
    ]
    return document


def _external_document() -> dict:
    document = _raw_document()
    document["title"] = "合成会议治理边界确认"
    document["summary"]["text"] = "团队先确认公开合成基线，再校验配置加载与适配。"
    document["topics"][0]["summary"] = "首批迁移仅建立严格加载与文档适配。"
    document["topics"][0]["details"][0]["text"] = "实际 Note 生成仍保持原路径。"
    return document


def _invariant_validator(raw: dict, evidence: SyntheticEvidenceBundle) -> dict:
    return _validate_document(
        raw,
        segment_ids={segment.segment_id for segment in evidence.segments},
        speaker_ids={segment.speaker_id for segment in evidence.segments if segment.speaker_id},
        content_type=ContentType.MEETING,
        segment_texts={segment.segment_id: segment.text for segment in evidence.segments},
        segment_speakers={segment.segment_id: segment.speaker_id for segment in evidence.segments},
        segment_starts={segment.segment_id: segment.start_ms for segment in evidence.segments},
    )


def test_shadow_dual_read_is_equivalent_and_writes_only_isolated_b_output(
    tmp_path: Path,
) -> None:
    profile = load_profile_bundle(_write_profile_bundle(tmp_path / "profile"))
    evidence = _evidence()
    evidence_ids: list[int] = []
    validation_ids: list[int] = []
    sentinel = tmp_path / "formal-state-sentinel.json"
    sentinel.write_text('{"unchanged":true}\n', encoding="utf-8")
    shadow_root = tmp_path / "shadow-output"
    shadow_root.mkdir()

    def builtin_runner(received: SyntheticEvidenceBundle) -> dict:
        evidence_ids.append(id(received))
        return _raw_document()

    def external_runner(received: SyntheticEvidenceBundle, received_profile) -> dict:
        evidence_ids.append(id(received))
        assert received_profile is profile
        assert received_profile.read_prompt("extraction") is not None
        return _external_document()

    def validator(raw: dict, received: SyntheticEvidenceBundle) -> dict:
        validation_ids.append(id(received))
        return _invariant_validator(raw, received)

    result = evaluate_meeting_profile_shadow(
        evidence=evidence,
        builtin_profile=BUILTIN_PROFILE,
        external_profile=profile,
        builtin_runner=builtin_runner,
        external_runner=external_runner,
        invariant_validator=validator,
        output_root=shadow_root,
        run_id="equivalent-public-fixture",
        validated_at="2026-08-28T10:00:00+08:00",
    )

    assert evidence_ids == [id(evidence), id(evidence)]
    assert validation_ids == [id(evidence), id(evidence)]
    assert result.report.equivalent is True
    assert result.report.contract.checks["timeline_ranges"] is True
    assert result.report.contract.checks["timeline_complete"] is True
    assert sentinel.read_text(encoding="utf-8") == '{"unchanged":true}\n'
    assert {path.name for path in result.run_directory.iterdir()} == {
        "external-structured-note.json",
        "equivalence-report.json",
    }
    assert not (result.run_directory / "builtin-structured-note.json").exists()
    forbidden_names = {
        "checkpoint.json",
        "candidate.json",
        "summary-revision.json",
        "publication.json",
        "current-state.json",
    }
    assert not forbidden_names.intersection(path.name for path in result.run_directory.rglob("*"))
    external_payload = json.loads(result.external_document_path.read_text(encoding="utf-8"))
    assert external_payload["profile"] == profile.reference.to_dict()


def test_repository_meeting_profile_participates_in_equivalent_synthetic_dual_read(
    tmp_path: Path,
) -> None:
    profile = load_bundled_meeting_profile()
    observed_profile_prompts: list[MeetingProfilePrompts] = []

    def external_runner(evidence: SyntheticEvidenceBundle, received_profile) -> dict:
        assert evidence is synthetic_evidence
        observed_profile_prompts.append(MeetingProfilePrompts.from_bundle(received_profile))
        return _raw_document()

    synthetic_evidence = _evidence()
    result = evaluate_meeting_profile_shadow(
        evidence=synthetic_evidence,
        builtin_profile=BUILTIN_PROFILE,
        external_profile=profile,
        builtin_runner=lambda evidence: _raw_document(),
        external_runner=external_runner,
        invariant_validator=_invariant_validator,
        output_root=tmp_path,
        run_id="repository-profile-equivalent",
        validated_at="2026-08-28T10:30:00+08:00",
    )

    assert result.report.equivalent is True
    assert result.report.external_profile == profile.reference
    assert len(observed_profile_prompts) == 1
    assert "输出顺序和阅读逻辑" in observed_profile_prompts[0].synthesis


def test_shadow_report_exposes_semantic_and_artifact_mismatch(tmp_path: Path) -> None:
    mismatched_order = (
        "summary",
        "objective",
        *BUILTIN_MEETING_SECTION_ORDER[2:],
    )
    profile = load_profile_bundle(
        _write_profile_bundle(tmp_path / "profile", section_order=mismatched_order)
    )

    def external_runner(evidence: SyntheticEvidenceBundle, _profile) -> dict:
        document = _external_document()
        document["actions"][0]["owner"] = ""
        return document

    result = evaluate_meeting_profile_shadow(
        evidence=_evidence(),
        builtin_profile=BUILTIN_PROFILE,
        external_profile=profile,
        builtin_runner=lambda evidence: _raw_document(),
        external_runner=external_runner,
        invariant_validator=_invariant_validator,
        output_root=tmp_path,
        run_id="mismatch-public-fixture",
        validated_at="2026-08-28T10:00:00+08:00",
    )

    assert result.report.equivalent is False
    assert result.report.contract.equivalent is True
    assert result.report.semantic.checks["action_facts"] is False
    assert result.report.artifact.checks["section_order"] is False


def test_invariant_failure_creates_no_shadow_result(tmp_path: Path) -> None:
    profile = load_profile_bundle(_write_profile_bundle(tmp_path / "profile"))
    invalid_external = _external_document()
    invalid_external["summary"]["evidence"] = ["unknown-segment"]
    run_directory = tmp_path / "speech-capture-profile-shadow-invalid-external"

    with pytest.raises(StructuringFailed):
        evaluate_meeting_profile_shadow(
            evidence=_evidence(),
            builtin_profile=BUILTIN_PROFILE,
            external_profile=profile,
            builtin_runner=lambda evidence: _raw_document(),
            external_runner=lambda evidence, bundle: copy.deepcopy(invalid_external),
            invariant_validator=_invariant_validator,
            output_root=tmp_path,
            run_id="invalid-external",
            validated_at="2026-08-28T10:00:00+08:00",
        )

    assert not run_directory.exists()


def test_shadow_output_rejects_non_temporary_directory(tmp_path: Path) -> None:
    profile = load_profile_bundle(_write_profile_bundle(tmp_path / "profile"))

    with pytest.raises(ShadowProfileEvaluationError, match="temporary directory"):
        evaluate_meeting_profile_shadow(
            evidence=_evidence(),
            builtin_profile=BUILTIN_PROFILE,
            external_profile=profile,
            builtin_runner=lambda evidence: pytest.fail("runner must not execute"),
            external_runner=lambda evidence, bundle: pytest.fail("runner must not execute"),
            invariant_validator=_invariant_validator,
            output_root=Path(__file__).parent,
            run_id="outside-temp",
            validated_at="2026-08-28T10:00:00+08:00",
        )


def test_shadow_module_imports_no_formal_state_or_publication_components() -> None:
    source_path = Path(shadow_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    forbidden = {
        "speech_capture_worker.job_store",
        "speech_capture_worker.checkpoints",
        "speech_capture_worker.summary_revisions",
        "speech_capture_worker.artifact_generation",
        "speech_capture_worker.publication",
        "speech_capture_worker.api",
    }

    assert imported_modules.isdisjoint(forbidden)
