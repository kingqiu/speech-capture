# Testing Strategy

## 1. Testing objective

Testing must prove two product promises:

1. every decodable part of the audio is represented or explicitly reported as unresolved;
2. extracted information is useful, evidence-linked, and does not introduce unsupported critical claims.

A pleasant interface or a successful process exit is not enough.

## 2. Test layers

### 2.1 Unit tests

Cover deterministic components:

- timeline range accounting;
- date-source precedence;
- filename and path sanitization;
- checksum calculation;
- state transitions;
- retry and idempotency rules;
- publication lease behavior;
- transcript correction ledger;
- protected Markdown regions;
- content-type renderers;
- redaction.

### 2.2 Schema and contract tests

- OpenAPI request and response validation.
- JSON Schema compatibility for artifacts.
- TypeScript and Python generated-type parity.
- Error-code stability.
- Older compatible plugin against newer Worker.
- Newer plugin behavior when a Worker lacks a requested capability.

### 2.3 Integration tests

Run the Worker with real FFmpeg, SQLite, model adapters, and an isolated artifact store.

Scenarios include:

- chunked upload and missing-part resume;
- checksum mismatch;
- media validation failure;
- process restart during every major stage;
- normalization-file corruption and deterministic rebuild;
- crash after raw ASR commit but before transcript materialization;
- retry of rejected and failed ASR chunks;
- model crash and timeout;
- partial diarization availability;
- pause, resume, cancel, and retry;
- artifact regeneration after transcript correction;
- duplicate-source behavior.

Model adapters also support deterministic fakes so reliability tests do not require expensive inference.

### 2.4 End-to-end tests

Exercise the plugin, Worker, Manager, and a temporary Vault:

- same-Mac submission;
- remote private-network submission;
- client closes after upload;
- another authorized client publishes;
- Worker reboot and recovery;
- interrupted publication and lease takeover;
- note, transcript, JSON, and raw artifact verification;
- no overwrite of user-owned Markdown.

### 2.5 UI tests

- all job states have readable labels and next actions;
- progressive segments reconstruct after reconnect;
- provisional text is visually distinct;
- warnings remain visible without blocking unrelated navigation;
- keyboard-only operation;
- reduced-motion mode;
- narrow panes and long multilingual text;
- screen-reader names for controls, progress, and status.

## 3. Audio coverage matrix

The private test set should span:

| Dimension | Coverage |
| --- | --- |
| Format | WAV, MP3, M4A, FLAC, supported media containers |
| Duration | seconds, minutes, hours, and a long-running overnight case |
| Language | Mandarin, English, code-switching, names and technical vocabulary |
| Content | meeting, interview, course, speech, voice memo, generic |
| Speakers | one, two, overlapping speakers, roundtable |
| Quality | clean, room echo, background noise, low volume, clipping |
| Structure | long silence, music, applause, non-speech, abrupt ending |
| Metadata | correct date, misleading date, absent date, filename date |
| Network | stable, slow, disconnected, resumed, duplicate request |
| Resources | low disk, memory pressure, model missing, host restart |

Synthetic and openly licensed fixtures cover repository tests. Real personal recordings remain local and ignored by Git.

## 4. Transcript completeness gates

For each validated source:

- decoded duration and stream boundaries are recorded;
- processed ranges form a monotonic, non-overlapping timeline;
- all decodable ranges have an outcome;
- timestamps stay within media bounds;
- concatenated chunk boundaries do not silently remove content;
- ASR output ending early is detected even if the model reports success;
- unresolved intervals cause `partial`, not `complete`;
- raw ASR output exists before cleanup or summarization.
- normalized frame chunks are contiguous, non-overlapping, and end at the exact final frame;
- container-duration rounding cannot make visible progress exceed the verified source duration.
- every materialized chunk still references matching checksummed raw evidence;
- estimated transcript timing blocks the alignment exit gate;
- forced alignment preserves stable text and segment identity while revising only timing metadata;
- forced-alignment words must account for the stable text and remain monotonic and in bounds;
- private forced-alignment evidence is checksummed before metadata changes and replays after interruption;
- missing, stale, incomplete, or tampered forced-alignment evidence blocks diarization;
- forced alignment pauses before model work when the resource boundary is blocked;
- uncovered ranges are persisted without transcript text and block diarization;
- a complete report is idempotent across restart and advances exactly once.
- uncovered-range PCM evidence is anchored to the exact alignment report and normalized-audio checksum;
- only sufficiently long near-digital silence receives a definite classification;
- short or audible PCM remains unresolved, and changed normalized audio invalidates prior measurement assumptions.
- a source range truncated by the normalized PCM boundary remains unresolved even when all available samples are zero;
- only current default-policy definite silence can become a stable `non_speech` outcome;
- silence can be backfilled before or between existing text without changing stable segment IDs or cursors;
- stale alignment evidence, custom thresholds, audible PCM, and overlapping ranges cannot authorize materialization;
- alignment is refreshed after backfill so the remaining uncovered timeline is durable immediately.
- a human-reviewed outcome must match one complete current unresolved range;
- review keys are idempotent and cannot be rebound to another range or outcome;
- stale review evidence and overlapping stable segments cannot authorize materialization;
- reviewed `inaudible` accounts for time without claiming a complete transcript;
- interruption after a reviewed segment commit is repaired without duplicating the segment;
- routine review checkpoints and CLI output contain no transcript or free-form reviewer text.
- speech-activity candidates are pinned to immutable model revisions and fixed configurations;
- detector regions must be finite, ordered, non-overlapping, non-empty, and within normalized audio;
- VAD observations are bound to current gap/alignment evidence and never materialize stable outcomes;
- resource blocking occurs before VAD model invocation, and no-gap runs do not load the model;
- `no_speech_detected` remains an evaluation observation, not proof of non-speech or inaudibility.

The core automated assertion is:

```text
decodable timeline
= transcribed
+ explicitly inaudible
+ explicitly failed after retry
+ classified non-speech
```

Any uncovered interval fails the job quality gate.

## 5. Information-quality evaluation

### 5.1 Gold-standard set

After the implementation is ready, the project owner will provide representative real recordings for local-only testing.

For a small, high-value subset, create:

- a carefully reviewed reference transcript;
- speaker labels;
- key facts;
- decisions and action items;
- expected content type and traits;
- known ambiguities;
- claims that must not appear.

The gold-standard directory is excluded from Git and cloud processing.

### 5.2 Metrics

Possible quantitative measurements:

- word or character error rate;
- named-entity and terminology accuracy;
- diarization error rate;
- speaker-attributed word accuracy;
- decision and action-item recall;
- evidence precision;
- unsupported critical-claim count;
- unresolved-range accuracy.

Human review also scores usefulness, clarity, omission severity, and whether uncertainty is represented honestly.

### 5.3 Summary acceptance

A summary passes only when:

- all critical factual items link to evidence;
- every transcript chunk participated in hierarchical analysis;
- named owners, dates, amounts, and commitments match the evidence;
- disagreement is not rewritten as consensus;
- unknown intent is not presented as fact;
- no critical unsupported claim appears;
- the note remains useful at normal reading length.

Thresholds will be set from the first measured local baseline rather than invented before the models are evaluated.

## 6. Fault-injection plan

Automated tests interrupt the system:

- before and after a chunk commit;
- during upload assembly;
- during ASR, alignment, diarization, and summarization;
- while SQLite is busy;
- before artifact rename;
- after Vault write but before acknowledgement;
- while a publication lease expires;
- during service or host restart;
- as free disk crosses warning and blocking thresholds;
- under sustained memory pressure.

Every case verifies that valid completed work survives and that the UI reports an actionable state.

## 7. Performance and resource tests

Record, by model profile and hardware:

- real-time factor;
- time to first stable segment;
- memory high-water mark;
- swap growth;
- temporary disk amplification;
- model load time;
- reconnect recovery time;
- publication time.

Performance regressions do not justify dropping content. When resources are unsafe, the correct result is a visible safe pause.

## 8. Privacy and release tests

Before each release:

- scan tracked files and build artifacts for credentials and private paths;
- assert that routine logs omit transcript text;
- inspect diagnostic redaction;
- verify private fixture directories remain ignored;
- test device revocation;
- test path traversal and symlink escape;
- verify cloud calls cannot occur without explicit consent;
- inspect packaged licenses and third-party notices.

## 9. Test evidence

Each release candidate produces a local redacted report containing:

- version matrix;
- tested model profiles;
- hardware class;
- pass and fail counts;
- completeness and quality metrics;
- known limitations;
- manual review sign-off.

The public repository may receive sanitized aggregate results, never private audio or transcript content.
