"""Private summary-candidate review and whole-version decisions."""

from __future__ import annotations

import hashlib
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


SUMMARY_REVISION_DRAFT_STAGE = "summary_revision_drafts"
SUMMARY_REVISION_DRAFT_SCHEMA_VERSION = "1.0.0"
MAX_SUMMARY_DRAFT_CHARACTERS = 2_000_000


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
    draft_markdown: str | None
    draft_version: int
    draft_updated_at: str | None
    draft_sha256: str | None

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


@dataclass(frozen=True)
class SummaryRevisionDraftResult:
    revision: SummaryRevisionView
    job: JobRecord
    saved: bool


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
        draft = _latest_draft(store, job_id, revision.checkpoint_key)
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
            draft_markdown=(
                str(draft.payload.get("markdown"))
                if draft is not None and isinstance(draft.payload.get("markdown"), str)
                else None
            ),
            draft_version=(
                _positive_int(draft.payload.get("draft_version"), 1)
                if draft is not None
                else 0
            ),
            draft_updated_at=(draft.updated_at if draft is not None else None),
            draft_sha256=(
                str(draft.payload.get("markdown_sha256"))
                if draft is not None
                and isinstance(draft.payload.get("markdown_sha256"), str)
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
        draft = _latest_draft(store, job_id, revision_key)
        draft_provenance = (
            {
                "source": "human_draft",
                "summary_revision_key": revision_key,
                "draft_version": _positive_int(draft.payload.get("draft_version"), 1),
                "markdown_sha256": draft.payload.get("markdown_sha256"),
                "updated_at": draft.updated_at,
            }
            if draft is not None
            else None
        )
        generator = ArtifactGenerator(store)
        artifact = (
            generator.generate(
                job_id,
                force=True,
                note_body_override=str(draft.payload["markdown"]),
                note_revision_provenance=draft_provenance,
            )
            if draft is not None and isinstance(draft.payload.get("markdown"), str)
            else generator.generate(job_id, force=True)
        )
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
            "draft_sha256": (
                draft_provenance.get("markdown_sha256")
                if decision is SummaryRevisionStatus.ACCEPTED
                and draft_provenance is not None
                else None
            ),
        },
    )
    return SummaryRevisionDecisionResult(
        revision=_view_by_key(store, job_id, revision_key),
        job=store.get_job(job_id),
        applied=True,
    )


def save_summary_revision_draft(
    store: JobStore,
    job_id: str,
    *,
    revision_key: str,
    markdown: str,
    expected_revision: int,
    expected_draft_version: int,
    idempotency_key: str,
) -> SummaryRevisionDraftResult:
    validate_idempotency_key(idempotency_key)
    job = store.get_job(job_id)
    if job.state not in {JobState.PROCESSED, JobState.PUBLISHED}:
        raise InvalidJobRequest("Candidate Note editing requires a processed or published job.")
    revisions = store.list_checkpoints(job_id, stage=SUMMARY_REVISION_STAGE)
    revision = _checkpoint_by_key(revisions, revision_key)
    if revision is None:
        raise InvalidJobRequest("The requested summary revision does not exist.")
    prior_decision = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage=SUMMARY_REVISION_DECISION_STAGE),
        revision_key,
    )
    prior_status = _decision_status(
        prior_decision.payload if prior_decision is not None else None
    )
    if prior_status is SummaryRevisionStatus.REJECTED:
        raise InvalidJobRequest("A rejected summary revision can no longer be edited.")
    normalized = markdown.strip()
    if not normalized:
        raise InvalidJobRequest("The candidate Note cannot be empty.")
    if len(normalized) > MAX_SUMMARY_DRAFT_CHARACTERS:
        raise InvalidJobRequest("The candidate Note is too large to save safely.")
    if any(
        line.strip() == MANUAL_SECTION_HEADING
        for line in normalized.splitlines()
    ):
        raise InvalidJobRequest(
            "Edit the protected manual section in the current Note, not in the candidate body."
        )
    if prior_status is SummaryRevisionStatus.ACCEPTED:
        published = store.get_publication_receipt(job_id)
        accepted_manifest = prior_decision.payload.get("artifact_manifest_sha256")
        if (
            published is not None
            and isinstance(accepted_manifest, str)
            and published.manifest_sha256 == accepted_manifest
        ):
            return _fork_published_summary_revision_draft(
                store,
                job_id,
                source_revision=revision,
                source_decision=prior_decision,
                markdown=normalized,
                expected_revision=expected_revision,
                expected_draft_version=expected_draft_version,
                idempotency_key=idempotency_key,
            )
    if job.revision != expected_revision:
        raise RevisionConflict(
            "The job changed after the candidate Note was loaded.",
            details={
                "job_id": job_id,
                "expected_revision": expected_revision,
                "current_revision": job.revision,
            },
        )
    latest = _latest_draft(store, job_id, revision_key)
    current_version = (
        _positive_int(latest.payload.get("draft_version"), 1) if latest is not None else 0
    )
    if current_version != expected_draft_version:
        raise RevisionConflict(
            "The candidate Note draft changed after it was loaded.",
            details={
                "expected_draft_version": expected_draft_version,
                "current_draft_version": current_version,
            },
        )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if latest is not None and latest.payload.get("markdown_sha256") == digest:
        return SummaryRevisionDraftResult(
            revision=_view_by_key(store, job_id, revision_key),
            job=job,
            saved=False,
        )
    draft_version = current_version + 1
    store.put_checkpoint(
        job_id,
        stage=SUMMARY_REVISION_DRAFT_STAGE,
        checkpoint_key=f"{revision_key}_draft_{draft_version:08d}",
        payload={
            "schema_version": SUMMARY_REVISION_DRAFT_SCHEMA_VERSION,
            "summary_revision_key": revision_key,
            "draft_version": draft_version,
            "markdown": normalized,
            "markdown_sha256": digest,
            "previous_markdown_sha256": (
                latest.payload.get("markdown_sha256") if latest is not None else None
            ),
            "idempotency_key": idempotency_key,
            "saved_at": datetime.now(UTC).isoformat(),
        },
    )
    if prior_status is SummaryRevisionStatus.ACCEPTED:
        draft_provenance = {
            "source": "human_draft",
            "summary_revision_key": revision_key,
            "draft_version": draft_version,
            "markdown_sha256": digest,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        artifact = ArtifactGenerator(store).generate(
            job_id,
            force=True,
            note_body_override=normalized,
            note_revision_provenance=draft_provenance,
        )
        decision_payload = dict(prior_decision.payload)
        decision_payload.update(
            {
                "artifact_manifest_sha256": artifact.manifest_sha256,
                "draft_sha256": digest,
                "amended_at": datetime.now(UTC).isoformat(),
            }
        )
        store.put_checkpoint(
            job_id,
            stage=SUMMARY_REVISION_DECISION_STAGE,
            checkpoint_key=revision_key,
            payload=decision_payload,
        )
        job = store.get_job(job_id)
    return SummaryRevisionDraftResult(
        revision=_view_by_key(store, job_id, revision_key),
        job=job,
        saved=True,
    )


def _fork_published_summary_revision_draft(
    store: JobStore,
    job_id: str,
    *,
    source_revision: Any,
    source_decision: Any,
    markdown: str,
    expected_revision: int,
    expected_draft_version: int,
    idempotency_key: str,
) -> SummaryRevisionDraftResult:
    """Create the next immutable Note version when editing a published revision."""

    revisions = store.list_checkpoints(job_id, stage=SUMMARY_REVISION_STAGE)
    target = next(
        (
            item
            for item in revisions
            if item.payload.get("source") == "published_human_amendment"
            and item.payload.get("source_revision_key") == source_revision.checkpoint_key
            and item.payload.get("manual_edit_idempotency_key") == idempotency_key
        ),
        None,
    )
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if target is None:
        job = store.get_job(job_id)
        if job.revision != expected_revision:
            raise RevisionConflict(
                "The job changed after the published Note was loaded.",
                details={
                    "job_id": job_id,
                    "expected_revision": expected_revision,
                    "current_revision": job.revision,
                },
            )
        source_draft = _latest_draft(store, job_id, source_revision.checkpoint_key)
        source_draft_version = (
            _positive_int(source_draft.payload.get("draft_version"), 1)
            if source_draft is not None
            else 0
        )
        if source_draft_version != expected_draft_version:
            raise RevisionConflict(
                "The published Note draft changed after it was loaded.",
                details={
                    "expected_draft_version": expected_draft_version,
                    "current_draft_version": source_draft_version,
                },
            )
        candidate_version = max(
            (
                _positive_int(item.payload.get("candidate_version"), index + 2)
                for index, item in enumerate(revisions)
            ),
            default=1,
        ) + 1
        target_key = (
            f"revision_manual_{candidate_version:08d}_"
            f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:12]}"
        )
        source_payload = source_revision.payload
        current_document = _document(source_payload.get("after_document"))
        current_checkpoint = source_payload.get("after_checkpoint")
        current_sha256 = source_payload.get("after_sha256")
        target, _ = store.put_checkpoint(
            job_id,
            stage=SUMMARY_REVISION_STAGE,
            checkpoint_key=target_key,
            payload={
                "schema_version": SUMMARY_REVISION_SCHEMA_VERSION,
                "structuring_generation": source_payload.get("structuring_generation"),
                "candidate_version": candidate_version,
                "corrections_sha256": source_payload.get("corrections_sha256"),
                "text_correction_count": 0,
                "speaker_rename_count": 0,
                "before_sha256": current_sha256,
                "after_sha256": current_sha256,
                "before_document": current_document,
                "after_document": current_document,
                "before_checkpoint": current_checkpoint,
                "after_checkpoint": current_checkpoint,
                "changed": True,
                "diff": "",
                "diff_truncated": False,
                "source": "published_human_amendment",
                "source_revision_key": source_revision.checkpoint_key,
                "source_manifest_sha256": source_decision.payload.get(
                    "artifact_manifest_sha256"
                ),
                "manual_edit_idempotency_key": idempotency_key,
            },
        )

    target_key = target.checkpoint_key
    latest = _latest_draft(store, job_id, target_key)
    if latest is not None:
        if latest.payload.get("markdown_sha256") != digest:
            raise RevisionConflict(
                "The manual Note version already exists with different content."
            )
    else:
        source_draft = _latest_draft(store, job_id, source_revision.checkpoint_key)
        store.put_checkpoint(
            job_id,
            stage=SUMMARY_REVISION_DRAFT_STAGE,
            checkpoint_key=f"{target_key}_draft_{1:08d}",
            payload={
                "schema_version": SUMMARY_REVISION_DRAFT_SCHEMA_VERSION,
                "summary_revision_key": target_key,
                "draft_version": 1,
                "markdown": markdown,
                "markdown_sha256": digest,
                "previous_markdown_sha256": (
                    source_draft.payload.get("markdown_sha256")
                    if source_draft is not None
                    else source_decision.payload.get("draft_sha256")
                ),
                "idempotency_key": idempotency_key,
                "saved_at": datetime.now(UTC).isoformat(),
            },
        )

    prior_target_decision = _checkpoint_by_key(
        store.list_checkpoints(job_id, stage=SUMMARY_REVISION_DECISION_STAGE),
        target_key,
    )
    if (
        prior_target_decision is not None
        and _decision_status(prior_target_decision.payload)
        is SummaryRevisionStatus.ACCEPTED
    ):
        return SummaryRevisionDraftResult(
            revision=_view_by_key(store, job_id, target_key),
            job=store.get_job(job_id),
            saved=False,
        )

    draft_provenance = {
        "source": "human_draft",
        "summary_revision_key": target_key,
        "draft_version": 1,
        "markdown_sha256": digest,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    artifact = ArtifactGenerator(store).generate(
        job_id,
        force=True,
        note_body_override=markdown,
        note_revision_provenance=draft_provenance,
    )
    store.put_checkpoint(
        job_id,
        stage=SUMMARY_REVISION_DECISION_STAGE,
        checkpoint_key=target_key,
        payload={
            "schema_version": "1.0.0",
            "status": SummaryRevisionStatus.ACCEPTED.value,
            "decided_at": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key,
            "artifact_manifest_sha256": artifact.manifest_sha256,
            "draft_sha256": digest,
            "source_revision_key": source_revision.checkpoint_key,
        },
    )
    return SummaryRevisionDraftResult(
        revision=_view_by_key(store, job_id, target_key),
        job=store.get_job(job_id),
        saved=True,
    )


def _view_by_key(store: JobStore, job_id: str, revision_key: str) -> SummaryRevisionView:
    for revision in list_summary_revisions(store, job_id).revisions:
        if revision.revision_key == revision_key:
            return revision
    raise InvalidJobRequest("The requested summary revision does not exist.")


def _latest_draft(store: JobStore, job_id: str, revision_key: str) -> Any | None:
    prefix = f"{revision_key}_draft_"
    candidates = [
        item
        for item in store.list_checkpoints(job_id, stage=SUMMARY_REVISION_DRAFT_STAGE)
        if item.checkpoint_key.startswith(prefix)
        and item.payload.get("summary_revision_key") == revision_key
    ]
    return max(
        candidates,
        key=lambda item: _positive_int(item.payload.get("draft_version"), 1),
        default=None,
    )


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
