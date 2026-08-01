# Roadmap

The roadmap is ordered by risk. Dates are intentionally omitted until the model and long-audio performance have been measured on the target Mac.

## Phase 0 — Design baseline

Status: complete.

- product requirements;
- architecture and trust boundaries;
- Worker API direction;
- output and evidence model;
- resource, recovery, and testing strategy;
- repository and license baseline.

Exit condition: the accepted product decisions are internally consistent and versioned.

## Phase 1 — Local model spike

Status: in progress. The MLX Qwen3-ASR profiles and timestamp path are validated; diarization, local summarization, and representative real-audio acceptance remain.

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

## Phase 2 — Backend personal alpha

Goal: complete the durable local processing contract before frontend implementation.

Status: in progress. Durable storage, resumable verified intake, one-active-job scheduling, resource preflight, progressive snapshots, deterministic normalization, immutable raw attempts, restart-safe local ASR chunk execution, a durable whole-transcript alignment/completeness gate, controlled forced-alignment fallback, conservative PCM evidence for uncovered ranges, evidence-bound definite-silence timeline backfill, and exact-range human-reviewed gap outcomes are implemented.

- versioned protocol and generated types;
- durable Worker database and artifact store;
- local pairing and authentication;
- resumable intake and source verification;
- job queue and resource preflight;
- progressive transcript snapshot and event contract;
- content detection and structured output;
- optional per-recording free-form context for post-ASR cleanup and synthesis, with
  transcript evidence remaining authoritative;
- publication protocol and atomic-package fixtures;
- transcript corrections and protected human sections;
- basic Worker Manager setup.

Exit condition: representative local jobs can be submitted through backend test tools, processed, recovered, and published through stable protocol fixtures, with every plugin-visible state represented.

## Phase 3 — Obsidian interaction design gate

Goal: validate the complete plugin experience before writing its user-facing pages.

- page and navigation information architecture;
- local and remote end-to-end wireflows;
- complete normal, warning, blocked, interrupted, recovery, and publication state matrix;
- protocol-to-interface mappings;
- Reading Capture-aligned visual system and component direction;
- key interaction visuals generated through Codex GPT Image with synthetic data;
- narrow-pane, keyboard, reduced-motion, light-theme, and dark-theme behavior;
- project-owner review, revision, and explicit approval.

Exit condition: the project owner approves the task workbench, progressive transcript, reader, speaker review, resource warning, reconnect, and publication designs. No Obsidian page implementation begins before this condition is met.

## Phase 4 — Obsidian local personal alpha

Goal: implement the approved same-Mac plugin workflow.

- Obsidian source selection, optional free-form context, and submission;
- Worker selection and local connection states;
- upload, verification, queue, and job controls;
- progressive transcript preview;
- disk, memory, model, and recovery actions;
- atomic Vault publication;
- transcript reader, speaker review, and corrections;
- structured-note regeneration with protected human sections;
- accessibility and theme conformance.

Exit condition: a user can process, review, and publish real recordings locally without using development commands, and the implementation matches the approved design contract.

## Phase 5 — Remote personal alpha

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

## Phase 6 — Reliability and quality

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

## Phase 7 — Packaging and experience refinement

Goal: remove developer-only setup and refine the approved experience without changing its core flows silently.

- packaged Worker runtime;
- model download, activation, and rollback;
- refined SwiftUI Worker Manager;
- component-level polish and design QA;
- additional long-tail workbench and reader states;
- accessibility and localization;
- controlled plugin and Worker update flow;
- install, update, downgrade, and uninstall tests.

Exit condition: the project owner can install a clean build on a fresh supported Mac and recover from common failures without opening a terminal.

## Phase 8 — Friend-ready preview

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
