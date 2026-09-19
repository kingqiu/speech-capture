"""Read-only trusted capability for validating complete meeting documents.

The adapter in this module deliberately knows nothing about jobs, checkpoints,
publication, Vaults, filesystems, or transports.  It binds one immutable evidence
snapshot to the Worker's existing pure document validator and only accepts an
already-normalized document.  Validation may reject a document, but it may never
silently rewrite one.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class MeetingInvariantValidatorError(ValueError):
    """Raised when a meeting document or its bound evidence fails closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class MeetingInvariantEvidenceSnapshot:
    """Immutable evidence required by the Worker's meeting invariant validator."""

    segment_ids: tuple[str, ...]
    speaker_ids: tuple[str, ...]
    segment_texts: Mapping[str, str]
    segment_speakers: Mapping[str, str | None]
    segment_starts: Mapping[str, int]
    snapshot_sha256: str

    @classmethod
    def from_segments(
        cls,
        segments: Sequence[Mapping[str, Any]],
    ) -> MeetingInvariantEvidenceSnapshot:
        if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_evidence_invalid",
                "Meeting invariant evidence must be a segment sequence.",
            )
        texts: dict[str, str] = {}
        speakers: dict[str, str | None] = {}
        starts: dict[str, int] = {}
        for segment in segments:
            if not isinstance(segment, Mapping):
                raise MeetingInvariantValidatorError(
                    "meeting_invariant_evidence_invalid",
                    "Meeting invariant evidence contains a non-object segment.",
                )
            segment_id = segment.get("segment_id")
            speaker_id = segment.get("speaker_id")
            text = segment.get("text")
            start_ms = segment.get("start_ms")
            if (
                not isinstance(segment_id, str)
                or not segment_id
                or segment_id in texts
                or not isinstance(text, str)
                or not text.strip()
                or "\x00" in text
                or (speaker_id is not None and (not isinstance(speaker_id, str) or not speaker_id))
                or isinstance(start_ms, bool)
                or not isinstance(start_ms, int)
                or start_ms < 0
            ):
                raise MeetingInvariantValidatorError(
                    "meeting_invariant_evidence_invalid",
                    "Meeting invariant evidence contains an invalid segment.",
                )
            texts[segment_id] = text
            speakers[segment_id] = speaker_id
            starts[segment_id] = start_ms
        if not texts:
            raise MeetingInvariantValidatorError(
                "meeting_invariant_evidence_empty",
                "Meeting invariant evidence cannot be empty.",
            )
        if len(set(starts.values())) != len(starts):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_evidence_order_invalid",
                "Meeting invariant evidence start times must be unique.",
            )
        ordered_ids = tuple(sorted(texts, key=starts.__getitem__))
        speaker_ids = tuple(
            sorted({speaker_id for speaker_id in speakers.values() if speaker_id is not None})
        )
        snapshot_payload = [
            {
                "segment_id": segment_id,
                "speaker_id": speakers[segment_id],
                "text": texts[segment_id],
                "start_ms": starts[segment_id],
            }
            for segment_id in ordered_ids
        ]
        return cls(
            segment_ids=ordered_ids,
            speaker_ids=speaker_ids,
            segment_texts=MappingProxyType(dict(texts)),
            segment_speakers=MappingProxyType(dict(speakers)),
            segment_starts=MappingProxyType(dict(starts)),
            snapshot_sha256=_canonical_json_sha256(snapshot_payload),
        )

    def matches_segments(self, segments: Sequence[Mapping[str, Any]]) -> bool:
        """Return whether another segment sequence is exactly this evidence snapshot."""

        try:
            other = type(self).from_segments(segments)
        except MeetingInvariantValidatorError:
            return False
        return other.snapshot_sha256 == self.snapshot_sha256


_TRUSTED_FACTORY_TOKEN = object()


class TrustedMeetingInvariantValidator:
    """Sealed read-only meeting validation capability.

    Instances are created only by the Worker adapter in ``structuring_execution``.
    The bound validator is never exposed and is invoked with detached inputs.
    """

    __slots__ = ("_snapshot", "_validator")

    def __init__(
        self,
        *,
        token: object,
        snapshot: MeetingInvariantEvidenceSnapshot,
        validator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        if token is not _TRUSTED_FACTORY_TOKEN or not callable(validator):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_capability_untrusted",
                "The meeting invariant capability was not created by the Worker adapter.",
            )
        self._snapshot = snapshot
        self._validator = validator

    @property
    def evidence_snapshot_sha256(self) -> str:
        """Content-free audit identity for the bound evidence snapshot."""

        return self._snapshot.snapshot_sha256

    def matches_segments(self, segments: Sequence[Mapping[str, Any]]) -> bool:
        return self._snapshot.matches_segments(segments)

    def __call__(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(document, Mapping):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_document_invalid",
                "The meeting invariant validator requires a document object.",
            )
        source = copy.deepcopy(dict(document))
        source_sha256 = _canonical_json_sha256(source)
        chapters = source.get("chapters")
        if not isinstance(chapters, list):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_document_not_normalized",
                "The trusted meeting validator requires a complete normalized document.",
            )
        raw = copy.deepcopy(source)
        raw.pop("chapters", None)
        try:
            validated = self._validator(raw)
        except Exception as error:
            raise MeetingInvariantValidatorError(
                "meeting_invariant_validation_failed",
                "The Worker meeting invariant validator rejected the document.",
            ) from error
        if not isinstance(validated, Mapping):
            raise MeetingInvariantValidatorError(
                "meeting_invariant_validator_invalid",
                "The Worker meeting invariant validator returned a non-object result.",
            )
        if _canonical_json_sha256(document) != source_sha256:
            raise MeetingInvariantValidatorError(
                "meeting_invariant_source_mutated",
                "The meeting document changed while it was being validated.",
            )
        if _canonical_json_sha256(validated) != source_sha256:
            raise MeetingInvariantValidatorError(
                "meeting_invariant_document_not_idempotent",
                "The meeting document requires normalization and was rejected without rewriting.",
            )
        return copy.deepcopy(source)


def _create_trusted_meeting_invariant_validator(
    *,
    snapshot: MeetingInvariantEvidenceSnapshot,
    validator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> TrustedMeetingInvariantValidator:
    """Internal constructor used by the Worker's pure structuring adapter."""

    if not isinstance(snapshot, MeetingInvariantEvidenceSnapshot):
        raise MeetingInvariantValidatorError(
            "meeting_invariant_evidence_invalid",
            "The meeting invariant capability requires an immutable evidence snapshot.",
        )
    return TrustedMeetingInvariantValidator(
        token=_TRUSTED_FACTORY_TOKEN,
        snapshot=snapshot,
        validator=validator,
    )
