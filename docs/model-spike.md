# Local Model Spike

## 1. Purpose

Phase 1 validates the model boundary before building the persistent Worker API or Obsidian UI.

The spike must answer:

- Can the target Mac run the accuracy-first model safely?
- Can a long source be divided and recombined without silent gaps?
- Can generation truncation be detected?
- Are timestamps monotonic and within source bounds?
- What latency, memory, and temporary disk behavior should drive Worker safeguards?
- What additional setup is required for local speaker diarization?

## 2. Baseline environment

Initial development host:

- Apple M4;
- 32 GB unified memory;
- Apple Silicon macOS;
- FFmpeg 8.0.1;
- Ollama installed with no summary model downloaded at the start of the spike;
- approximately 75 GiB free before model downloads.

Machine serial numbers, hostnames, account names, absolute paths, and other stable personal identifiers are deliberately excluded.

## 3. Upstream baseline

The first probe pins:

- `mlx-qwen3-asr` 0.3.5;
- accuracy model `Qwen/Qwen3-ASR-1.7B`;
- speed model `Qwen/Qwen3-ASR-0.6B`;
- forced aligner `Qwen/Qwen3-ForcedAligner-0.6B`;
- optional pyannote 4.x diarization.

The pinned upstream package already exposes:

- energy-based long-audio chunks;
- chunk start and end times;
- per-chunk generation finish reasons;
- an aggregate truncation flag;
- progress callbacks;
- native forced alignment;
- optional pyannote speaker attribution;
- explicit model sessions;
- per-chunk MLX cache release.

Speech Capture still needs its own Worker. The upstream server does not own our resumable source upload, durable queue, resource policy, correction ledger, publication lease, Vault package, or processed-versus-published contract.

## 4. Download budget

Approximate upstream repository payloads at the start of this spike:

| Model | Download size |
| --- | ---: |
| Qwen3-ASR 1.7B | 4.70 GB |
| Qwen3-ASR 0.6B | 1.88 GB |
| Qwen3 ForcedAligner 0.6B | 1.84 GB |

The probe applies 15% download headroom and preserves the larger of 20 GiB or 10% of the target volume as free space.

## 5. Probe contract

The local command writes a private JSON report containing:

- safe environment versions;
- source display name, checksum, format, duration, and audio stream facts;
- model profile and options;
- progress events without transcript text;
- model output and timestamps;
- elapsed time, real-time factor, and process memory high-water mark;
- coverage and timestamp issues.
- optional normalized character error rate against a reviewed reference.

The report excludes the source absolute path, hostname, device serial number, user account, and credentials.

Reports and private fixtures remain under ignored local directories.

## 6. Completeness gate

For the model spike, `complete` requires:

- a valid ordered zero-based chunk sequence;
- continuous chunk coverage from source start to source end;
- no overlap beyond codec tolerance;
- no chunk or aggregate generation truncation;
- a finish reason for every chunk;
- non-empty output for every chunk;
- monotonic timestamps within the decoded source duration.
- character error rate at or below the configured threshold when a reference is supplied.

An empty chunk is considered unresolved until a later Worker stage can prove that range is silence or non-speech. This conservative rule prevents an empty ASR response from being accepted silently.

Timeline coverage proves that every range was processed, not that every spoken word was recognized. Controlled fixtures therefore add a reviewed reference and normalized multilingual character error rate. The first synthetic run exposed substitutions even though its timeline was mechanically complete, validating the need for both gates.

## 7. Measurement sequence

1. Run unit tests for coverage rules.
2. Generate a non-private synthetic speech fixture.
3. Download and run the 1.7B accuracy profile.
4. Add forced-alignment timestamps.
5. Run a long synthetic boundary case exceeding one upstream chunk.
6. Compare the 0.6B profile if resource measurements justify it.
7. Add a private real recording supplied by the project owner.
8. Add a real multi-speaker sample and pyannote token after the model terms are accepted.

## 8. Exit criteria

Phase 1 exits only after:

- the probe completes on at least one Chinese or Chinese-English speech sample;
- a multi-chunk sample has no unexplained timeline gap;
- truncation detection has a passing automated test;
- timestamp validation has run against real model output;
- baseline speed and memory are recorded;
- diarization setup and its remaining evidence are explicit.

## 9. Initial measured result

The first controlled bilingual fixture was generated locally with separate system voices:

- Chinese, then English, then Chinese;
- 47.987 seconds of AAC audio in an M4A container;
- two energy-based ASR chunks;
- 286 normalized reference characters;
- no private or user-provided content.

Results:

| Profile | Elapsed | Real-time factor | First completed chunk | Peak process memory | CER | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Qwen3-ASR 1.7B + aligner | 29.62 s | 0.617 | 15.79 s | 5.75 GB | 0.00% | complete |
| Qwen3-ASR 0.6B + aligner | 11.76 s | 0.245 | 6.14 s | 3.53 GB | 1.05% | complete |

Both profiles:

- covered the decoded source timeline;
- returned a finish reason for every chunk;
- reported no generation truncation;
- produced monotonic in-bounds timestamps;
- preserved the Chinese-English-Chinese transition.

The 0.6B profile used about 61% of the measured peak process memory and about 40% of the elapsed time of the 1.7B profile on this fixture. The 1.7B profile remains the accuracy-first default; 0.6B is validated as a meaningful speed and resource option.

The sanitized machine-readable result is stored in [the synthetic bilingual benchmark](benchmarks/2026-07-30-synthetic-bilingual-asr.json). Raw probe reports and generated audio remain ignored locally.

## 10. Remaining evidence

This baseline does not yet prove production quality:

- synthetic speech is cleaner than meetings, interviews, and handheld recordings;
- the test is under one minute rather than multi-hour;
- no overlap, noise, echo, or low-volume speech was present;
- diarization still needs accepted pyannote model terms, a local Hugging Face token, and a real multi-speaker sample;
- content extraction and Ollama summarization have not yet been measured.

The isolated Python environment successfully imports pyannote.audio 4.0.7, torch, and torchcodec. The upstream doctor reports only the expected missing-token warning; no diarization inference or gated model download has been attempted.

The next quality step is a private real recording supplied by the project owner. Its audio, reviewed transcript, and raw reports must remain outside Git.

The validated probe boundary is now connected to the persistent Worker core for one-chunk-at-a-time execution. The Worker adds deterministic private PCM normalization, frame-complete chunk planning, raw-attempt durability, output rejection and retry, stable transcript materialization, progress, resource boundary pause, and restart replay. The probe remains the benchmark harness; it is not the durable job store.
