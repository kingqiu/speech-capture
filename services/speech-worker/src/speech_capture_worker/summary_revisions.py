"""Private summary-candidate review and whole-version decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from speech_capture_worker.artifact_generation import (
    ARTIFACT_STAGE,
    MANUAL_SECTION_HEADING,
    NOTE_MARKDOWN,
    ArtifactGenerator,
    _read_manual_section,
)
from speech_capture_worker.corrections import CorrectionField, corrections_sha256
from speech_capture_worker.domain import JobRecord, JobState, validate_idempotency_key
from speech_capture_worker.errors import InvalidJobRequest, RevisionConflict
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.structuring_execution import (
    STRUCTURING_CHECKPOINT_KEY,
    STRUCTURING_STAGE,
    SUMMARY_REVISION_DECISION_STAGE,
    SUMMARY_REVISION_SCHEMA_VERSION,
    SUMMARY_REVISION_STAGE,
)


class SummaryRevisionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SummaryRevisionView:
    revision_key: str
    base_version: int
    candidate_version: int
    status: SummaryRevisionStatus
    changed: bool
    text_correction_count: int
    speaker_rename_count: int
    before_document: dict[str, Any] | None
    after_document: dict[str, Any] | None
    diff_truncated: bool
    created_at: str
    decided_at: str | None
    artifact_manifest_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SummaryRevisionCollection:
    revisions: tuple[SummaryRevisionView, ...]
    current_version: int
    manual_section_markdown: str
    can_regenerate: bool


@dataclass(frozen=True)
class SummaryRevisionDecisionResult:
    revision: SummaryRevisionView
    job: JobRecord
    applied: bool


@dataclass(frozen=True)
class SummaryRevisionRegenerationResult:
    revision: SummaryRevisionView
    job: JobRecord
    applied: bool


def list_summary_revisions(
    store: JobStore,
    job_id: str,
) -> SummaryRevisionCollection:
    revisions = store.list_checkpoints(job_id, stage=SUMMARY_REVISION_STAGE)
    decisions = {
        item.checkpoint_key: item
        for item in store.list_checkpoints(
            job_id,
            stage=SUMMARY_REVISION_DECISION_STAGE,
        )
    }
    current_version = 1
    views: list[SummaryRevisionView] = []
    for index, revision in enumerate(revisions):
        payload = revision.payload
        decision = decisions.get(revision.checkpoint_key)
        status = _decision_status(decision.payload if decision is not None else None)
        candidate_version = _positive_int(payload.get("candidate_version"), index + 2)
        view = SummaryRevisionView(
            revision_key=revision.checkpoint_key,
            base_version=current_version,
            candidate_version=candidate_version,
            status=status,
            changed=payload.get("changed") is True,
            text_correction_count=_nonnegative_int(payload.get("text_correction_count")),
            speaker_rename_count=_nonnegative_int(payload.get("speaker_rename_count")),
            before_document=_document(payload.get("before_document")),
            after_document=_document(payload.get("after_document")),
            diff_truncated=payload.get("diff_truncated") is True,
            created_at=revision.created_at,
            decided_at=(
                str(decision.payload.get("decided_at"))
                if decision is not None and isinstance(decision.payload.get("decided_at"), str)
                else None
            ),
            artifact_manifest_sha256=(
                str(decision.payload.get("artifact_manifest_sha256"))
                if decision is not None
                and isinstance(decision.payload.get("artifact_manifest_sha256"), str)
                else None
            ),
        )
        views.append(view)
        if status is SummaryRevisionStatus.ACCEPTED:
            current_version = candidate_version
    manual_section = _read_manual_section(
        store.get_job_stage_directory(job_id, stage=ARTIFACT_STAGE) / NOTE_MARKDOWN
    )
    relevant_corrections = [
        correction
        for correction in store.list_corrections(job_id)
        if correction.field
        in {
            CorrectionField.TRANSCRIPT_TEXT,
            CorrectionField.SEGMENT_REVIEW,
            CorrectionField.SPEAKER_DISPLAY_NAME,
        }
    ]
    latest_corrections_sha256 = (
        str(revisions[-1].payload.get("corrections_sha256")) if revisions else None
    )
    current_corrections_sha256 = corrections_sha256(relevant_corrections)
    return SummaryRevisionCollection(
        revisions=tuple(views),
        current_version=current_version,
        manual_section_markdown=(
            manual_section if manual_section is not None else f"{MANUAL_SECTION_HEADING}\n\n"
        ),
        can_regenerate=(
            bool(relevant_corrections) and latest_corrections_sha256 != current_corrections_sha256
        ),
    )


def regenerate_summary_revision(
    store: JobStore,
    job_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
    regenerate: Callable[[str], None],
) -> SummaryRevisionRegenerationResult:
    validate_idempotency_key(idempotency_key)
    job = store.get_job(job_id)
    if job.revision != expected_revision:
        raise RevisionConflict(
            "The job changed after the transcript review was loaded.",
            details={
                "job_id": job_id,
                "expected_revision": expected_revision,
                "current_revision": job.revision,
            },
        )
    if job.state not in {JobState.PROCESSED, JobState.PUBLISHED}:
        raise InvalidJobRequest("Summary regeneration requires a processed or published job.")
    collection = list_summary_revisions(store, job_id)
    pending = next(
        (
            item
            for item in reversed(collection.revisions)
            if item.status is SummaryRevisionStatus.PENDING
        ),
        None,
    )
    if pending is not None:
        return SummaryRevisionRegenerationResult(
            revision=pending,
            job=job,
            applied=False,
        )
    if not collection.can_regenerate:
        raise InvalidJobRequest(
            "The transcript has no new corrections that require note regeneration."
        )

    regenerate(job_id)
    updated = list_summary_revisions(store, job_id)
    pending = next(
        (
            item
            for item in reversed(updated.revisions)
            if item.status is SummaryRevisionStatus.PENDING
        ),
        None,
    )
    if pending is None:
        raise InvalidJobRequest("Note regeneration did not produce a reviewable candidate.")
    return SummaryRevisionRegenerationResult(
        revision=pending,
        job=store.get_job(job_id),
        applied=True,
    )


def decide_summary_revision(
    store: JobStore,
    job_id: str,
    *,
    revision_key: str,
    decision: SummaryRevisionStatus,
    expected_revision: int,
    idempotency_key: str,
) -> SummaryRevisionDecisionResult:
    if decision is SummaryRevisionStatus.PENDING:
        raise InvalidJobRequest("A summary revision decision must accept or reject the candidate.")
    validate_idempotency_key(idempotency_key)
    job = store.get_job(job_id)
    if job.revision != expected_revision:
        raise RevisionConflict(
            "The job changed after the summary comparison was loaded.",
            details={
                "job_id": job_id,
                "expected_revision": expected_revision,
                "current_revision": job.revision,
            },
        )
    if job.state not in {JobState.PROCESSED, JobState.PUBLISHED}:
        raise InvalidJobRequest("Summary decisions require a processed or published job.")

    revision = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage=SUMMARY_REVISION_STAGE),
        revision_key,
    )
    if revision is None:
        raise InvalidJobRequest("The requested summary revision does not exist.")
    prior_decision = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage=SUMMARY_REVISION_DECISION_STAGE),
        revision_key,
    )
    if prior_decision is not None:
        prior_status = _decision_status(prior_decision.payload)
        if prior_status is not decision:
            raise InvalidJobRequest("The summary revision already has a different decision.")
        return SummaryRevisionDecisionResult(
            revision=_view_by_key(store, job_id, revision_key),
            job=job,
            applied=False,
        )

    payload = revision.payload
    if payload.get("schema_version") != SUMMARY_REVISION_SCHEMA_VERSION:
        raise InvalidJobRequest("The summary revision is too old to decide in Obsidian.")
    before_checkpoint = _checkpoint_payload(payload.get("before_checkpoint"))
    after_checkpoint = _checkpoint_payload(payload.get("after_checkpoint"))
    current_checkpoint = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage=STRUCTURING_STAGE),
        STRUCTURING_CHECKPOINT_KEY,
    )
    if current_checkpoint is None:
        raise InvalidJobRequest("The current structured note evidence is unavailable.")
    current_sha = current_checkpoint.payload.get("raw_sha256")
    before_sha = before_checkpoint.get("raw_sha256")
    after_sha = after_checkpoint.get("raw_sha256")
    if current_sha not in {before_sha, after_sha}:
        raise RevisionConflict("The structured note changed after this candidate was generated.")

    manifest_sha256: str | None = None
    if decision is SummaryRevisionStatus.ACCEPTED:
        if current_sha != after_sha:
            store.put_checkpoint(
                job_id,
                stage=STRUCTURING_STAGE,
                checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
                payload=after_checkpoint,
            )
        artifact = ArtifactGenerator(store).generate(job_id, force=True)
        manifest_sha256 = artifact.manifest_sha256
    else:
        if current_sha != before_sha:
            store.put_checkpoint(
                job_id,
                stage=STRUCTURING_STAGE,
                checkpoint_key=STRUCTURING_CHECKPOINT_KEY,
                payload=before_checkpoint,
            )
        # Rejecting the Note candidate still keeps real transcript and speaker
        # corrections. Regenerate the package with the prior structured Note so
        # those accepted transcript-layer changes are not silently discarded.
        # Synthetic/legacy candidates without an append-only correction ledger
        # retain the historical no-regeneration behavior.
        relevant_corrections = [
            correction
            for correction in store.list_corrections(job_id)
            if correction.field
            in {
                CorrectionField.TRANSCRIPT_TEXT,
                CorrectionField.SEGMENT_REVIEW,
                CorrectionField.SPEAKER_DISPLAY_NAME,
            }
        ]
        if relevant_corrections:
            artifact = ArtifactGenerator(store).generate(job_id, force=True)
            manifest_sha256 = artifact.manifest_sha256

    store.put_checkpoint(
        job_id,
        stage=SUMMARY_REVISION_DECISION_STAGE,
        checkpoint_key=revision_key,
        payload={
            "schema_version": "1.0.0",
            "status": decision.value,
            "decided_at": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key,
            "artifact_manifest_sha256": manifest_sha256,
        },
    )
    return SummaryRevisionDecisionResult(
        revision=_view_by_key(store, job_id, revision_key),
        job=store.get_job(job_id),
        applied=True,
    )


def _view_by_key(store: JobStore, job_id: str, revision_key: str) -> SummaryRevisionView:
    for revision in list_summary_revisions(store, job_id).revisions:
        if revision.revision_key == revision_key:
            return revision
    raise InvalidJobRequest("The requested summary revision does not exist.")


def _checkpoint_by_key(checkpoints: list[Any], key: str) -> Any | None:
    return next((item for item in checkpoints if item.checkpoint_key == key), None)


def _decision_status(payload: dict[str, Any] | None) -> SummaryRevisionStatus:
    if payload is None:
        return SummaryRevisionStatus.PENDING
    try:
        return SummaryRevisionStatus(payload.get("status"))
    except ValueError as exc:
        raise InvalidJobRequest("The stored summary revision decision is invalid.") from exc


def _checkpoint_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("raw_sha256"), str):
        raise InvalidJobRequest("The summary revision is missing structured note evidence.")
    return value


def _document(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _positive_int(value: Any, fallback: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback
    )
