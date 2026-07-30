# Speech Capture Worker

Status: Phase 1 model spike. There is no runnable Worker service yet.

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
