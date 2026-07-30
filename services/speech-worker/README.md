# Speech Capture Worker

Status: design only; no runnable service exists yet.

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

See the [architecture](../../docs/architecture.md), [Worker API direction](../../docs/worker-api.md), and [testing strategy](../../docs/testing-strategy.md).
