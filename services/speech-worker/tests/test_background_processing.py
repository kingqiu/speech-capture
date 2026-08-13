import hashlib
from types import SimpleNamespace

from speech_capture_worker.background_processing import (
    BACKGROUND_STAGE,
    BackgroundStepOutcome,
    ContinuousJobExecutor,
)
from speech_capture_worker.domain import JobState, UploadCreateRequest
from speech_capture_worker.gap_speech_activity import GapSpeechActivityOutcome
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.media_probe import MediaProbeResult


def _source_probe(_):
    return MediaProbeResult(
        duration_seconds=3.0,
        audio_stream_count=1,
        format_name="wav",
    )


def _queued_job(store: JobStore, *, suffix: str):
    content = f"synthetic-background-audio-{suffix}".encode()
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_test",
            source_display_name=f"synthetic-{suffix}.wav",
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/wav",
        ),
        idempotency_key=f"upload-{suffix}",
    )
    store.put_upload_part(
        upload.upload_id,
        part_number=1,
        content=content,
        part_sha256=checksum,
    )
    store.complete_upload(upload.upload_id)
    job, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"job-{suffix}",
    )
    return job


def test_continuous_executor_claims_and_advances_one_queued_job(tmp_path) -> None:
    seen = []
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=_source_probe,
    ) as store:
        queued = _queued_job(store, suffix="claim")
        store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )

        def advance(job):
            seen.append(job)
            return BackgroundStepOutcome.ADVANCED

        outcome = ContinuousJobExecutor(
            store,
            data_dir=tmp_path,
            advance_job=advance,
        ).run_once()
        current = store.get_job(queued.job_id)

    assert outcome is BackgroundStepOutcome.ADVANCED
    assert [job.job_id for job in seen] == [queued.job_id]
    assert seen[0].state is JobState.PREPROCESSING
    assert current.state is JobState.PREPROCESSING


def test_continuous_executor_records_generic_failure_and_stops_retry_loop(tmp_path) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=_source_probe,
    ) as store:
        queued = _queued_job(store, suffix="failure")
        store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )

        def fail(_job):
            raise RuntimeError("private details must not enter the public job")

        outcome = ContinuousJobExecutor(
            store,
            data_dir=tmp_path,
            advance_job=fail,
        ).run_once()
        current = store.get_job(queued.job_id)
        checkpoints = store.list_checkpoints(queued.job_id, stage=BACKGROUND_STAGE)
        repeated = ContinuousJobExecutor(
            store,
            data_dir=tmp_path,
            advance_job=fail,
        ).run_once()

    assert outcome is BackgroundStepOutcome.FAILED
    assert repeated is BackgroundStepOutcome.IDLE
    assert current.state is JobState.FAILED
    assert current.last_error_code == "BACKGROUND_STAGE_FAILED"
    assert current.last_error_message == (
        "The local Worker could not complete the current processing stage."
    )
    assert checkpoints[-1].payload == {
        "exception_type": "RuntimeError",
        "state": "preprocessing",
    }


def test_continuous_executor_does_not_fail_a_job_that_advanced_concurrently(
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=_source_probe,
    ) as store:
        queued = _queued_job(store, suffix="stale-failure")
        store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )

        def advance_then_fail(observed):
            store.transition_job(
                observed.job_id,
                JobState.TRANSCRIBING,
                expected_revision=observed.revision,
                reason_code="concurrent_test_advance",
            )
            raise RuntimeError("the observed processing snapshot is now stale")

        outcome = ContinuousJobExecutor(
            store,
            data_dir=tmp_path,
            advance_job=advance_then_fail,
        ).run_once()
        current = store.get_job(queued.job_id)
        checkpoints = store.list_checkpoints(queued.job_id, stage=BACKGROUND_STAGE)

    assert outcome is BackgroundStepOutcome.ADVANCED
    assert current.state is JobState.TRANSCRIBING
    assert current.last_error_code is None
    assert checkpoints == []


def test_preprocessing_advances_directly_into_asr(monkeypatch, tmp_path) -> None:
    calls = []
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=_source_probe,
    ) as store:
        queued = _queued_job(store, suffix="preprocess-asr")
        preprocessing = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )

        class FakePreprocessor:
            def __init__(self, received_store):
                assert received_store is store

            def prepare(self, job_id):
                calls.append(("prepare", job_id))

        class FakeAsrExecutor:
            def __init__(self, received_store, engine):
                assert received_store is store
                assert engine == "fake-asr"

            def run_all(self, job_id):
                calls.append(("asr", job_id))

        monkeypatch.setattr(
            "speech_capture_worker.background_processing.AudioPreprocessor",
            FakePreprocessor,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.AsrChunkExecutor",
            FakeAsrExecutor,
        )
        executor = ContinuousJobExecutor(store, data_dir=tmp_path)
        monkeypatch.setattr(executor, "_asr_engine", lambda _profile: "fake-asr")

        outcome = executor._advance_job(preprocessing)

    assert outcome is BackgroundStepOutcome.ADVANCED
    assert calls == [
        ("prepare", queued.job_id),
        ("asr", queued.job_id),
    ]


def test_alignment_resource_block_pauses_before_gap_retranscription(
    monkeypatch,
    tmp_path,
) -> None:
    with JobStore(
        tmp_path / "worker.sqlite3",
        source_probe=_source_probe,
    ) as store:
        queued = _queued_job(store, suffix="vad-resource")
        preprocessing = store.claim_job_for_processing(
            queued.job_id,
            expected_revision=queued.revision,
        )
        transcribing = store.transition_job(
            queued.job_id,
            JobState.TRANSCRIBING,
            expected_revision=preprocessing.revision,
        )
        aligning = store.transition_job(
            queued.job_id,
            JobState.ALIGNING,
            expected_revision=transcribing.revision,
        )

        class FakeFinalizer:
            def __init__(self, received_store):
                assert received_store is store

            def finalize(self, job_id):
                return SimpleNamespace(job=store.get_job(job_id))

        class FakeGapAnalyzer:
            def __init__(self, received_store):
                assert received_store is store

            def analyze(self, _job_id):
                return None

        class FakeSilenceMaterializer:
            def __init__(self, received_store):
                assert received_store is store

            def materialize(self, job_id):
                return SimpleNamespace(
                    alignment=SimpleNamespace(job=store.get_job(job_id))
                )

        class FakeDetector:
            def __init__(self, **_kwargs):
                pass

        class FakeSpeechActivityAnalyzer:
            def __init__(self, received_store, _detector):
                assert received_store is store

            def analyze(self, job_id):
                return SimpleNamespace(
                    outcome=GapSpeechActivityOutcome.SAFE_PAUSED,
                    job=store.get_job(job_id),
                )

        class UnexpectedRetranscription:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("gap retranscription must not run after a safe pause")

        monkeypatch.setattr(
            "speech_capture_worker.background_processing.TranscriptAlignmentFinalizer",
            FakeFinalizer,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.TranscriptGapAnalyzer",
            FakeGapAnalyzer,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.DefiniteSilenceMaterializer",
            FakeSilenceMaterializer,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.PyannoteVoiceActivityDetector",
            FakeDetector,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.GapSpeechActivityAnalyzer",
            FakeSpeechActivityAnalyzer,
        )
        monkeypatch.setattr(
            "speech_capture_worker.background_processing.GapRetranscriptionExecutor",
            UnexpectedRetranscription,
        )
        executor = ContinuousJobExecutor(store, data_dir=tmp_path)

        outcome = executor._advance_alignment(aligning)
        current = store.get_job(queued.job_id)

    assert outcome is BackgroundStepOutcome.ADVANCED
    assert current.state is JobState.PAUSED
    assert current.last_error_code == "SPEECH_ACTIVITY_RESOURCE_BLOCKED"
