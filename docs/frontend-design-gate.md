# Obsidian Frontend Design Gate

Speech Capture uses an explicit design approval gate between Worker development and Obsidian frontend implementation. The purpose is to stabilize the long-running processing contract first, then validate the complete user experience before page structure becomes expensive to change.

## 1. Required sequence

Work proceeds in this order:

1. complete and validate the Worker backend and protocol behaviors needed by the plugin;
2. derive the Obsidian information architecture from those behaviors;
3. define end-to-end wireflows, a state matrix, and protocol-to-interface mappings;
4. create key interaction visuals with the GPT Image capability provided through Codex;
5. review the designs with the project owner and revise them;
6. receive explicit approval from the project owner;
7. only then begin implementation of the Obsidian task workbench and transcript reader.

Backend, protocol, test-fixture, and non-visual research work may continue before approval. The project must not implement the plugin's user-facing pages or commit to an unapproved component structure during this period.

## 2. Backend readiness for design

The design phase starts when the Worker exposes a sufficiently stable personal-V1 contract for:

- durable intake, resumable upload, verification, and duplicate detection;
- queue, lifecycle, pause, resume, cancel, retry, and recovery states;
- disk, memory, model, and connectivity preflight outcomes;
- progressive transcript snapshots, committed segments, provisional tails, and event cursors;
- speaker-diarization progress and reviewable anonymous speaker labels;
- processed, partial, failed, and published semantics;
- output-package, evidence-link, correction, and regeneration data;
- representative fixtures for normal, slow, interrupted, blocked, and recoverable scenarios.

This gate does not require optional cloud providers, interview-intent recognition, or friend-ready packaging to be complete.

## 3. Required design deliverables

The project owner receives the following before frontend implementation:

- page and navigation information architecture;
- local and remote end-to-end wireflows;
- screen-by-state matrix covering normal, empty, loading, warning, blocked, paused, disconnected, failed, processed, partial, and published states;
- protocol-to-interface mapping for every visible status, action, warning, progress value, and transcript update;
- key high-fidelity interaction images generated through Codex GPT Image;
- component, spacing, typography, color, and interaction specifications aligned with Reading Capture;
- Simplified Chinese interface copy and English-ready message keys;
- behavior for narrow sidebars, detached windows, keyboard navigation, reduced motion, and light and dark Obsidian themes;
- an approval checklist recording accepted decisions and unresolved items.

The images communicate hierarchy, state, and interaction intent. The written state and data specifications remain the implementation source of truth.

## 4. Key interaction visuals

The first visual review should include:

1. source selection, source facts, recording-date suggestion, and confirmation;
2. Worker selection, local or remote connection state, and processing profile;
3. resumable upload and verification;
4. active processing with stage progress, committed transcript segments, a labeled provisional tail, speaker-labeling status, and live-tail control;
5. disk-blocked, memory-warning, and safe-paused recovery states;
6. Worker-offline, reconnecting, and restored-snapshot states;
7. processed-but-not-published and publication-recovery states;
8. transcript reading, audio evidence navigation, speaker renaming, and attribution review;
9. correction and summary-regeneration comparison with protected `我的补充`;
10. first-run Worker setup and the essential plugin settings groups.

When one image cannot explain a temporal interaction safely, a short sequence of frames should be used instead of compressing multiple states into one screen.

## 5. Image-generation safety and traceability

- Use synthetic filenames, people, transcripts, identifiers, dates, endpoints, and diagnostic values.
- Do not send private audio, real transcripts, credentials, email addresses, device identifiers, or absolute local paths to image generation.
- Record the prompt purpose, version, and accepted design decision alongside each retained image.
- Store approved and comparison images under `docs/design/` when the design phase begins.
- Treat generated text inside images as illustrative; finalized interface copy belongs in the written specification.
- Do not infer implementation behavior solely from an image.

## 6. Approval criteria

Frontend implementation remains blocked until the project owner explicitly approves:

- the default submission and processing flow;
- how progressive and final transcript evidence are distinguished;
- multi-speaker review and correction behavior;
- resource, connectivity, recovery, and publication states;
- the information architecture of the task workbench and transcript reader;
- primary terminology and Chinese copy direction;
- visual alignment with Reading Capture;
- accessibility and narrow-pane behavior;
- the set of remaining design questions that may safely be resolved during implementation.

Any later change that materially alters navigation, evidence semantics, destructive actions, privacy boundaries, or recovery behavior returns the affected flow to design review.

## 7. Approval and implementation fidelity

The project owner explicitly approved Stage H on 2026-08-03. The approved visuals are implementation targets,
not loose inspiration. Stage I must reproduce their layout, hierarchy, density, typography, semantic colors,
controls, responsive behavior, and state presentation as closely as the Obsidian host permits.

The written interaction baseline remains authoritative for behavior and data ownership; the current images in the
visual review index are authoritative for visual presentation; the message catalog is authoritative for interface
copy. Each key implemented page requires a synthetic-data screenshot comparison against its approved visual before
the page is considered complete. A material departure must be documented and returned to the project owner for
review rather than introduced as an implementation-time redesign.
