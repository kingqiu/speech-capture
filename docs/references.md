# Reference Projects and Sources

These projects inform the design or implementation research. Inclusion here does not mean their code has been copied.

## Product and UI references

- [Reading Capture](https://github.com/kingqiu/reading-capture-plugin) — visual direction and interaction consistency for the related Obsidian workflow.
- [whisper-obsidian-plugin](https://github.com/nikdanilov/whisper-obsidian-plugin) — prior art for transcription inside Obsidian.
- [Audio Transcription community plugin page](https://community.obsidian.md/plugins/audio-transcription) — community workflow reference.
- [Audio-To-Text](https://github.com/aaldrich29/Audio-To-Text) — prior art for audio-to-text processing.

## Model and platform references

- [mlx-qwen3-asr](https://github.com/moona3k/mlx-qwen3-asr) — planned Apple Silicon ASR integration.
- [Obsidian developer documentation](https://docs.obsidian.md/) — plugin platform contract.
- [MLX](https://github.com/ml-explore/mlx) — Apple Silicon machine-learning runtime.
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — planned local speaker-diarization foundation.
- [Ollama](https://github.com/ollama/ollama) — planned local summary-model runtime.
- [Tailscale documentation](https://tailscale.com/kb) — recommended private-network setup, not a protocol dependency.

## Reuse policy

Before reusing code, assets, prompts, schemas, or bundled model files:

1. inspect the exact upstream version and license;
2. record the dependency and required attribution;
3. confirm compatibility with this project's noncommercial license and distribution plan;
4. prefer documented APIs and independent implementation when license terms are unclear;
5. never import private data or credentials from a reference installation.

References should be pinned to versions or commits when they become implementation dependencies.
