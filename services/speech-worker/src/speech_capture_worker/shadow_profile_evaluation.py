"""Test-only dual-read evaluation for a future external meeting profile.

This module deliberately has no dependency on the job store, checkpoint writer,
summary revisions, artifact publication, or the HTTP API.  Phase B2 callers provide
two inert runners and the current invariant validator.  The external result and the
comparison report can only be written below the operating system's temporary
directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from speech_capture_worker.content_profiles import ProfileBundle, ProfileReference
from speech_capture_worker.structured_note_document import (
    StructuredNoteDocument,
    adapt_current_structured_document,
)

SHADOW_EVALUATION_SCHEMA_VERSION = "1.0.0"
SHADOW_MANUAL_SUPPLEMENT_POLICY = "protected-append-only"
SHADOW_PUBLICATION_INPUTS = (
    "note_markdown",
    "transcript_markdown",
    "evidence_markdown",
    "timeline_markdown",
    "speech_record",
)
BUILTIN_MEETING_SECTION_ORDER = (
    "objective",
    "summary",
    "context",
    "topics",
    "discussion_threads",
    "speaker_summaries",
    "highlights",
    "decisions",
    "actions",
    "risks",
    "open_questions",
)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(?:\d+(?:\.\d+)?%?|[一二三四五六七八九十百千万]+(?:个|项|次|天|周|月|年))"
)


class ShadowProfileEvaluationError(ValueError):
    """Raised before a shadow evaluation can write an isolated result."""


@dataclass(frozen=True)
class ShadowEvidenceSegment:
    segment_id: str
    text: str
    speaker_id: str | None
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.segment_id or not self.segment_id.isprintable():
            raise ShadowProfileEvaluationError("segment_id must be printable and non-empty.")
        if not self.text.strip():
            raise ShadowProfileEvaluationError("Synthetic evidence text must not be empty.")
        if self.speaker_id is not None and (
            not self.speaker_id or not self.speaker_id.isprintable()
        ):
            raise ShadowProfileEvaluationError("speaker_id must be printable when present.")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ShadowProfileEvaluationError("Synthetic evidence has an invalid time range.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


@dataclass(frozen=True)
class SyntheticEvidenceBundle:
    bundle_id: str
    content_type: str
    segments: tuple[ShadowEvidenceSegment, ...]
    recording_context: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.bundle_id or not self.bundle_id.isprintable():
            raise ShadowProfileEvaluationError("bundle_id must be printable and non-empty.")
        if self.content_type != "meeting":
            raise ShadowProfileEvaluationError("Phase B2 only permits synthetic meeting input.")
        if not self.segments:
            raise ShadowProfileEvaluationError("Synthetic evidence requires at least one segment.")
        segment_ids = [segment.segment_id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ShadowProfileEvaluationError("Synthetic segment ids must be unique.")
        starts = [segment.start_ms for segment in self.segments]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            raise ShadowProfileEvaluationError(
                "Synthetic segments must have unique ordered starts."
            )
        if not isinstance(self.recording_context, Mapping):
            raise ShadowProfileEvaluationError("recording_context must be a mapping.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "content_type": self.content_type,
            "segments": [segment.to_dict() for segment in self.segments],
            "recording_context": copy.deepcopy(dict(self.recording_context)),
        }


@dataclass(frozen=True)
class ShadowArtifactPlan:
    section_order: tuple[str, ...]
    timeline_output: str = "separate_markdown"
    evidence_output: str = "separate_markdown"
    manual_supplement_policy: str = SHADOW_MANUAL_SUPPLEMENT_POLICY
    publication_inputs: tuple[str, ...] = SHADOW_PUBLICATION_INPUTS

    @classmethod
    def current_builtin_meeting(cls) -> ShadowArtifactPlan:
        return cls(section_order=BUILTIN_MEETING_SECTION_ORDER)

    @classmethod
    def from_profile(cls, profile: ProfileBundle) -> ShadowArtifactPlan:
        sections = profile.renderer.get("sections")
        if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
            raise ShadowProfileEvaluationError("Validated profile renderer has no sections.")
        return cls(
            section_order=tuple(str(section["field"]) for section in sections),
            timeline_output=str(profile.renderer["timeline_output"]),
            evidence_output=str(profile.renderer["evidence_output"]),
        )


@dataclass(frozen=True)
class EquivalenceLayer:
    equivalent: bool
    checks: Mapping[str, bool]
    differences: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalent": self.equivalent,
            "checks": dict(self.checks),
            "differences": list(self.differences),
        }


@dataclass(frozen=True)
class ShadowEquivalenceReport:
    evidence_bundle_sha256: str
    builtin_profile: ProfileReference
    external_profile: ProfileReference
    builtin_document_sha256: str
    external_document_sha256: str
    contract: EquivalenceLayer
    semantic: EquivalenceLayer
    artifact: EquivalenceLayer

    @property
    def equivalent(self) -> bool:
        return self.contract.equivalent and self.semantic.equivalent and self.artifact.equivalent

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_EVALUATION_SCHEMA_VERSION,
            "equivalent": self.equivalent,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "builtin_profile": self.builtin_profile.to_dict(),
            "external_profile": self.external_profile.to_dict(),
            "builtin_document_sha256": self.builtin_document_sha256,
            "external_document_sha256": self.external_document_sha256,
            "contract": self.contract.to_dict(),
            "semantic": self.semantic.to_dict(),
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True)
class ShadowEvaluationResult:
    report: ShadowEquivalenceReport
    run_directory: Path
    external_document_path: Path
    report_path: Path


BuiltinRunner = Callable[[SyntheticEvidenceBundle], Mapping[str, Any]]
ExternalRunner = Callable[[SyntheticEvidenceBundle, ProfileBundle], Mapping[str, Any]]
InvariantValidator = Callable[
    [Mapping[str, Any], SyntheticEvidenceBundle],
    Mapping[str, Any],
]


def evaluate_meeting_profile_shadow(
    *,
    evidence: SyntheticEvidenceBundle,
    builtin_profile: ProfileReference,
    external_profile: ProfileBundle,
    builtin_runner: BuiltinRunner,
    external_runner: ExternalRunner,
    invariant_validator: InvariantValidator,
    output_root: str | Path,
    run_id: str,
    validated_at: str,
    builtin_artifact_plan: ShadowArtifactPlan | None = None,
) -> ShadowEvaluationResult:
    """Run isolated A/B extraction and write only B plus a comparison report.

    The same immutable ``SyntheticEvidenceBundle`` instance is passed to both
    runners.  Both raw documents then pass through the exact same validator
    callback.  No output directory is created until both paths have validated and
    all three equivalence layers have been computed.
    """

    if external_profile.content_type != "meeting":
        raise ShadowProfileEvaluationError("The external shadow profile must be meeting.")
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ShadowProfileEvaluationError("run_id has invalid syntax.")
    temp_root = _require_test_temporary_root(output_root)

    builtin_raw = _require_document_mapping(builtin_runner(evidence), label="builtin runner")
    external_raw = _require_document_mapping(
        external_runner(evidence, external_profile),
        label="external profile runner",
    )
    builtin_validated = _require_document_mapping(
        invariant_validator(copy.deepcopy(builtin_raw), evidence),
        label="builtin validated document",
    )
    external_validated = _require_document_mapping(
        invariant_validator(copy.deepcopy(external_raw), evidence),
        label="external validated document",
    )

    source_hashes = _source_hashes(evidence)
    builtin_envelope = _adapt_shadow_document(
        builtin_validated,
        evidence=evidence,
        profile=builtin_profile,
        validated_at=validated_at,
        source_hashes=source_hashes,
        path_name="builtin",
    )
    external_envelope = _adapt_shadow_document(
        external_validated,
        evidence=evidence,
        profile=external_profile.reference,
        validated_at=validated_at,
        source_hashes=source_hashes,
        path_name="external",
    )
    builtin_plan = builtin_artifact_plan or ShadowArtifactPlan.current_builtin_meeting()
    external_plan = ShadowArtifactPlan.from_profile(external_profile)
    report = compare_shadow_documents(
        builtin=builtin_envelope,
        external=external_envelope,
        evidence=evidence,
        builtin_artifact_plan=builtin_plan,
        external_artifact_plan=external_plan,
    )

    run_directory = temp_root / f"speech-capture-profile-shadow-{run_id}"
    try:
        run_directory.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as error:
        raise ShadowProfileEvaluationError("Shadow run directory already exists.") from error
    external_document_path = run_directory / "external-structured-note.json"
    report_path = run_directory / "equivalence-report.json"
    _write_json_exclusive(external_document_path, external_envelope.to_dict())
    _write_json_exclusive(report_path, report.to_dict())
    return ShadowEvaluationResult(
        report=report,
        run_directory=run_directory,
        external_document_path=external_document_path,
        report_path=report_path,
    )


def compare_shadow_documents(
    *,
    builtin: StructuredNoteDocument,
    external: StructuredNoteDocument,
    evidence: SyntheticEvidenceBundle,
    builtin_artifact_plan: ShadowArtifactPlan,
    external_artifact_plan: ShadowArtifactPlan,
) -> ShadowEquivalenceReport:
    """Compare validated stable envelopes without requiring byte-identical wording."""

    builtin_content = builtin.content
    external_content = external.content
    contract_checks = {
        "field_set": set(builtin_content) == set(external_content),
        "field_types": _shape_signature(builtin_content) == _shape_signature(external_content),
        "evidence_ids": _evidence_link_signature(builtin_content)
        == _evidence_link_signature(external_content),
        "known_evidence_ids": (
            _evidence_targets(builtin_content) | _evidence_targets(external_content)
        ).issubset({segment.segment_id for segment in evidence.segments}),
        "empty_fields": _empty_field_signature(builtin_content)
        == _empty_field_signature(external_content),
        "timeline_ranges": _timeline_signature(builtin_content)
        == _timeline_signature(external_content),
        "timeline_complete": _timeline_is_complete(builtin_content, evidence)
        and _timeline_is_complete(external_content, evidence),
    }
    semantic_checks = {
        "decision_facts": _outcome_fact_signatures(builtin_content, "decisions")
        == _outcome_fact_signatures(external_content, "decisions"),
        "action_facts": _action_fact_signatures(builtin_content)
        == _action_fact_signatures(external_content),
        "numeric_facts": _numeric_facts(builtin_content) == _numeric_facts(external_content),
        "people_and_organizations": _people_and_organizations(builtin_content)
        == _people_and_organizations(external_content),
        "unresolved_question_facts": _outcome_fact_signatures(builtin_content, "open_questions")
        == _outcome_fact_signatures(external_content, "open_questions"),
    }
    artifact_checks = {
        "section_order": builtin_artifact_plan.section_order
        == external_artifact_plan.section_order,
        "timeline_output": builtin_artifact_plan.timeline_output
        == external_artifact_plan.timeline_output,
        "evidence_output": builtin_artifact_plan.evidence_output
        == external_artifact_plan.evidence_output,
        "evidence_link_targets": _evidence_link_signature(builtin_content)
        == _evidence_link_signature(external_content),
        "manual_supplement_protection": builtin_artifact_plan.manual_supplement_policy
        == external_artifact_plan.manual_supplement_policy
        == SHADOW_MANUAL_SUPPLEMENT_POLICY,
        "publication_inputs": builtin_artifact_plan.publication_inputs
        == external_artifact_plan.publication_inputs
        == SHADOW_PUBLICATION_INPUTS,
    }
    return ShadowEquivalenceReport(
        evidence_bundle_sha256=_sha256_json(evidence.to_dict()),
        builtin_profile=builtin.profile,
        external_profile=external.profile,
        builtin_document_sha256=_sha256_json(builtin.to_dict()),
        external_document_sha256=_sha256_json(external.to_dict()),
        contract=_layer(contract_checks),
        semantic=_layer(semantic_checks),
        artifact=_layer(artifact_checks),
    )


def _adapt_shadow_document(
    document: Mapping[str, Any],
    *,
    evidence: SyntheticEvidenceBundle,
    profile: ProfileReference,
    validated_at: str,
    source_hashes: Mapping[str, str],
    path_name: str,
) -> StructuredNoteDocument:
    return adapt_current_structured_document(
        copy.deepcopy(dict(document)),
        document_id=f"synthetic-shadow:{evidence.bundle_id}:{path_name}",
        content_type=evidence.content_type,
        profile=profile,
        evidence_bundle_sha256=source_hashes["evidence"],
        corrected_transcript_sha256=source_hashes["transcript"],
        recording_context_sha256=source_hashes["context"],
        validated_at=validated_at,
    )


def _source_hashes(evidence: SyntheticEvidenceBundle) -> dict[str, str]:
    return {
        "evidence": _sha256_json(evidence.to_dict()),
        "transcript": _sha256_json(
            [
                {
                    "segment_id": segment.segment_id,
                    "text": segment.text,
                    "speaker_id": segment.speaker_id,
                }
                for segment in evidence.segments
            ]
        ),
        "context": _sha256_json(dict(evidence.recording_context)),
    }


def _shape_signature(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            "object",
            tuple(sorted((str(key), _shape_signature(item)) for key, item in value.items())),
        )
    if isinstance(value, list):
        return ("array", tuple(sorted({_shape_signature(item) for item in value}, key=repr)))
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _evidence_targets(value: Any) -> frozenset[str]:
    targets: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "evidence" and isinstance(item, list):
                targets.update(target for target in item if isinstance(target, str))
            else:
                targets.update(_evidence_targets(item))
    elif isinstance(value, list):
        for item in value:
            targets.update(_evidence_targets(item))
    return frozenset(targets)


def _evidence_link_signature(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    links: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = (*path, str(key))
            if key == "evidence" and isinstance(item, list):
                links.append(
                    (
                        next_path,
                        tuple(sorted(target for target in item if isinstance(target, str))),
                    )
                )
            else:
                links.extend(_evidence_link_signature(item, path=next_path))
    elif isinstance(value, list):
        for item in value:
            links.extend(_evidence_link_signature(item, path=path))
    return tuple(sorted(links))


def _empty_field_signature(content: Mapping[str, Any]) -> tuple[tuple[str, bool], ...]:
    return tuple(sorted((field, value in (None, "", [], {})) for field, value in content.items()))


def _timeline_signature(content: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = content.get("timeline_sections")
    if not isinstance(raw, list):
        return ()
    return tuple(
        (str(item.get("start_segment_id")), str(item.get("end_segment_id")))
        for item in raw
        if isinstance(item, Mapping)
    )


def _timeline_is_complete(content: Mapping[str, Any], evidence: SyntheticEvidenceBundle) -> bool:
    signature = _timeline_signature(content)
    if not signature:
        return False
    ordered_ids = [segment.segment_id for segment in evidence.segments]
    order = {segment_id: index for index, segment_id in enumerate(ordered_ids)}
    try:
        ranges = [(order[start], order[end]) for start, end in signature]
    except KeyError:
        return False
    if ranges[0][0] != 0 or ranges[-1][1] != len(ordered_ids) - 1:
        return False
    return all(
        start <= end and (index == 0 or start == ranges[index - 1][1] + 1)
        for index, (start, end) in enumerate(ranges)
    )


def _outcome_fact_signatures(
    content: Mapping[str, Any], field: str
) -> frozenset[tuple[tuple[str, ...], tuple[str, ...]]]:
    raw = content.get(field)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        (
            tuple(sorted(str(item) for item in value.get("evidence", []))),
            tuple(sorted(_numbers_from_value(value))),
        )
        for value in raw
        if isinstance(value, Mapping)
    )


def _action_fact_signatures(
    content: Mapping[str, Any],
) -> frozenset[tuple[tuple[str, ...], str, str, tuple[str, ...]]]:
    raw = content.get("actions")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        (
            tuple(sorted(str(item) for item in value.get("evidence", []))),
            _normalize_fact_text(value.get("owner")),
            _normalize_fact_text(value.get("deadline")),
            tuple(sorted(_numbers_from_value(value))),
        )
        for value in raw
        if isinstance(value, Mapping)
    )


def _numeric_facts(content: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(_numbers_from_value(content))


def _numbers_from_value(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        numbers: set[str] = set()
        for item in value.values():
            numbers.update(_numbers_from_value(item))
        return numbers
    if isinstance(value, list):
        numbers = set()
        for item in value:
            numbers.update(_numbers_from_value(item))
        return numbers
    if isinstance(value, str):
        return {_normalize_fact_text(match.group(0)) for match in _NUMBER_PATTERN.finditer(value)}
    return set()


def _people_and_organizations(content: Mapping[str, Any]) -> frozenset[str]:
    facts: set[str] = set()
    context = content.get("context")
    if isinstance(context, list):
        for item in context:
            if isinstance(item, Mapping) and item.get("kind") in {"participant", "organization"}:
                facts.add(_normalize_fact_text(item.get("title")))
    speakers = content.get("speaker_summaries")
    if isinstance(speakers, list):
        for item in speakers:
            if not isinstance(item, Mapping):
                continue
            for field in ("speaker_id", "display_name", "affiliation", "role"):
                normalized = _normalize_fact_text(item.get(field))
                if normalized:
                    facts.add(normalized)
    return frozenset(facts)


def _normalize_fact_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def _layer(checks: Mapping[str, bool]) -> EquivalenceLayer:
    differences = tuple(name for name, passed in checks.items() if not passed)
    return EquivalenceLayer(
        equivalent=not differences,
        checks=dict(checks),
        differences=differences,
    )


def _require_document_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ShadowProfileEvaluationError(f"{label} did not return a document mapping.")
    return copy.deepcopy(dict(value))


def _require_test_temporary_root(value: str | Path) -> Path:
    root = Path(value)
    if root.is_symlink() or not root.is_dir():
        raise ShadowProfileEvaluationError("Shadow output root must be a real existing directory.")
    resolved = root.resolve(strict=True)
    system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    try:
        resolved.relative_to(system_temp)
    except ValueError as error:
        raise ShadowProfileEvaluationError(
            "Shadow output is restricted to the operating system temporary directory."
        ) from error
    return resolved


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
