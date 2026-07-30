# Roadmap

The roadmap is ordered by risk. Dates are intentionally omitted until the model and long-audio performance have been measured on the target Mac.

## Phase 0 — Design baseline

Status: current.

- product requirements;
- architecture and trust boundaries;
- Worker API direction;
- output and evidence model;
- resource, recovery, and testing strategy;
- repository and license baseline.

Exit condition: the accepted product decisions are internally consistent and versioned.

## Phase 1 — Local model spike

Goal: prove the two most uncertain foundations before building a polished plugin.

- install and run `mlx-qwen3-asr`;
- evaluate 1.7B and 0.6B profiles;
- validate timestamps and long-audio chunk boundaries;
- integrate a minimal pyannote diarization path;
- evaluate Ollama Qwen3 14B and 8B hierarchical extraction;
- measure memory, temporary disk, and real-time factor;
- test restart-safe checkpoint files;
- create the first private gold-standard samples.

Exit condition: at least one representative long recording is completely accounted for, speaker labeling is measurable, and important summary claims link to evidence.

## Phase 2 — Local personal alpha

Goal: complete the full same-Mac workflow.

- versioned protocol and generated types;
- durable Worker database and artifact store;
- local pairing and authentication;
- Obsidian submission flow;
- job queue and resource preflight;
- progressive transcript preview;
- content detection and structured output;
- atomic Vault publication;
- transcript corrections and protected human sections;
- basic Worker Manager setup.

Exit condition: a user can process and publish real recordings locally without using development commands.

## Phase 3 — Remote personal alpha

Goal: submit from another Obsidian desktop and continue independently.

- HTTPS Worker endpoint configuration;
- recommended Tailscale setup;
- resumable large-file upload;
- disconnect and reconnect recovery;
- persistent device pairing and revocation;
- processed-versus-published flow;
- publication lease and failover;
- Worker system-service installation and restart recovery.

Exit condition: a laptop can submit, disconnect, and later receive a verified package processed by the home Mac.

## Phase 4 — Reliability and quality

Goal: turn the personal alpha into a dependable daily tool.

- completeness quality gates;
- fault injection across all stages;
- disk and memory safe-pause behavior;
- artifact and database migration tests;
- real-audio quality evaluation;
- terminology learning workflow;
- revision diff and conflict handling;
- redacted diagnostics;
- performance regression tracking.

Exit condition: the acceptance principles in the product requirements pass on the private test matrix.

## Phase 5 — Packaging and design refinement

Goal: remove developer-only setup and finalize the experience.

- packaged Worker runtime;
- model download, activation, and rollback;
- refined SwiftUI Worker Manager;
- Reading Capture-aligned component system;
- high-fidelity workbench and reader states;
- accessibility and localization;
- controlled plugin and Worker update flow;
- install, update, downgrade, and uninstall tests.

Exit condition: the project owner can install a clean build on a fresh supported Mac and recover from common failures without opening a terminal.

## Phase 6 — Friend-ready preview

Goal: safely share the noncommercial personal version.

- Apple Developer ID signing and notarization;
- private security reporting route;
- third-party license audit;
- release notes and compatibility matrix;
- onboarding documentation;
- sanitized diagnostics workflow;
- limited external device and network testing.

Exit condition: an invited user can install, pair, process, update, revoke, and uninstall with documented support boundaries.

## Later possibilities

- interview-intent recognition;
- additional ASR and diarization backends;
- Windows and Linux Workers;
- carefully reviewed mobile submission;
- optional cloud providers;
- optional synchronized-folder transport;
- translation artifacts;
- cross-note and agent workflows built on `speech-record.json`.

These are not V1 commitments. They must preserve the evidence and privacy guarantees before adoption.
