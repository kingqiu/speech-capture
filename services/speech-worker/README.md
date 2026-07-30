# Speech Capture Worker

Status: local model spike, persistent Worker core, durable media intake, and one-active-job scheduling. There is no network Worker service yet.

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
```

`runtime/` is ignored by Git. The CLI requires an explicit data directory and does not create a global service installation.

Upload completion requires FFprobe. The packaged Worker will own that dependency; during development it must be available on `PATH`.

See [Persistent Worker core](../../docs/worker-core.md) for state, schema, recovery, resource rules, and current boundaries.

## ASR probe

The first executable is a local-only probe for measuring the ASR integration before it becomes a durable Worker job.

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
