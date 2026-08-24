"""Continuous restart-safe execution of queued local Worker jobs."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from speech_capture_worker.alignment import (
    CHECKPOINT_KEY as ALIGNMENT_CHECKPOINT_KEY,
)
from speech_capture_worker.alignment import (
    CHECKPOINT_STAGE as ALIGNMENT_STAGE,
)
from speech_capture_worker.alignment import TranscriptAlignmentFinalizer
from speech_capture_worker.artifact_generation import ArtifactGenerator
from speech_capture_worker.asr_execution import AsrChunkExecutor, MlxQwenAsrEngine
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.bounded_gap_resolution import (
    MAX_AUTOMATED_ALIGNMENT_GENERATIONS,
    BoundedGapMaterializer,
)
from speech_capture_worker.diarization_execution import (
    DIARIZATION_MODEL_REVISION,
    PyannoteSpeakerDiarizationEngine,
    SpeakerDiarizationExecutor,
)
from speech_capture_worker.domain import JobRecord, JobState, ModelProfile
from speech_capture_worker.gap_analysis import (
    DefiniteSilenceMaterializer,
    TranscriptGapAnalyzer,
)
from speech_capture_worker.gap_retranscription import GapRetranscriptionExecutor
from speech_capture_worker.gap_speech_activity import (
    GapSpeechActivityAnalyzer,
    GapSpeechActivityOutcome,
    PyannoteVoiceActivityDetector,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.model_activation import resolve_active_model_target
from speech_capture_worker.natural_pause import NaturalPauseMaterializer
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome
from speech_capture_worker.structuring_execution import (
    OllamaStructuringEngine,
    StructuringExecutor,
)

LOGGER = logging.getLogger(__name__)
VAD_MODEL_REVISION = "660b9e20307a2b0cdb400d0f80aadc04a701fc54"
BACKGROUND_STAGE = "background_processing"
AWAITING_GAP_REVIEW_KEY = "awaiting_gap_review"


class BackgroundStepOutcome(StrEnum):
    IDLE = "idle"
    ADVANCED = "advanced"
    WAITING = "waiting"
    FAILED = "failed"


AdvanceJob = Callable[[JobRecord], BackgroundStepOutcome]


class ContinuousJobExecutor:
    """Advance at most one durable job stage on each call."""

    def __init__(
        self,
        store: JobStore,
        *,
        data_dir: Path,
        advance_job: AdvanceJob | None = None,
    ) -> None:
        self.store = store
        self.data_dir = data_dir.resolve()
        self._advance_job_override = advance_job

    def run_once(self) -> BackgroundStepOutcome:
        job = self.store.get_active_processing_job()
        if job is None:
            scheduled = JobScheduler(self.store).run_once()
            if scheduled.outcome in {
                SchedulerOutcome.IDLE,
                SchedulerOutcome.BLOCKED,
            }:
                return BackgroundStepOutcome.IDLE
            if scheduled.outcome is SchedulerOutcome.BUSY:
                job = self.store.get_active_processing_job()
            else:
                job = scheduled.job
        if job is None:
            return BackgroundStepOutcome.IDLE
        try:
            if self._advance_job_override is not None:
                return self._advance_job_override(job)
            return self._advance_job(job)
        except Exception as exc:
            LOGGER.exception("The background Worker stage failed.")
            recorded = self._fail_active_job(job, exception_type=type(exc).__name__)
            return (
                BackgroundStepOutcome.FAILED
                if recorded
                else BackgroundStepOutcome.ADVANCED
            )

    def _advance_job(self, job: JobRecord) -> BackgroundStepOutcome:
        if job.state is JobState.PREPROCESSING:
            AudioPreprocessor(self.store).prepare(job.job_id)
            self._run_asr(self.store.get_job(job.job_id))
            return BackgroundStepOutcome.ADVANCED
        if job.state is JobState.TRANSCRIBING:
            self._run_asr(job)
            return BackgroundStepOutcome.ADVANCED
        if job.state is JobState.ALIGNING:
            return self._advance_alignment(job)
        if job.state is JobState.DIARIZING:
            SpeakerDiarizationExecutor(
                self.store,
                PyannoteSpeakerDiarizationEngine(
                    model_revision=DIARIZATION_MODEL_REVISION,
                    cache_dir=self.data_dir / "models" / "pyannote",
                ),
            ).run(job.job_id)
            return BackgroundStepOutcome.ADVANCED
        if job.state is JobState.STRUCTURING:
            profile = job.model_profile.value
            StructuringExecutor(
                self.store,
                OllamaStructuringEngine(
                    model=resolve_active_model_target(
                        self.data_dir,
                        profile=profile,
                        key="ollama_accuracy" if profile == "accuracy" else "ollama_editor",
                        fallback="qwen3:14b" if profile == "accuracy" else "qwen3:8b",
                    ),
                    editor_model=resolve_active_model_target(
                        self.data_dir,
                        profile=profile,
                        key="ollama_editor",
                        fallback="qwen3:8b",
                    ),
                ),
            ).run(job.job_id)
            return BackgroundStepOutcome.ADVANCED
        if job.state is JobState.QUALITY_CHECK:
            ArtifactGenerator(self.store).generate(job.job_id)
            return BackgroundStepOutcome.ADVANCED
        return BackgroundStepOutcome.WAITING

    def _advance_alignment(self, job: JobRecord) -> BackgroundStepOutcome:
        finalizer = TranscriptAlignmentFinalizer(self.store)
        first = finalizer.finalize(job.job_id)
        if first.job.state is JobState.DIARIZING:
            return BackgroundStepOutcome.ADVANCED
        if self._is_waiting_for_same_gap_review(job.job_id):
            return BackgroundStepOutcome.WAITING

        TranscriptGapAnalyzer(self.store).analyze(job.job_id)
        silence = DefiniteSilenceMaterializer(self.store).materialize(job.job_id)
        if silence.alignment.job.state is JobState.DIARIZING:
            return BackgroundStepOutcome.ADVANCED

        current_alignment = self._alignment_checkpoint(job.job_id)
        if (
            current_alignment is not None
            and current_alignment.generation
            >= MAX_AUTOMATED_ALIGNMENT_GENERATIONS
        ):
            bounded = BoundedGapMaterializer(self.store).materialize(job.job_id)
            if bounded.alignment.job.state is JobState.DIARIZING:
                return BackgroundStepOutcome.ADVANCED
            current = self.store.get_job(job.job_id)
            if current.state is JobState.ALIGNING:
                self.store.transition_job(
                    job.job_id,
                    JobState.WAITING_USER,
                    expected_revision=current.revision,
                    reason_code="bounded_gap_resolution_requires_review",
                    error_code="ALIGNMENT_REVIEW_REQUIRED",
                    error_message=(
                        "Automatic gap repair reached its safe limit; preserved "
                        "evidence requires review."
                    ),
                    event_type="job.waiting_for_alignment_review",
                )
            return BackgroundStepOutcome.ADVANCED

        detector = PyannoteVoiceActivityDetector(
            model_revision=VAD_MODEL_REVISION,
            cache_dir=self.data_dir / "models" / "pyannote",
        )
        speech_activity = GapSpeechActivityAnalyzer(self.store, detector).analyze(job.job_id)
        if speech_activity.outcome is GapSpeechActivityOutcome.SAFE_PAUSED:
            current = self.store.get_job(job.job_id)
            if current.state is JobState.ALIGNING:
                self.store.transition_job(
                    job.job_id,
                    JobState.PAUSED,
                    expected_revision=current.revision,
                    reason_code="gap_speech_activity_resource_blocked",
                    error_code="SPEECH_ACTIVITY_RESOURCE_BLOCKED",
                    error_message=(
                        "Worker resources must recover before speech-activity analysis can start."
                    ),
                    event_type="resource.safe_paused",
                )
            return BackgroundStepOutcome.ADVANCED
        retranscribed = GapRetranscriptionExecutor(
            self.store,
            self._asr_engine(job.model_profile),
        ).run(job.job_id)
        if (
            retranscribed.alignment is not None
            and retranscribed.alignment.job.state is JobState.DIARIZING
        ):
            return BackgroundStepOutcome.ADVANCED
        if retranscribed.added_segment_count:
            return BackgroundStepOutcome.ADVANCED

        pauses = NaturalPauseMaterializer(self.store).materialize(job.job_id)
        if pauses.alignment.job.state is JobState.DIARIZING:
            return BackgroundStepOutcome.ADVANCED
        if pauses.created_segment_count:
            return BackgroundStepOutcome.ADVANCED

        current_alignment = self._alignment_checkpoint(job.job_id)
        if current_alignment is not None:
            self.store.put_checkpoint(
                job.job_id,
                stage=BACKGROUND_STAGE,
                checkpoint_key=AWAITING_GAP_REVIEW_KEY,
                payload={
                    "alignment_generation": current_alignment.generation,
                    "alignment_sha256": current_alignment.payload_sha256,
                    "unresolved_ranges": current_alignment.payload.get(
                        "unresolved_ranges", []
                    ),
                },
            )
        return BackgroundStepOutcome.WAITING

    def _is_waiting_for_same_gap_review(self, job_id: str) -> bool:
        alignment = self._alignment_checkpoint(job_id)
        if alignment is None:
            return False
        waiting = next(
            (
                item
                for item in self.store.list_checkpoints(job_id, stage=BACKGROUND_STAGE)
                if item.checkpoint_key == AWAITING_GAP_REVIEW_KEY
            ),
            None,
        )
        return waiting is not None and (
            waiting.payload.get("alignment_generation") == alignment.generation
            and waiting.payload.get("alignment_sha256") == alignment.payload_sha256
        )

    def _alignment_checkpoint(self, job_id: str):
        return next(
            (
                item
                for item in self.store.list_checkpoints(job_id, stage=ALIGNMENT_STAGE)
                if item.checkpoint_key == ALIGNMENT_CHECKPOINT_KEY
            ),
            None,
        )

    def _asr_engine(self, profile: ModelProfile) -> MlxQwenAsrEngine:
        accuracy = profile is ModelProfile.ACCURACY
        return MlxQwenAsrEngine(
            model_profile=profile,
            model_target=resolve_active_model_target(
                self.data_dir,
                profile=profile.value,
                key="asr_accuracy" if accuracy else "asr_speed",
                fallback=(
                    "Qwen/Qwen3-ASR-1.7B"
                    if accuracy
                    else "Qwen/Qwen3-ASR-0.6B"
                ),
            ),
        )

    def _run_asr(self, job: JobRecord) -> None:
        AsrChunkExecutor(
            self.store,
            self._asr_engine(job.model_profile),
        ).run_all(job.job_id)

    def _fail_active_job(self, observed: JobRecord, *, exception_type: str) -> bool:
        try:
            current = self.store.get_job(observed.job_id)
            if (
                current.revision != observed.revision
                or current.state is not observed.state
            ):
                LOGGER.info(
                    "Ignoring a stale background-stage failure because the job advanced."
                )
                return False
            if current.state not in {
                JobState.PREPROCESSING,
                JobState.TRANSCRIBING,
                JobState.ALIGNING,
                JobState.DIARIZING,
                JobState.STRUCTURING,
                JobState.QUALITY_CHECK,
            }:
                return False
            self.store.put_checkpoint(
                observed.job_id,
                stage=BACKGROUND_STAGE,
                checkpoint_key=f"failure_{current.revision:08d}",
                payload={"exception_type": exception_type, "state": current.state.value},
            )
            self.store.transition_job(
                observed.job_id,
                JobState.FAILED,
                expected_revision=current.revision,
                reason_code="background_stage_failed",
                error_code="BACKGROUND_STAGE_FAILED",
                error_message=(
                    "The local Worker could not complete the current processing stage."
                ),
                event_type="job.background_stage_failed",
            )
            return True
        except Exception:
            LOGGER.exception("The failed background stage could not be recorded.")
            return False


class BackgroundProcessingService:
    """Own one daemon thread that continuously advances durable jobs."""

    def __init__(self, data_dir: Path, *, poll_interval_seconds: float = 1.0) -> None:
        self.data_dir = data_dir.resolve()
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="speech-capture-background-processor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            with JobStore(self.data_dir / "worker.sqlite3") as store:
                store.recover_interrupted_jobs()
                executor = ContinuousJobExecutor(store, data_dir=self.data_dir)
                while not self._stop_event.is_set():
                    outcome = executor.run_once()
                    if outcome is BackgroundStepOutcome.ADVANCED:
                        continue
                    self._stop_event.wait(self.poll_interval_seconds)
        except Exception:
            LOGGER.exception("The background Worker processor stopped unexpectedly.")
