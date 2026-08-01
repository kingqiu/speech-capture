"""Resume-safe developer pipeline for one real recording.

Usage:
    python scripts/run_real_pipeline.py \
      --audio <private-audio-path> \
      --data-dir <worker-data-dir> \
      [--model-revision <pyannote-revision>] \
      [--ollama-model qwen3:14b]

Private audio and generated data stay under the data directory and are never
written to the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from speech_capture_worker.alignment import TranscriptAlignmentFinalizer
from speech_capture_worker.artifact_generation import ArtifactGenerator
from speech_capture_worker.asr_execution import (
    AsrChunkExecutor,
    AsrRunOutcome,
    MlxQwenAsrEngine,
)
from speech_capture_worker.audio_preprocessing import AudioPreprocessor
from speech_capture_worker.diarization_execution import (
    PyannoteSpeakerDiarizationEngine,
    SpeakerDiarizationExecutor,
)
from speech_capture_worker.domain import (
    JobState,
    UploadCreateRequest,
)
from speech_capture_worker.job_store import JobStore
from speech_capture_worker.scheduler import JobScheduler, SchedulerOutcome
from speech_capture_worker.structuring_execution import (
    OllamaStructuringEngine,
    StructuringExecutor,
)

DIARIZATION_REVISION = "84fd25912480287da0247647c3d2b4853cb3ee5d"


def _json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _ensure_upload_and_job(
    store: JobStore,
    *,
    audio: Path,
    data_dir: Path,
) -> str:
    content = audio.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    upload, _ = store.create_upload(
        UploadCreateRequest(
            vault_id="vault_primary",
            source_display_name=audio.name,
            source_sha256=checksum,
            source_size_bytes=len(content),
            media_type="audio/mpeg",
        ),
        idempotency_key=f"real-{checksum[:16]}-upload",
    )
    store.put_upload_part(
        upload.upload_id,
        part_number=1,
        content=content,
        part_sha256=checksum,
    )
    store.complete_upload(upload.upload_id)
    queued, _ = store.create_job_from_upload(
        upload.upload_id,
        idempotency_key=f"real-{checksum[:16]}-job",
    )
    job = store.get_job(queued.job_id)
    if job.state is JobState.QUEUED:
        claimed = JobScheduler(
            store,
            storage_path=data_dir,
        ).run_once()
        if claimed.outcome is not SchedulerOutcome.CLAIMED:
            raise RuntimeError(f"scheduler outcome: {claimed.outcome.value}")
        return claimed.job.job_id
    return job.job_id


def _run_asr(store: JobStore, job_id: str) -> None:
    job = store.get_job(job_id)
    if job.state not in {
        JobState.PREPROCESSING,
        JobState.TRANSCRIBING,
    }:
        return
    executor = AsrChunkExecutor(
        store,
        MlxQwenAsrEngine(model_profile=job.model_profile),
    )
    while True:
        result = executor.run_next(job_id)
        if result.outcome is AsrRunOutcome.TRANSCRIPTION_COMPLETED:
            _json(
                {
                    "event": "asr_completed",
                    "job_id": job_id,
                    "state": result.job.state.value,
                }
            )
            return
        if result.outcome in {
            AsrRunOutcome.PARTIAL,
            AsrRunOutcome.SAFE_PAUSED,
        }:
            _json(
                {
                    "event": "asr_stopped",
                    "job_id": job_id,
                    "outcome": result.outcome.value,
                    "state": result.job.state.value,
                }
            )
            sys.exit(2)
        if result.chunk_index is not None and result.chunk_index % 10 == 0:
            plan = AudioPreprocessor(store).get_plan(job_id)
            _json(
                {
                    "event": "asr_progress",
                    "job_id": job_id,
                    "chunk_index": result.chunk_index,
                    "total_chunks": len(plan.chunks),
                }
            )


def _write_readable_preview(store: JobStore, job_id: str, data_dir: Path) -> None:
    segments = []
    after = 0
    while True:
        snapshot = store.get_job_snapshot(
            job_id,
            after_segment_sequence=after,
            segment_limit=500,
        )
        segments.extend(snapshot.stable_segments)
        if not snapshot.has_more_segments:
            break
        after = snapshot.next_after_segment_sequence

    def fmt(ms: int) -> str:
        seconds = ms // 1000
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    lines = ["# 可读文字稿预览", ""]
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        lines.append(f"## {fmt(segment.start_ms)}–{fmt(segment.end_ms)}")
        lines.append("")
        lines.append(text)
        lines.append("")
    output = data_dir / "transcript-readable.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    _json(
        {
            "event": "readable_preview_written",
            "path": str(output),
            "segment_count": len(segments),
        }
    )


def _run_stages(store: JobStore, job_id: str, args: argparse.Namespace) -> None:
    job = store.get_job(job_id)
    if job.state is JobState.ALIGNING:
        finalized = TranscriptAlignmentFinalizer(store).finalize(job_id)
        _json(
            {
                "event": "alignment_completed",
                "job_id": job_id,
                "outcome": finalized.outcome.value,
                "state": finalized.job.state.value,
            }
        )
    job = store.get_job(job_id)
    if job.state is JobState.DIARIZING:
        engine = PyannoteSpeakerDiarizationEngine(
            model_revision=args.model_revision,
            cache_dir=args.data_dir / "models" / "pyannote",
        )
        result = SpeakerDiarizationExecutor(store, engine).run(job_id)
        _json(
            {
                "event": "diarization_completed",
                "job_id": job_id,
                "outcome": result.outcome.value,
                "state": result.job.state.value,
                "speaker_turn_count": result.speaker_turn_count,
                "attributed_segment_count": result.attributed_segment_count,
            }
        )
    job = store.get_job(job_id)
    if job.state is JobState.STRUCTURING:
        result = StructuringExecutor(
            store,
            OllamaStructuringEngine(model=args.ollama_model),
        ).run(job_id)
        _json(
            {
                "event": "structuring_completed",
                "job_id": job_id,
                "outcome": result.outcome.value,
                "state": result.job.state.value,
                "content_type": result.content_type,
                "finding_count": result.finding_count,
                "unsupported_finding_count": result.unsupported_finding_count,
            }
        )
    job = store.get_job(job_id)
    if job.state is JobState.QUALITY_CHECK:
        result = ArtifactGenerator(store).generate(job_id)
        _json(
            {
                "event": "artifacts_generated",
                "job_id": job_id,
                "outcome": result.outcome.value,
                "state": result.job.state.value,
                "speech_id": result.speech_id,
                "manifest_sha256": result.manifest_sha256,
                "package_relative_path": result.package_relative_path,
            }
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model-revision", default=DIARIZATION_REVISION)
    parser.add_argument("--ollama-model", default="qwen3:14b")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with JobStore(
        data_dir / "worker.sqlite3",
        upload_chunk_size_bytes=64 * 1024 * 1024,
    ) as store:
        store.recover_interrupted_jobs()
        job = _ensure_upload_and_job(store, audio=args.audio, data_dir=data_dir)
        _run_asr(store, job)
        _write_readable_preview(store, job, data_dir)
        _run_stages(store, job, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
