# Speech Capture Worker

Status: local model spike, persistent Worker core, durable media intake, one-active-job scheduling, progressive transcript persistence, deterministic normalization, restart-safe local ASR chunk execution, durable whole-transcript alignment finalization, controlled forced-alignment fallback, conservative PCM gap evidence, evidence-bound definite-silence materialization, and explicit human-reviewed gap outcomes. There is no network Worker service yet.

The Worker performs durable local processing outside Obsidian. It will own:

- paired-device authorization;
- resumable source uploads and checksum verification;
- persistent job queue and restart recovery;
- disk and memory preflight;
- MLX Qwen3-ASR orchestration;
- timing alignment and pyannote speaker diarization;
- local Ollama classification and hierarchical summarization;
- transcript completeness and evidence checks;
- artifact storage and publication leases;
- redacted local diagnostics.

The first supported platform is Apple Silicon macOS.

Planned implementation: Python 3.11, FastAPI, SQLite, FFmpeg, MLX, pyannote, Ollama, and `uv`.

## Persistent Worker core

The package now contains:

- a strict job state machine;
- SQLite job, event, checkpoint, upload-manifest, and part-receipt storage;
- resumable checksum-bound upload parts;
- atomic whole-source assembly and FFprobe validation;
- verified upload-to-job binding;
- one-active-job scheduling with persisted resource preflight;
- monotonic progress, stable transcript outcomes, and a revision-guarded provisional tail;
- text-preserving alignment and speaker-attribution revisions;
- bounded reconnect snapshots and content-free update cursors;
- private deterministic 16 kHz PCM normalization and complete frame-based chunk plans;
- immutable checksummed raw ASR attempts and idempotent replay;
- real MLX Qwen3-ASR chunk execution with validation, retry, and safe boundary pause;
- a private alignment report that separately proves raw evidence, aligned timing,
  complete timeline accounting, and transcript completeness before diarization;
- one-estimated-segment-at-a-time forced alignment that preserves stable text
  and IDs, persists checksummed private word evidence, and refreshes the gate;
- a private gap-analysis report that classifies only sufficiently long
  near-digital silence and leaves all audible or uncertain PCM unresolved;
- evidence-bound backfill of default-policy definite silence as aligned
  `non_speech` timeline outcomes, followed by automatic alignment refresh;
- exact-range, version-anchored human review of unresolved gaps as aligned
  `non_speech` or `inaudible` outcomes without storing reviewer text or identity;
- idempotent creation, revision guards, and restart recovery;
- disk and memory preflight.

Developer commands:

```bash
uv sync --extra dev

uv run speech-capture-worker init \
  --data-dir runtime/dev-worker

uv run speech-capture-worker preflight \
  --storage-path runtime/dev-worker \
  --profile accuracy \
  --source-size-bytes 536870912 \
  --duration-sec 3600

uv run speech-capture-worker create-upload \
  --data-dir runtime/dev-worker \
  --vault-id vault_primary \
  --source-name meeting.m4a \
  --source-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --source-size-bytes 536870912 \
  --media-type audio/mp4 \
  --idempotency-key upload-test-001

uv run speech-capture-worker create-job-from-upload \
  --data-dir runtime/dev-worker \
  upl_example \
  --idempotency-key job-test-001

uv run speech-capture-worker schedule-once \
  --data-dir runtime/dev-worker

uv run speech-capture-worker snapshot \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker updates \
  --data-dir runtime/dev-worker \
  job_example \
  --after-sequence 0

uv run speech-capture-worker prepare-audio \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker run-asr-next \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker finalize-alignment \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker force-align-next \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker analyze-gaps \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker materialize-silence \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker review-gap \
  --data-dir runtime/dev-worker \
  job_example \
  --review-key review-0001 \
  --start-ms 12500 \
  --end-ms 13900 \
  --outcome non_speech

uv sync --extra dev --extra diarization
uv run speech-capture-worker analyze-speech-activity \
  --data-dir runtime/dev-worker \
  job_example \
  --model-revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

`runtime/` is ignored by Git. The CLI requires an explicit data directory and does not create a global service installation.

`analyze-gaps` consumes the latest durable alignment report while the job remains
in `aligning`. Its evidence is anchored to that report generation and the
checksummed normalized WAV. It does not infer non-speech from audible PCM or
advance the job by itself.

`force-align-next` consumes the current alignment report and processes at most
one stable `estimated` transcript segment. It requires language metadata,
checks resources before model work, aligns the unchanged text against the exact
normalized PCM range, stores the raw word result in a private checksummed file,
then updates only the segment timing metadata. Invalid, incomplete, stale, or
tampered evidence cannot pass the whole-transcript exit gate.

`materialize-silence` reruns the conservative default policy, commits only
currently proven silence, stores one evidence checkpoint per inserted range,
and reruns whole-transcript alignment. Custom thresholds remain useful for
inspection but cannot authorize timeline materialization.

`review-gap` records one explicit human decision for an exact unresolved range.
The review key is an opaque idempotency key, not a reviewer identity. Only
`non_speech` and `inaudible` are accepted; the command stores no free-form
review text, rejects stale alignment reports or partial-range decisions, and
refreshes alignment after materialization. This is not an automatic classifier.

`analyze-speech-activity` is an evidence-only evaluation boundary. It requires
the optional `diarization` dependency and a full commit SHA for
`pyannote/segmentation`, runs a resource check before loading the model, and
stores only detector identity, timing observations, and source-evidence
anchors. Both `speech_detected` and `no_speech_detected` explicitly carry
`materialization_authorized: false`; the command never inserts `non_speech` or
`inaudible` segments. A cached Hugging Face authorization may be required to
obtain the revision-pinned model.

Upload completion requires FFprobe. The packaged Worker will own that dependency; during development it must be available on `PATH`.

See [Persistent Worker core](../../docs/worker-core.md) for state, schema, recovery, resource rules, and current boundaries.

## ASR probe

The local-only probe remains the benchmarking and quality-measurement tool. Durable Worker execution now reuses the validated MLX model boundary while adding normalized chunk plans, immutable attempts, retries, progress, and restart replay.

From this directory:

```bash
uv sync --extra dev
uv run speech-capture-asr-probe doctor
uv run speech-capture-asr-probe download \
  --model Qwen/Qwen3-ASR-1.7B \
  --with-aligner
uv run speech-capture-asr-probe run /path/to/audio.m4a \
  --model Qwen/Qwen3-ASR-1.7B \
  --timestamps \
  --reference-file /path/to/reviewed-transcript.txt \
  --output tests/output/asr-probe.json
```

Probe reports may contain transcript text. `tests/output/` and private fixtures are ignored by Git and must remain local.

The probe checks source duration, chunk timeline continuity, generation truncation, empty chunk output, timestamp bounds, and optional normalized character error rate against a reviewed transcript. It exits with a non-zero status when the current result cannot be called complete.

See the [architecture](../../docs/architecture.md), [Worker API direction](../../docs/worker-api.md), and [testing strategy](../../docs/testing-strategy.md).
