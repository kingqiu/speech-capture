"""Bounded, deterministic planning for future meeting field repairs.

This module does not call a model, persist checkpoints, create revisions, or
publish artifacts.  It converts explicit semantic issues into immutable repair
plans over a minimal evidence packet.  Runtime integration is intentionally
deferred until the public synthetic contract and refusal tests are accepted.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

QUANTITATIVE_PROMOTION_REPAIR = "meeting_quantitative_promotion"
SPEAKER_GROUNDING_REPAIR = "meeting_speaker_grounding"
TOPIC_DETAIL_REPAIR = "meeting_topic_detail"
MEETING_FIELD_REPAIR_KEYS = frozenset(
    {
        QUANTITATIVE_PROMOTION_REPAIR,
        SPEAKER_GROUNDING_REPAIR,
        TOPIC_DETAIL_REPAIR,
    }
)

QUANTITATIVE_PROMOTION_ISSUE = "important_quantitative_facts_not_promoted"
TOPIC_DETAIL_ISSUE = "meeting_topic_detail_insufficient"
SPEAKER_GROUNDING_ISSUES = frozenset(
    {
        "speaker_summary_has_no_self_evidence",
        "speaker_summaries_are_duplicated",
        "speaker_summary_sentence_not_grounded",
    }
)
DETERMINISTIC_ONLY_ISSUES = frozenset(
    {
        "summary_claims_missing_decisions",
        "summary_claims_missing_assignments",
        "summary_claims_missing_deadlines",
        "evidence_backed_highlights_removed",
        "evidence_backed_decisions_removed",
        "evidence_backed_actions_removed",
        "evidence_backed_risks_removed",
        "evidence_backed_open_questions_removed",
        "speaker_role_not_directly_supported",
        "speaker_affiliation_not_directly_supported",
    }
)

MAX_REPAIR_CALLS = 3
MAX_PACKET_SEGMENTS = 24
MAX_PACKET_CHARACTERS = 12_000
MAX_PACKET_ESTIMATED_TOKENS = 4_000
MAX_ADJACENT_SEGMENTS = 1
MAX_REPAIR_OUTPUT_TOKENS = 1_536
MAX_REPAIR_FIELD_CHARACTERS = 1_200
MAX_FIELD_CALL_SECONDS = 120.0
MAX_TOTAL_REPAIR_SECONDS = 180.0
MAX_PARSER_RETRIES_PER_REPAIR = 1
MAX_HEARTBEAT_SECONDS = 10.0

_QUANTITATIVE_FIELDS = frozenset({"highlights", "topics", "actions", "open_questions"})
_IMPORTANT_NUMBER = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[-–—至到]\s*\d+(?:\.\d+)?\s*"
    r"(?:%|年|个月|月|周|天|个|项|款|份|层|套))|"
    r"(?:\d+(?:\.\d+)?\s*(?:%|年|个月|月|周|天|个|项|款|份|层|套))"
)
_EVIDENCE_TEXT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_REPAIR_FIELD_CHARACTERS,
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": ["text", "evidence"],
    "additionalProperties": False,
}
_ACTION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "minLength": 1, "maxLength": 600},
        "owner": {"const": ""},
        "deadline": {"const": ""},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": ["task", "owner", "deadline", "evidence"],
    "additionalProperties": False,
}
_SPEAKER_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker_id": {"type": "string", "minLength": 1},
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_REPAIR_FIELD_CHARACTERS,
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": ["speaker_id", "summary", "evidence"],
    "additionalProperties": False,
}


class MeetingFieldRepairPlanningError(ValueError):
    """Raised when a bounded field repair cannot be planned safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MeetingFieldTarget:
    """One unambiguous field or list item in the immutable baseline."""

    field: str
    item_index: int | None = None
    item_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "item_index": self.item_index,
            "item_key": self.item_key,
        }

    @property
    def identity(self) -> tuple[str, int | None, str | None]:
        return (self.field, self.item_index, self.item_key)


@dataclass(frozen=True)
class MeetingRepairIssue:
    """A validator failure enriched with an explicit target and evidence anchors."""

    code: str
    target: MeetingFieldTarget
    anchor_segment_ids: tuple[str, ...]
    speaker_id: str | None = None


@dataclass(frozen=True)
class MeetingEvidencePacket:
    """A bounded evidence packet that is safe to pass to one field repair."""

    repair_key: str
    target: MeetingFieldTarget
    segments: tuple[Mapping[str, Any], ...]
    character_count: int
    estimated_tokens: int
    packet_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_key": self.repair_key,
            "target": self.target.to_dict(),
            "segments": [copy.deepcopy(dict(segment)) for segment in self.segments],
            "character_count": self.character_count,
            "estimated_tokens": self.estimated_tokens,
            "packet_sha256": self.packet_sha256,
        }


@dataclass(frozen=True)
class MeetingFieldRepairPlan:
    """One model-free plan entry with optimistic concurrency preconditions."""

    issue_code: str
    repair_key: str
    target: MeetingFieldTarget
    baseline_field_json: str
    baseline_field_sha256: str
    evidence_packet: MeetingEvidencePacket

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_code": self.issue_code,
            "repair_key": self.repair_key,
            "target": self.target.to_dict(),
            "baseline_field": json.loads(self.baseline_field_json),
            "baseline_field_sha256": self.baseline_field_sha256,
            "evidence_packet": self.evidence_packet.to_dict(),
        }


def plan_meeting_field_repairs(
    *,
    baseline: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    issues: Sequence[MeetingRepairIssue],
) -> tuple[MeetingFieldRepairPlan, ...]:
    """Create at most three non-overlapping repair plans without calling a model."""

    if len(issues) > MAX_REPAIR_CALLS:
        raise MeetingFieldRepairPlanningError(
            "repair_call_budget_exceeded",
            f"A meeting candidate may plan at most {MAX_REPAIR_CALLS} field repairs.",
        )
    planned_targets: set[tuple[str, int | None, str | None]] = set()
    plans: list[MeetingFieldRepairPlan] = []
    for issue in issues:
        repair_key = _repair_key_for_issue(issue)
        if issue.target.identity in planned_targets:
            raise MeetingFieldRepairPlanningError(
                "overlapping_repair_targets",
                "Multiple meeting repairs may not modify the same target.",
            )
        target_value = _target_value(baseline, issue.target)
        baseline_field_json = canonical_json_text(target_value)
        packet = build_meeting_evidence_packet(
            repair_key=repair_key,
            target=issue.target,
            segments=segments,
            anchor_segment_ids=issue.anchor_segment_ids,
            speaker_id=issue.speaker_id,
        )
        plans.append(
            MeetingFieldRepairPlan(
                issue_code=issue.code,
                repair_key=repair_key,
                target=issue.target,
                baseline_field_json=baseline_field_json,
                baseline_field_sha256=canonical_json_sha256(target_value),
                evidence_packet=packet,
            )
        )
        planned_targets.add(issue.target.identity)
    return tuple(plans)


def build_meeting_evidence_packet(
    *,
    repair_key: str,
    target: MeetingFieldTarget,
    segments: Sequence[Mapping[str, Any]],
    anchor_segment_ids: Sequence[str],
    speaker_id: str | None = None,
    adjacent_segments: int = MAX_ADJACENT_SEGMENTS,
) -> MeetingEvidencePacket:
    """Select anchors and bounded adjacency; refuse instead of truncating."""

    if not anchor_segment_ids:
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_missing",
            "A meeting field repair requires at least one evidence anchor.",
        )
    if adjacent_segments < 0 or adjacent_segments > MAX_ADJACENT_SEGMENTS:
        raise MeetingFieldRepairPlanningError(
            "repair_adjacency_out_of_bounds",
            f"Evidence adjacency must be between 0 and {MAX_ADJACENT_SEGMENTS}.",
        )
    normalized_segments = tuple(_normalize_segment(segment) for segment in segments)
    segment_positions = {
        str(segment["segment_id"]): index for index, segment in enumerate(normalized_segments)
    }
    if len(segment_positions) != len(normalized_segments):
        raise MeetingFieldRepairPlanningError(
            "duplicate_segment_id",
            "Meeting evidence segment ids must be unique.",
        )
    unknown = tuple(
        dict.fromkeys(
            segment_id
            for segment_id in anchor_segment_ids
            if segment_id not in segment_positions
        )
    )
    if unknown:
        raise MeetingFieldRepairPlanningError(
            "unknown_evidence_anchor",
            "A meeting field repair referenced an unknown evidence segment.",
        )
    if repair_key == SPEAKER_GROUNDING_REPAIR and not any(
        normalized_segments[segment_positions[segment_id]]["speaker_id"] == speaker_id
        for segment_id in anchor_segment_ids
    ):
        raise MeetingFieldRepairPlanningError(
            "speaker_repair_has_no_self_anchor",
            "Speaker grounding requires at least one anchor spoken by the target speaker.",
        )

    selected_positions: set[int] = set()
    for segment_id in dict.fromkeys(anchor_segment_ids):
        anchor_position = segment_positions[segment_id]
        start = max(0, anchor_position - adjacent_segments)
        end = min(len(normalized_segments), anchor_position + adjacent_segments + 1)
        for position in range(start, end):
            segment = normalized_segments[position]
            if repair_key == SPEAKER_GROUNDING_REPAIR and segment["speaker_id"] != speaker_id:
                continue
            selected_positions.add(position)

    selected = tuple(
        copy.deepcopy(normalized_segments[index]) for index in sorted(selected_positions)
    )
    if not selected:
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_missing",
            "The bounded meeting evidence packet is empty after applying constraints.",
        )
    if len(selected) > MAX_PACKET_SEGMENTS:
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_segment_limit_exceeded",
            "The bounded meeting evidence packet exceeds its segment limit.",
        )
    character_count = sum(len(str(segment["text"])) for segment in selected)
    estimated_tokens = character_count
    if character_count > MAX_PACKET_CHARACTERS:
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_character_limit_exceeded",
            "The bounded meeting evidence packet exceeds its character limit.",
        )
    if estimated_tokens > MAX_PACKET_ESTIMATED_TOKENS:
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_token_limit_exceeded",
            "The bounded meeting evidence packet exceeds its conservative token estimate.",
        )
    packet_payload = {
        "repair_key": repair_key,
        "target": target.to_dict(),
        "segments": list(selected),
        "character_count": character_count,
        "estimated_tokens": estimated_tokens,
    }
    return MeetingEvidencePacket(
        repair_key=repair_key,
        target=target,
        segments=tuple(MappingProxyType(dict(segment)) for segment in selected),
        character_count=character_count,
        estimated_tokens=estimated_tokens,
        packet_sha256=canonical_json_sha256(packet_payload),
    )


def canonical_json_sha256(value: Any) -> str:
    """Return a stable field or packet precondition hash."""

    return f"sha256:{hashlib.sha256(canonical_json_text(value).encode('utf-8')).hexdigest()}"


def canonical_json_text(value: Any) -> str:
    """Return immutable canonical JSON for a bounded baseline field value."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise MeetingFieldRepairPlanningError(
            "noncanonical_repair_value",
            "Meeting repair values must be canonical JSON.",
        ) from error


def current_target_sha256(
    document: Mapping[str, Any], target: MeetingFieldTarget
) -> str:
    """Recompute a target hash immediately before a future atomic merge."""

    return canonical_json_sha256(_target_value(document, target))


def meeting_repair_result_json_schema(plan: MeetingFieldRepairPlan) -> dict[str, Any]:
    """Return the strict, target-specific schema for one future short model call."""

    if plan.repair_key == SPEAKER_GROUNDING_REPAIR:
        schema = copy.deepcopy(_SPEAKER_RESULT_SCHEMA)
        schema["properties"]["speaker_id"] = {"const": plan.target.item_key}
        return schema
    if plan.repair_key in {QUANTITATIVE_PROMOTION_REPAIR, TOPIC_DETAIL_REPAIR}:
        item_schema = (
            _ACTION_ITEM_SCHEMA if plan.target.field == "actions" else _EVIDENCE_TEXT_ITEM_SCHEMA
        )
        maximum = 3
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": copy.deepcopy(item_schema),
                    "minItems": 1,
                    "maxItems": maximum,
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
    raise MeetingFieldRepairPlanningError(
        "unknown_meeting_repair_key",
        "The meeting field repair key has no registered result schema.",
    )


def validate_and_merge_meeting_field_repairs(
    *,
    baseline: Mapping[str, Any],
    plans: Sequence[MeetingFieldRepairPlan],
    results: Sequence[Mapping[str, Any]],
    final_validator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate all local results, atomically merge, then require the full gate."""

    if len(plans) != len(results):
        raise MeetingFieldRepairPlanningError(
            "repair_result_count_mismatch",
            "Every meeting field repair plan requires exactly one result.",
        )
    if len(plans) > MAX_REPAIR_CALLS:
        raise MeetingFieldRepairPlanningError(
            "repair_call_budget_exceeded",
            f"A meeting candidate may merge at most {MAX_REPAIR_CALLS} field repairs.",
        )
    target_identities = [plan.target.identity for plan in plans]
    if len(target_identities) != len(set(target_identities)):
        raise MeetingFieldRepairPlanningError(
            "overlapping_repair_targets",
            "Multiple meeting repairs may not modify the same target.",
        )

    validated_results: list[dict[str, Any]] = []
    for plan, result in zip(plans, results, strict=True):
        expected_repair_key = _repair_key_for_issue(
            MeetingRepairIssue(
                code=plan.issue_code,
                target=plan.target,
                anchor_segment_ids=tuple(
                    str(segment["segment_id"])
                    for segment in plan.evidence_packet.segments
                ),
                speaker_id=(
                    plan.target.item_key
                    if plan.repair_key == SPEAKER_GROUNDING_REPAIR
                    else None
                ),
            )
        )
        if expected_repair_key != plan.repair_key:
            raise MeetingFieldRepairPlanningError(
                "repair_plan_registration_mismatch",
                "The meeting repair issue and registered repair key do not match.",
            )
        if current_target_sha256(baseline, plan.target) != plan.baseline_field_sha256:
            raise MeetingFieldRepairPlanningError(
                "repair_target_hash_changed",
                "The meeting repair target changed after planning.",
            )
        if plan.evidence_packet.repair_key != plan.repair_key:
            raise MeetingFieldRepairPlanningError(
                "repair_plan_packet_mismatch",
                "The meeting repair plan and evidence packet do not match.",
            )
        if plan.evidence_packet.target != plan.target:
            raise MeetingFieldRepairPlanningError(
                "repair_plan_packet_mismatch",
                "The meeting repair target and evidence packet do not match.",
            )
        packet_payload = {
            "repair_key": plan.evidence_packet.repair_key,
            "target": plan.evidence_packet.target.to_dict(),
            "segments": [dict(segment) for segment in plan.evidence_packet.segments],
            "character_count": plan.evidence_packet.character_count,
            "estimated_tokens": plan.evidence_packet.estimated_tokens,
        }
        if canonical_json_sha256(packet_payload) != plan.evidence_packet.packet_sha256:
            raise MeetingFieldRepairPlanningError(
                "repair_packet_hash_changed",
                "The bounded meeting evidence packet changed after planning.",
            )
        validated_results.append(_validate_repair_result(plan, result))

    merged = copy.deepcopy(dict(baseline))
    for plan, result in zip(plans, validated_results, strict=True):
        _merge_validated_result(merged, plan, result)

    finalized = final_validator(copy.deepcopy(merged))
    if not isinstance(finalized, Mapping):
        raise MeetingFieldRepairPlanningError(
            "final_meeting_validator_returned_invalid_document",
            "The final meeting validator must return a document object.",
        )
    finalized_document = copy.deepcopy(dict(finalized))
    targeted_fields = {plan.target.field for plan in plans}
    for field, value in baseline.items():
        if (
            field not in targeted_fields
            and canonical_json_sha256(finalized_document.get(field))
            != canonical_json_sha256(value)
        ):
            raise MeetingFieldRepairPlanningError(
                "untouched_field_changed",
                "A meeting field repair modified an unauthorized document field.",
            )
    return finalized_document


def validate_meeting_field_repair_result(
    plan: MeetingFieldRepairPlan, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one parsed result without merging or mutating the baseline."""

    return _validate_repair_result(plan, result)


def _repair_key_for_issue(issue: MeetingRepairIssue) -> str:
    if issue.code in DETERMINISTIC_ONLY_ISSUES or issue.code.startswith("evidence_backed_"):
        raise MeetingFieldRepairPlanningError(
            "deterministic_issue_not_model_repairable",
            "This meeting semantic issue must be repaired or rejected deterministically.",
        )
    if issue.code == QUANTITATIVE_PROMOTION_ISSUE:
        target_is_valid = issue.target.field in _QUANTITATIVE_FIELDS
        if issue.target.field == "topics":
            target_is_valid = (
                target_is_valid
                and issue.target.item_index is not None
                and issue.target.item_key is None
            )
        else:
            target_is_valid = (
                target_is_valid
                and issue.target.item_index is None
                and issue.target.item_key is None
            )
        if not target_is_valid or issue.speaker_id is not None:
            raise MeetingFieldRepairPlanningError(
                "invalid_quantitative_repair_target",
                "Quantitative promotion requires one approved result or topic target.",
            )
        return QUANTITATIVE_PROMOTION_REPAIR
    if issue.code in SPEAKER_GROUNDING_ISSUES:
        if (
            issue.target.field != "speaker_summaries"
            or issue.target.item_key is None
            or issue.target.item_index is not None
            or issue.speaker_id != issue.target.item_key
        ):
            raise MeetingFieldRepairPlanningError(
                "invalid_speaker_repair_target",
                "Speaker grounding requires one explicit speaker summary target.",
            )
        return SPEAKER_GROUNDING_REPAIR
    if issue.code == TOPIC_DETAIL_ISSUE:
        if (
            issue.target.field != "topics"
            or issue.target.item_index is None
            or issue.target.item_key is not None
            or issue.speaker_id is not None
        ):
            raise MeetingFieldRepairPlanningError(
                "invalid_topic_repair_target",
                "Topic detail repair requires one explicit topic index.",
            )
        return TOPIC_DETAIL_REPAIR
    raise MeetingFieldRepairPlanningError(
        "unknown_meeting_repair_issue",
        "The meeting semantic issue is not registered for field repair.",
    )


def _target_value(document: Mapping[str, Any], target: MeetingFieldTarget) -> Any:
    if target.field not in document:
        raise MeetingFieldRepairPlanningError(
            "repair_target_missing",
            "The requested meeting repair field is absent from the baseline.",
        )
    value = document[target.field]
    if target.item_index is not None and target.item_key is not None:
        raise MeetingFieldRepairPlanningError(
            "ambiguous_repair_target",
            "A meeting repair target cannot use both an index and item key.",
        )
    if target.item_index is not None:
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or target.item_index < 0
            or target.item_index >= len(value)
        ):
            raise MeetingFieldRepairPlanningError(
                "repair_target_missing",
                "The requested meeting repair list item is absent from the baseline.",
            )
        return copy.deepcopy(value[target.item_index])
    if target.item_key is not None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise MeetingFieldRepairPlanningError(
                "repair_target_missing",
                "The requested keyed repair field is not a list.",
            )
        matches = [
            item
            for item in value
            if isinstance(item, Mapping) and item.get("speaker_id") == target.item_key
        ]
        if len(matches) != 1:
            raise MeetingFieldRepairPlanningError(
                "repair_target_not_unique",
                "The requested keyed meeting repair target is not unique.",
            )
        return copy.deepcopy(matches[0])
    return copy.deepcopy(value)


def _normalize_segment(segment: Mapping[str, Any]) -> dict[str, Any]:
    segment_id = segment.get("segment_id")
    text = segment.get("text")
    speaker_id = segment.get("speaker_id")
    if not isinstance(segment_id, str) or not segment_id or not segment_id.isprintable():
        raise MeetingFieldRepairPlanningError(
            "invalid_evidence_segment",
            "Meeting evidence requires a printable non-empty segment id.",
        )
    if not isinstance(text, str) or not text.strip():
        raise MeetingFieldRepairPlanningError(
            "invalid_evidence_segment",
            "Meeting evidence requires non-empty text.",
        )
    if speaker_id is not None and (
        not isinstance(speaker_id, str) or not speaker_id or not speaker_id.isprintable()
    ):
        raise MeetingFieldRepairPlanningError(
            "invalid_evidence_segment",
            "Meeting evidence speaker ids must be printable when present.",
        )
    return {
        "segment_id": segment_id,
        "speaker_id": speaker_id,
        "text": text,
    }


def _validate_repair_result(
    plan: MeetingFieldRepairPlan, result: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise MeetingFieldRepairPlanningError(
            "invalid_repair_result",
            "A meeting field repair result must be an object.",
        )
    packet_segment_ids = {
        str(segment["segment_id"]) for segment in plan.evidence_packet.segments
    }
    if plan.repair_key == SPEAKER_GROUNDING_REPAIR:
        if set(result) != {"speaker_id", "summary", "evidence"}:
            raise MeetingFieldRepairPlanningError(
                "repair_result_has_unauthorized_fields",
                "Speaker grounding may only return speaker_id, summary, and evidence.",
            )
        speaker_id = result.get("speaker_id")
        summary = result.get("summary")
        if (
            speaker_id != plan.target.item_key
            or not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 1200
        ):
            raise MeetingFieldRepairPlanningError(
                "invalid_speaker_repair_result",
                "Speaker grounding must return the planned speaker and a non-empty summary.",
            )
        evidence = _validated_result_evidence(result.get("evidence"), packet_segment_ids)
        _require_numbers_grounded(summary, evidence, plan.evidence_packet.segments)
        return {
            "speaker_id": speaker_id,
            "summary": summary.strip(),
            "evidence": evidence,
        }

    if plan.repair_key not in {QUANTITATIVE_PROMOTION_REPAIR, TOPIC_DETAIL_REPAIR}:
        raise MeetingFieldRepairPlanningError(
            "unknown_meeting_repair_key",
            "The meeting field repair key has no registered validator.",
        )
    if set(result) != {"items"}:
        raise MeetingFieldRepairPlanningError(
            "repair_result_has_unauthorized_fields",
            "A meeting field repair result may only return its bounded items array.",
        )
    raw_items = result.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 3:
        raise MeetingFieldRepairPlanningError(
            "invalid_repair_result",
            "A bounded meeting repair must return between one and three items.",
        )
    validated_items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise MeetingFieldRepairPlanningError(
                "invalid_repair_result",
                "Every bounded meeting repair item must be an object.",
            )
        if plan.target.field == "actions":
            if set(raw_item) != {"task", "owner", "deadline", "evidence"}:
                raise MeetingFieldRepairPlanningError(
                    "repair_result_has_unauthorized_fields",
                    "A bounded action repair returned unauthorized fields.",
                )
            task = raw_item.get("task")
            if not isinstance(task, str) or not task.strip() or len(task) > 600:
                raise MeetingFieldRepairPlanningError(
                    "invalid_repair_result",
                    "A bounded action repair requires a non-empty task.",
                )
            if raw_item.get("owner") != "" or raw_item.get("deadline") != "":
                raise MeetingFieldRepairPlanningError(
                    "repair_result_invents_action_metadata",
                    "Quantitative promotion may not create owners or deadlines.",
                )
            evidence = _validated_result_evidence(
                raw_item.get("evidence"), packet_segment_ids
            )
            _require_numbers_grounded(task, evidence, plan.evidence_packet.segments)
            validated_items.append(
                {
                    "task": task.strip(),
                    "owner": "",
                    "deadline": "",
                    "evidence": evidence,
                }
            )
            continue
        if set(raw_item) != {"text", "evidence"}:
            raise MeetingFieldRepairPlanningError(
                "repair_result_has_unauthorized_fields",
                "A bounded evidence item returned unauthorized fields.",
            )
        text = raw_item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise MeetingFieldRepairPlanningError(
                "invalid_repair_result",
                "A bounded evidence item requires non-empty text.",
            )
        evidence = _validated_result_evidence(raw_item.get("evidence"), packet_segment_ids)
        _require_numbers_grounded(text, evidence, plan.evidence_packet.segments)
        validated_items.append({"text": text.strip(), "evidence": evidence})
    return {"items": validated_items}


def _validated_result_evidence(value: Any, packet_segment_ids: set[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 3
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise MeetingFieldRepairPlanningError(
            "invalid_repair_evidence",
            "Meeting repair evidence must contain one to three unique segment ids.",
        )
    if not set(value).issubset(packet_segment_ids):
        raise MeetingFieldRepairPlanningError(
            "repair_evidence_outside_packet",
            "A meeting repair result referenced evidence outside its bounded packet.",
        )
    return list(value)


def _require_numbers_grounded(
    text: str,
    evidence_ids: Sequence[str],
    packet_segments: Sequence[Mapping[str, Any]],
) -> None:
    output_numbers = {
        _normalized_number(match.group(0)) for match in _IMPORTANT_NUMBER.finditer(text)
    }
    if not output_numbers:
        return
    selected_evidence = "".join(
        str(segment["text"])
        for segment in packet_segments
        if segment["segment_id"] in evidence_ids
    )
    evidence_numbers = {
        _normalized_number(match.group(0))
        for match in _IMPORTANT_NUMBER.finditer(selected_evidence)
    }
    if not output_numbers.issubset(evidence_numbers):
        raise MeetingFieldRepairPlanningError(
            "repair_result_invents_quantitative_fact",
            "A meeting field repair introduced a number absent from its cited evidence.",
        )


def _normalized_number(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).replace(
        "—", "-"
    ).replace("–", "-")


def _merge_validated_result(
    document: dict[str, Any], plan: MeetingFieldRepairPlan, result: Mapping[str, Any]
) -> None:
    if plan.repair_key == SPEAKER_GROUNDING_REPAIR:
        summaries = document.get("speaker_summaries")
        if not isinstance(summaries, list):
            raise MeetingFieldRepairPlanningError(
                "repair_target_missing",
                "The speaker summary repair target is no longer available.",
            )
        matching = [
            item
            for item in summaries
            if isinstance(item, dict) and item.get("speaker_id") == plan.target.item_key
        ]
        if len(matching) != 1:
            raise MeetingFieldRepairPlanningError(
                "repair_target_not_unique",
                "The speaker summary repair target is no longer unique.",
            )
        matching[0]["summary"] = result["summary"]
        matching[0]["evidence"] = copy.deepcopy(result["evidence"])
        return

    items = document.get(plan.target.field)
    if not isinstance(items, list):
        raise MeetingFieldRepairPlanningError(
            "repair_target_missing",
            "The bounded meeting repair list target is no longer available.",
        )
    additions = copy.deepcopy(result["items"])
    if plan.target.field == "topics":
        index = plan.target.item_index
        if index is None or index < 0 or index >= len(items) or not isinstance(items[index], dict):
            raise MeetingFieldRepairPlanningError(
                "repair_target_missing",
                "The topic detail repair target is no longer available.",
            )
        details = items[index].get("details")
        if not isinstance(details, list) or len(details) + len(additions) > 6:
            raise MeetingFieldRepairPlanningError(
                "repair_result_field_limit_exceeded",
                "The topic detail repair exceeds the document field limit.",
            )
        details.extend(additions)
        return
    field_limit = 8 if plan.target.field == "highlights" else 10
    if len(items) + len(additions) > field_limit:
        raise MeetingFieldRepairPlanningError(
            "repair_result_field_limit_exceeded",
            "The bounded meeting repair exceeds the document field limit.",
        )
    items.extend(additions)
