# Speech Capture Worker

Status: persistent Worker core plus a versioned authenticated API with fail-closed local/HTTPS listeners, durable media intake, one-active-job scheduling, progressive transcript persistence, deterministic normalization, restart-safe local ASR chunk execution, one-call batch execution of all remaining ASR chunks, durable whole-transcript alignment finalization, anonymous speaker attribution, content-type classification and evidence-linked extraction, deterministic backend artifact generation, controlled forced-alignment fallback, conservative PCM gap evidence, evidence-bound definite-silence materialization, and explicit human-reviewed gap outcomes.

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
- one-call restart-safe batch execution of every remaining ASR chunk with an
  optional per-run chunk limit;
- a synthetic multi-chunk end-to-end pass from verified intake through
  transcription completion and alignment advancement to `diarizing`;
- revision-pinned pyannote speaker diarization with anonymous speaker
  attribution, safe degradation, and restart-safe evidence;
- content-type classification and bounded-batch evidence-linked extraction
  through local Ollama, with unsupported findings kept out of the summary;
- scene-specific synthesis contracts and Note rendering for interviews,
  courses, speeches, voice memos or personal notes, and generic content;
- synthetic cross-type quality gates that require complete-transcript input,
  evidence-linked sections, and content-appropriate headings without meeting-template filler;
- deterministic backend artifacts: raw transcript, evidence transcript,
  structured record, content-aware note, and a checksummed manifest;
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

# Plain HTTP is accepted only on loopback. Remote binds fail unless an explicit
# private-network IP and protected TLS certificate/key pair are supplied.
uv run speech-capture-worker serve \
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

uv run speech-capture-worker run-asr-all \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker finalize-alignment \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker run-diarization \
  --data-dir runtime/dev-worker \
  job_example \
  --model-revision 84fd25912480287da0247647c3d2b4853cb3ee5d

uv run speech-capture-worker run-structuring \
  --data-dir runtime/dev-worker \
  job_example \
  --model qwen3:14b

uv run speech-capture-worker generate-artifacts \
  --data-dir runtime/dev-worker \
  job_example

# Save optional free-form context after raw ASR. The file may contain any number
# of lines or paragraphs; the expected revision prevents concurrent overwrites.
uv run speech-capture-worker set-recording-context \
  --data-dir runtime/dev-worker \
  --expected-revision 10 \
  --context-file runtime/dev-worker/recording-context.txt \
  job_example

# Save or clear a user-selected content type. Changing the type reuses the
# corrected transcript, but re-extracts type-dependent findings before the Note.
uv run speech-capture-worker set-content-type \
  --data-dir runtime/dev-worker \
  --expected-revision 11 \
  --content-type speech \
  job_example

# Apply only an explicit confirmed term correction without rerunning ASR or the
# long note models, then regenerate the deterministic artifact package.
uv run speech-capture-worker run-structuring \
  --data-dir runtime/dev-worker \
  --context-corrections-only \
  job_example

uv run speech-capture-worker generate-artifacts \
  --data-dir runtime/dev-worker \
  --force \
  job_example

# Recompute useful structure and replace artifacts after reviewing a processed job.
uv run speech-capture-worker run-structuring \
  --data-dir runtime/dev-worker \
  job_example \
  --model qwen3:14b \
  --force

uv run speech-capture-worker generate-artifacts \
  --data-dir runtime/dev-worker \
  job_example \
  --force

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

Ollama classification and extraction use strict JSON Schemas. Extraction runs
in bounded independent batches, so one invalid batch is rejected without
discarding valid findings from the rest of the recording. Every published
finding must reference stable transcript segment IDs. `--force` writes a new
durable structuring generation and atomically replaces the artifact package
without changing the processed transcript.

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
refreshes alignment after materialization. A fully accounted timeline with
reviewed `inaudible` ranges may continue through diarization while downstream
artifacts remain explicitly partial. This is not an automatic classifier.

`analyze-speech-activity` is an evidence-only evaluation boundary. It requires
the optional `diarization` dependency and a full commit SHA for
`pyannote/segmentation`, runs a resource check before loading the model, and
stores only detector identity, timing observations, and source-evidence
anchors. Both `speech_detected` and `no_speech_detected` explicitly carry
`materialization_authorized: false`; the command never inserts `non_speech` or
`inaudible` segments. A cached Hugging Face authorization may be required to
obtain the revision-pinned model.

## Private VAD gold-set probe

Copy
[`examples/vad-gold-manifest.example.json`](examples/vad-gold-manifest.example.json)
and the referenced local audio into a `test-data-private/` directory. That
directory is ignored by Git, and the CLI refuses manifests outside it or
cache/report paths outside the manifest directory. Use opaque dataset and sample IDs; labels accept
only ordered, non-overlapping `speech` and `non_speech` ranges in milliseconds.
Unlabeled ranges are excluded from scoring.

Long recordings are supported. The probe no longer caps samples at 30 minutes;
the detector runs on fixed 10-minute windows with a 2-second margin on each
side, so speech spanning a window boundary is not lost. Decoding still
normalizes the whole sample in memory once, so a two-hour 16 kHz mono sample
should be planned with roughly one gigabyte of transient memory.

Run a baseline without inventing acceptance thresholds:

First accept the conditions on the
[`pyannote/segmentation` model page](https://huggingface.co/pyannote/segmentation)
and authenticate through the local Hugging Face credential store. Never put a
token in the command line, manifest, or report. Resolve the accepted model's
full commit SHA and pass that immutable value below; `main` is rejected.

```bash
uv sync --extra dev --extra diarization
uv run speech-capture-vad-probe \
  --manifest test-data-private/vad/manifest.json \
  --model-revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --cache-dir test-data-private/vad/model-cache \
  --output test-data-private/vad/report.json
```

After the owner explicitly chooses a policy from measured baselines, all four
policy options must be supplied together:

```bash
uv run speech-capture-vad-probe \
  --manifest test-data-private/vad/manifest.json \
  --model-revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --cache-dir test-data-private/vad/model-cache \
  --output test-data-private/vad/report.json \
  --max-speech-miss-rate <OWNER_SELECTED_RATE> \
  --max-false-speech-rate <OWNER_SELECTED_RATE> \
  --minimum-speech-reference-ms <OWNER_SELECTED_DURATION_MS> \
  --minimum-non-speech-reference-ms <OWNER_SELECTED_DURATION_MS>
```

The placeholders must be replaced with an explicitly reviewed policy; the
project does not provide default values. Reports contain source hashes, opaque IDs, aggregate durations,
and confusion metrics, but no audio path, filename, or transcript. They are
atomically written with `0600` permissions. A passing report still has
`automatic_materialization_authorized: false`.

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

See the [architecture](../../docs/architecture.md), [Worker API direction](../../docs/worker-api.md),
[HTTPS and private-network setup](../../docs/private-network-setup.md), and
[testing strategy](../../docs/testing-strategy.md).

Stage G background-service development and restart boundaries are documented in
[macOS Worker background service](../../docs/macos-background-service.md).
