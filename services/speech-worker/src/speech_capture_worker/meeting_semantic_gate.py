"""Deterministic semantic guards for external meeting quality edits.

The gate never creates content and never decides whether a proposal is true.  It
only rejects high-confidence regressions between an already evidence-validated
baseline and a quality-edited candidate.  Callers opt in through registered
ProfileBundle validators; the current builtin path remains unchanged.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SUMMARY_CONSISTENCY_VALIDATOR = "meeting.summary.outcomes_consistent"
RESULT_PRESERVATION_VALIDATOR = "meeting.quality.results_preserved"
QUANTITATIVE_PROMOTION_VALIDATOR = "meeting.quantitative_facts.promoted"
SPEAKER_ATTRIBUTION_VALIDATOR = "meeting.speaker.attribution_bounded"

MEETING_SEMANTIC_VALIDATORS = frozenset(
    {
        SUMMARY_CONSISTENCY_VALIDATOR,
        RESULT_PRESERVATION_VALIDATOR,
        QUANTITATIVE_PROMOTION_VALIDATOR,
        SPEAKER_ATTRIBUTION_VALIDATOR,
    }
)

_PRESERVED_FIELDS = ("highlights", "decisions", "actions", "risks", "open_questions")
_PRIMARY_FIELDS = (
    "objective",
    "summary",
    "context",
    "highlights",
    "topics",
    "discussion_threads",
    "decisions",
    "actions",
    "risks",
    "open_questions",
)
_DECISION_CLAIM = re.compile(
    r"(?:达成|形成|作出|做出)(?:了)?(?:多项|若干项|明确的?)?(?:决定|决议)"
)
_ASSIGNMENT_CLAIM = re.compile(
    r"(?:完成|明确|确定)(?:了)?(?:任务|工作|责任)(?:的)?(?:分配|安排)|"
    r"(?:任务|责任)(?:已|的)?(?:分配|安排)"
)
_DEADLINE_CLAIM = re.compile(
    r"(?:明确|确定)(?:了)?(?:完成|交付|截止)(?:日期|时间|期限)|明确时间节点"
)
_IMPORTANT_NUMBER = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[-–—至到]\s*\d+(?:\.\d+)?\s*"
    r"(?:%|年|个月|月|周|天|个|项|款|份|层|套))|"
    r"(?:\d+(?:\.\d+)?\s*(?:%|年|个月|月|周|天|个|项|款|份|层|套))"
)
_TIMELINE_IMPORTANCE = re.compile(
    r"规则|阈值|优先|冲突|范围|至少|最多|全部|覆盖|匹配|期限|周期|数据|交付"
)


@dataclass(frozen=True)
class MeetingSemanticGateResult:
    passed: bool
    checks: Mapping[str, bool]
    issue_codes: tuple[str, ...]


class MeetingSemanticGateError(ValueError):
    """Raised when an opted-in external meeting candidate regresses semantically."""

    def __init__(self, result: MeetingSemanticGateResult) -> None:
        self.result = result
        codes = ", ".join(result.issue_codes) or "unknown"
        super().__init__(f"Meeting semantic gate rejected the candidate: {codes}.")


def evaluate_meeting_semantic_edit(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]],
    validators: Sequence[str],
) -> MeetingSemanticGateResult:
    """Evaluate only registered, deterministic B3.1 guards."""

    enabled = frozenset(validators) & MEETING_SEMANTIC_VALIDATORS
    checks: dict[str, bool] = {}
    issues: list[str] = []

    if SUMMARY_CONSISTENCY_VALIDATOR in enabled:
        consistency_issues = _summary_consistency_issues(candidate)
        checks[SUMMARY_CONSISTENCY_VALIDATOR] = not consistency_issues
        issues.extend(consistency_issues)

    if RESULT_PRESERVATION_VALIDATOR in enabled:
        preservation_issues = _result_preservation_issues(baseline, candidate)
        checks[RESULT_PRESERVATION_VALIDATOR] = not preservation_issues
        issues.extend(preservation_issues)

    if QUANTITATIVE_PROMOTION_VALIDATOR in enabled:
        quantitative_issues = _quantitative_promotion_issues(baseline, candidate)
        checks[QUANTITATIVE_PROMOTION_VALIDATOR] = not quantitative_issues
        issues.extend(quantitative_issues)

    if SPEAKER_ATTRIBUTION_VALIDATOR in enabled:
        speaker_issues = _speaker_attribution_issues(baseline, candidate, segments=segments)
        checks[SPEAKER_ATTRIBUTION_VALIDATOR] = not speaker_issues
        issues.extend(speaker_issues)

    return MeetingSemanticGateResult(
        passed=all(checks.values()),
        checks=checks,
        issue_codes=tuple(dict.fromkeys(issues)),
    )


def require_meeting_semantic_edit(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]],
    validators: Sequence[str],
) -> Mapping[str, Any]:
    result = evaluate_meeting_semantic_edit(
        baseline,
        candidate,
        segments=segments,
        validators=validators,
    )
    if not result.passed:
        raise MeetingSemanticGateError(result)
    return candidate


def repair_meeting_semantic_edit(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]],
    validators: Sequence[str],
) -> dict[str, Any]:
    """Apply only lossless, deterministic repairs before the semantic gate.

    Evidence-backed baseline result items are copied verbatim when a quality edit
    drops them. Unsupported newly inferred role metadata is cleared, and summary
    sentences that contradict the final structured fields are removed. No new fact,
    evidence reference, owner, deadline, or decision is synthesized.
    """

    enabled = frozenset(validators) & MEETING_SEMANTIC_VALIDATORS
    repaired = copy.deepcopy(dict(candidate))
    if RESULT_PRESERVATION_VALIDATOR in enabled:
        for field in _PRESERVED_FIELDS:
            baseline_items = [
                item for item in _mapping_items(baseline.get(field)) if _evidence(item)
            ]
            candidate_items = [
                copy.deepcopy(dict(item)) for item in _mapping_items(repaired.get(field))
            ]
            for item in baseline_items:
                if not any(
                    _items_semantically_match(item, other, field=field)
                    for other in candidate_items
                ):
                    candidate_items.append(copy.deepcopy(dict(item)))
            if isinstance(baseline.get(field), list) or isinstance(repaired.get(field), list):
                repaired[field] = candidate_items

    if SPEAKER_ATTRIBUTION_VALIDATOR in enabled:
        _clear_unsupported_speaker_metadata(
            baseline,
            repaired,
            segments=segments,
        )

    if SUMMARY_CONSISTENCY_VALIDATOR in enabled:
        _remove_inconsistent_summary_sentences(baseline, repaired)
    return repaired


def _summary_consistency_issues(candidate: Mapping[str, Any]) -> list[str]:
    summary = _evidence_text(candidate.get("summary"))
    decisions = _mapping_items(candidate.get("decisions"))
    actions = _mapping_items(candidate.get("actions"))
    issues: list[str] = []
    if not decisions and _DECISION_CLAIM.search(summary):
        issues.append("summary_claims_missing_decisions")
    if not any(_normalized(item.get("owner")) for item in actions) and _ASSIGNMENT_CLAIM.search(
        summary
    ):
        issues.append("summary_claims_missing_assignments")
    if not any(_normalized(item.get("deadline")) for item in actions) and _DEADLINE_CLAIM.search(
        summary
    ):
        issues.append("summary_claims_missing_deadlines")
    return issues


def _result_preservation_issues(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[str]:
    issues: list[str] = []
    for field in _PRESERVED_FIELDS:
        baseline_items = [item for item in _mapping_items(baseline.get(field)) if _evidence(item)]
        candidate_items = _mapping_items(candidate.get(field))
        unmatched = [
            item
            for item in baseline_items
            if not any(
                _items_semantically_match(item, other, field=field)
                for other in candidate_items
            )
        ]
        if unmatched:
            issues.append(f"evidence_backed_{field}_removed")
    return issues


def _quantitative_promotion_issues(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> list[str]:
    required = _important_numbers({field: baseline.get(field) for field in _PRIMARY_FIELDS})
    for text in _strings(baseline.get("timeline_sections")):
        if _TIMELINE_IMPORTANCE.search(text):
            required.update(_IMPORTANT_NUMBER.findall(text))
    present = _important_numbers({field: candidate.get(field) for field in _PRIMARY_FIELDS})
    normalized_required = {_normalized_number(value) for value in required}
    normalized_present = {_normalized_number(value) for value in present}
    if normalized_required - normalized_present:
        return ["important_quantitative_facts_not_promoted"]
    return []


def _speaker_attribution_issues(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]],
) -> list[str]:
    segment_by_id = {
        str(segment.get("segment_id")): segment
        for segment in segments
        if isinstance(segment.get("segment_id"), str)
    }
    baseline_by_speaker = {
        str(item.get("speaker_id")): item
        for item in _mapping_items(baseline.get("speaker_summaries"))
        if isinstance(item.get("speaker_id"), str)
    }
    issues: list[str] = []
    summaries_seen: set[str] = set()
    for item in _mapping_items(candidate.get("speaker_summaries")):
        speaker_id = str(item.get("speaker_id") or "")
        evidence_ids = _evidence(item)
        if evidence_ids and not any(
            str(segment_by_id.get(segment_id, {}).get("speaker_id") or "") == speaker_id
            for segment_id in evidence_ids
        ):
            issues.append("speaker_summary_has_no_self_evidence")
        summary = _normalized(item.get("summary"))
        if summary and summary in summaries_seen:
            issues.append("speaker_summaries_are_duplicated")
        summaries_seen.add(summary)

        baseline_item = baseline_by_speaker.get(speaker_id, {})
        evidence_text = "".join(
            str(segment_by_id.get(segment_id, {}).get("text") or "")
            for segment_id in evidence_ids
        )
        normalized_evidence = _normalized(evidence_text)
        for field in ("role", "affiliation"):
            value = _normalized(item.get(field))
            if not value or value == _normalized(baseline_item.get(field)):
                continue
            if value not in normalized_evidence:
                issues.append(f"speaker_{field}_not_directly_supported")
        baseline_summary = _normalized(baseline_item.get("summary"))
        candidate_summary = _normalized(item.get("summary"))
        if candidate_summary and candidate_summary != baseline_summary:
            sentences = _sentences(str(item.get("summary") or ""))
            if any(not _sentence_is_grounded(sentence, evidence_text) for sentence in sentences):
                issues.append("speaker_summary_sentence_not_grounded")
    return issues


def _clear_unsupported_speaker_metadata(
    baseline: Mapping[str, Any],
    candidate: dict[str, Any],
    *,
    segments: Sequence[Mapping[str, Any]],
) -> None:
    segment_by_id = {
        str(segment.get("segment_id")): segment
        for segment in segments
        if isinstance(segment.get("segment_id"), str)
    }
    baseline_by_speaker = {
        str(item.get("speaker_id")): item
        for item in _mapping_items(baseline.get("speaker_summaries"))
        if isinstance(item.get("speaker_id"), str)
    }
    summaries = candidate.get("speaker_summaries")
    if not isinstance(summaries, list):
        return
    for raw_item in summaries:
        if not isinstance(raw_item, dict):
            continue
        speaker_id = str(raw_item.get("speaker_id") or "")
        baseline_item = baseline_by_speaker.get(speaker_id, {})
        evidence_text = "".join(
            str(segment_by_id.get(segment_id, {}).get("text") or "")
            for segment_id in _evidence(raw_item)
        )
        normalized_evidence = _normalized(evidence_text)
        for field in ("role", "affiliation"):
            value = _normalized(raw_item.get(field))
            if not value or value == _normalized(baseline_item.get(field)):
                continue
            if value not in normalized_evidence:
                raw_item[field] = ""
        baseline_summary = str(baseline_item.get("summary") or "").strip()
        candidate_summary = str(raw_item.get("summary") or "").strip()
        if not candidate_summary or _normalized(candidate_summary) == _normalized(baseline_summary):
            continue
        grounded_sentences = [
            sentence
            for sentence in _sentences(candidate_summary)
            if _sentence_is_grounded(sentence, evidence_text)
        ]
        if grounded_sentences:
            raw_item["summary"] = "".join(grounded_sentences)
        elif baseline_summary:
            raw_item["summary"] = baseline_summary


def _remove_inconsistent_summary_sentences(
    baseline: Mapping[str, Any], candidate: dict[str, Any]
) -> None:
    summary = candidate.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("text"), str):
        return
    decisions = _mapping_items(candidate.get("decisions"))
    actions = _mapping_items(candidate.get("actions"))
    has_assignments = any(_normalized(item.get("owner")) for item in actions)
    has_deadlines = any(_normalized(item.get("deadline")) for item in actions)
    sentences = re.findall(r"[^。！？!?]+[。！？!?]?", summary["text"])
    retained = []
    for sentence in sentences:
        if not decisions and _DECISION_CLAIM.search(sentence):
            continue
        if not has_assignments and _ASSIGNMENT_CLAIM.search(sentence):
            continue
        if not has_deadlines and _DEADLINE_CLAIM.search(sentence):
            continue
        retained.append(sentence)
    text = "".join(retained).strip()
    if text:
        summary["text"] = text
        return
    baseline_text = _evidence_text(baseline.get("summary")).strip()
    if baseline_text:
        summary["text"] = baseline_text


def _items_semantically_match(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, field: str
) -> bool:
    if not (_evidence(baseline) & _evidence(candidate)):
        return False
    baseline_text = _item_text(baseline, field=field)
    candidate_text = _item_text(candidate, field=field)
    if _text_similarity(baseline_text, candidate_text) < 0.18:
        return False
    if field == "actions":
        for metadata in ("owner", "deadline"):
            expected = _normalized(baseline.get(metadata))
            if expected and expected != _normalized(candidate.get(metadata)):
                return False
    return _important_numbers(baseline).issubset(_important_numbers(candidate))


def _item_text(item: Mapping[str, Any], *, field: str) -> str:
    preferred = "task" if field == "actions" else "text"
    value = item.get(preferred)
    if isinstance(value, str):
        return value
    return " ".join(_strings(item))


def _text_similarity(left: str, right: str) -> float:
    left_set = _bigrams(_normalized(left))
    right_set = _bigrams(_normalized(right))
    if not left_set or not right_set:
        return float(bool(left_set == right_set and left_set))
    return len(left_set & right_set) / len(left_set | right_set)


def _sentence_is_grounded(sentence: str, evidence_text: str) -> bool:
    sentence_bigrams = _bigrams(_normalized(sentence))
    evidence_bigrams = _bigrams(_normalized(evidence_text))
    if not sentence_bigrams or not evidence_bigrams:
        return False
    return len(sentence_bigrams & evidence_bigrams) / len(sentence_bigrams) >= 0.24


def _sentences(value: str) -> list[str]:
    return [item for item in re.findall(r"[^。！？!?]+[。！？!?]?", value) if item.strip()]


def _bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _important_numbers(value: Any) -> set[str]:
    return {
        _normalized_number(match.group(0))
        for text in _strings(value)
        for match in _IMPORTANT_NUMBER.finditer(text)
    }


def _normalized_number(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).replace("—", "-").replace(
        "–", "-"
    )


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _evidence(value: Mapping[str, Any]) -> set[str]:
    raw = value.get("evidence")
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def _evidence_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _normalized(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )
