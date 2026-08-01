# Persistent Worker Core

## 1. Status

The Worker core now has an executable local foundation for:

- strict job-state transitions;
- SQLite job persistence;
- append-only state events;
- idempotent job creation;
- optimistic revision guards;
- private stage checkpoints;
- safe restart recovery;
- disk and memory preflight;
- idempotent upload manifests;
- resumable checksum-bound upload parts;
- atomic whole-source assembly and SHA-256 verification;
- FFprobe audio-stream and duration validation;
- verified-upload job binding;
- one-active-job scheduling with resource evidence;
- monotonic processing progress;
- stable transcript outcomes and one revision-guarded provisional tail;
- alignment and speaker-attribution revisions that preserve stable text;
- bounded reconnect snapshots and content-free update cursors;
- deterministic private 16 kHz mono PCM normalization;
- complete frame-based, energy-aware ASR chunk plans;
- immutable raw ASR attempts with checksummed private files;
- one-chunk-at-a-time local MLX execution, retry, replay, and safe resource pause;
- durable whole-transcript alignment, evidence, and timeline-readiness reports;
- a strict completeness gate before speaker diarization;
- checksummed, replayable forced alignment for stable estimated segments;
- conservative, checksummed PCM evidence for uncovered timeline ranges;
- evidence-bound definite-silence backfill and immediate alignment refresh;
- version-anchored explicit human review for exact unresolved ranges;
- stable machine-readable errors;
- a developer CLI.

This is not yet the network Worker service. It can execute or replay one local ASR chunk through backend tools, but it does not yet run a continuous background job loop, expose FastAPI, authenticate devices, diarize speakers, structure notes, or publish to a Vault.

## 2. Core invariants

1. Every job starts in `created` at revision zero.
2. Every committed state transition increments the revision exactly once.
3. Every revision has exactly one event.
4. Invalid state jumps do not modify the job or event history.
5. A caller must supply the revision it observed.
6. A repeated idempotent create returns the original job.
7. Reusing an idempotency key for different input is an explicit conflict.
8. Stage checkpoints survive pause, failure, cancellation, and Worker restart.
9. Routine job events never contain transcript text.
10. A job cannot start model work after a blocking resource preflight.
11. An accepted upload part cannot change checksum unless corruption recovery first invalidates its receipt.
12. A source becomes complete only after part, byte-length, whole-file checksum, and media checks pass.
13. Worker records and CLI responses never expose the absolute source-storage path.
14. A schedulable job references one complete verified upload with identical source metadata.
15. At most one heavy processing job is active on a Worker.
16. Every scheduler start decision persists the resource report used to make it.
17. Stable transcript outcomes commit in source-timeline order and cannot overlap.
18. Retrying an identical transcript commit key is idempotent; changing its content is a conflict.
19. Stable transcript text is not rewritten by later timing or speaker attribution.
20. Routine state and reconnect events never contain transcript text.
21. A snapshot is bounded, internally consistent, and carries the event cursor needed to reconnect.
22. Normalized audio is 16 kHz mono 16-bit PCM inside private Worker storage.
23. The persisted chunk plan accounts for every normalized frame exactly once.
24. A raw ASR attempt is written and checksummed before its text becomes a stable visible segment.
25. Retrying a completed attempt replays its raw evidence instead of invoking the model again.
26. Severe resource pressure pauses before the next chunk; it never discards prior raw attempts or segments.
27. A job cannot enter diarization until every planned chunk has matching durable raw evidence and a materialization checkpoint.
28. A job cannot enter diarization while transcribed timing is estimated, a source range lacks a stable outcome, or a failed range remains. An explicitly reviewed `inaudible` range may proceed while preserving partial transcript semantics.
29. Alignment checkpoints contain ranges, counts, and stable issue codes, but never transcript text.
30. Gap analysis may prove sufficiently long near-digital silence, but audible or uncertain PCM remains unresolved and cannot be silently labeled non-speech.
31. Only current gap evidence produced by the fixed conservative policy may authorize an aligned `non_speech` backfill.
32. Alignment backfill may append a stable segment sequence before or between existing timeline ranges, but it may never rewrite IDs, overlap another segment, or change transcript text.
33. Human review may account for one complete current unresolved range as `non_speech` or `inaudible`, but a review key can never be rebound and does not prove automatic classification quality.
34. A fallback segment may become `aligned` only while matching private forced-alignment evidence remains intact; the fallback never rewrites stable text or segment identity.

## 3. SQLite layout

Schema version five contains ten tables:

### 3.1 `jobs`

Stores:

- Worker job ID;
- Vault identity;
- optional verified source-upload reference;
- source display name, size, and SHA-256;
- current state and revision;
- selected model profile;
- language and content-type hints;
- canonical JSON options;
- idempotency key and request fingerprint;
- latest safe error;
- created and updated timestamps.

The production scheduling path accepts only jobs with a complete source-upload reference. Direct unbound jobs remain available to state-machine development tools but are never selected by the scheduler.

The table does not store a source absolute path, device credential, pairing token, or cloud key.

### 3.2 `job_events`

Stores one ordered event per job revision:

- global event sequence;
- job revision;
- event type;
- prior and target states;
- stable reason code;
- safe error facts;
- timestamp.

The unique `(job_id, revision)` constraint prevents two different histories for one revision.

### 3.3 `job_checkpoints`

Stores private processing payloads separately from routine events:

- stage and checkpoint key;
- checkpoint generation;
- canonical JSON payload;
- payload SHA-256;
- created and updated timestamps.

Writing the same payload is idempotent. Replacing it increments the checkpoint generation without changing the job revision.

Transcript text may exist inside this private table later. It is never copied into state events or routine progress logs.

### 3.4 `uploads`

Stores:

- Worker upload ID and Vault identity;
- display-only filename, media type, source byte size, and whole-file SHA-256;
- Worker-selected chunk size and part count;
- upload state and safe error;
- idempotency key and request fingerprint;
- detected media format, audio duration, and audio-stream count after verification;
- a Worker-relative source location, never an absolute path.

States are `uploading`, `verifying`, `complete`, and `failed`. A failed manifest can retry verification when the failure is recoverable, such as repairing a missing FFprobe runtime.

### 3.5 `upload_parts`

Stores one durable receipt per 1-based part number:

- exact byte length;
- SHA-256;
- creation and update timestamps.

Binary part data lives under the private Worker application-data directory. Database receipts contain no audio bytes.

### 3.6 `transcript_segments`

Stores one durable timeline outcome per committed range:

- monotonically assigned segment sequence and stable segment ID;
- idempotent commit key and immutable request fingerprint;
- non-overlapping start and end time;
- `transcribed`, `inaudible`, `non_speech`, or `failed` outcome;
- text only for `transcribed`;
- language and confidence when available;
- estimated or aligned timing state;
- pending, anonymous, confirmed, or unavailable speaker state;
- a revision number for timing and speaker metadata.

Text is stable after commit. Alignment and speaker attribution may revise metadata, but never rewrite the committed text.

### 3.7 `provisional_transcripts`

Stores at most one explicitly unstable tail per job. Every revision uses an optimistic generation guard. Committing an overlapping stable segment removes the tail atomically.

### 3.8 `job_progress`

Stores the latest monotonic active-job progress:

- processing stage;
- processed and total media milliseconds;
- stage progress;
- elapsed and optional estimated remaining time;
- diarization state;
- generation and payload hash for idempotency.

### 3.9 `job_updates`

Provides the bounded reconnect cursor used by future HTTP clients. It includes state, progress, segment-availability, provisional, timing, and speaker events. Payloads contain safe metadata such as time ranges, segment IDs, state, and text length, but never transcript text.

### 3.10 `asr_attempts`

Stores safe metadata for every immutable model attempt:

- zero-based normalized-audio chunk index and one-based attempt number;
- caller-owned attempt key and complete request fingerprint;
- `succeeded`, `rejected`, or `failed` state;
- model ID, frame range, and time range;
- language, finish reason, truncation flag, and elapsed time;
- Worker-relative raw JSON location and SHA-256;
- stable rejection or execution error code.

The raw model payload lives in a private `0600` file, not in routine events. Attempt numbers cannot have gaps, and an attempt key cannot silently change evidence.

## 4. Durability configuration

The current store uses:

- SQLite WAL journal mode;
- foreign-key enforcement;
- full synchronous durability;
- five-second busy timeout;
- `BEGIN IMMEDIATE` for mutations;
- a versioned schema;
- atomic transactions for job and event writes;
- database file mode `0600`;
- a newly created application-data directory mode `0700`.

The Worker Manager will own the final application-data directory. The developer CLI requires an explicit directory and does not invent a global location.

Upload parts and assembled sources use:

- private directories with mode `0700`;
- files created with mode `0600`;
- same-directory temporary files;
- file and directory `fsync`;
- atomic replace;
- generated upload IDs and part numbers rather than user-supplied path components;
- symbolic-link and resolved-path boundary checks.

Normalized audio and raw ASR attempts use the same private-directory, restrictive-permission, same-directory atomic-write, checksum, and path-boundary rules.

## 5. State machine

Normal transitions are allowlisted. Representative success flow:

```mermaid
flowchart LR
    Created --> Uploading --> Verifying --> Queued
    Queued --> Preprocessing --> Transcribing --> Aligning
    Aligning --> Diarizing --> Structuring
    Aligning --> Structuring
    Structuring --> QualityCheck["Quality check"]
    QualityCheck --> Processed --> Publishing --> Published
```

Operational branches include:

- active processing to `paused`, `waiting_user`, `partial`, `failed`, or `cancelled` where appropriate;
- `paused`, `waiting_user`, and `failed` back to `queued`;
- `partial` back to `queued` or forward to controlled publication;
- `publishing` back to `processed` when a publisher is unavailable;
- terminal `published` and `cancelled` states with no outgoing transition.

Transitions to `waiting_user`, `partial`, or `failed` require a stable error code. Safe pause may carry a resource error code but does not require one for a manual pause.

## 6. Idempotency and concurrency

Job creation is unique by `(vault_id, idempotency_key)`.

The Worker also hashes a canonical representation of the complete create request.

- Same key and same fingerprint: return the existing job.
- Same key and different fingerprint: return `IDEMPOTENCY_KEY_CONFLICT`.

State changes use optimistic concurrency:

```text
UPDATE jobs
SET revision = revision + 1, ...
WHERE job_id = ? AND revision = expected_revision
```

A stale caller receives `JOB_REVISION_CONFLICT` and must fetch a new snapshot. Concurrent creation and transition behavior is covered by tests using separate SQLite connections. Heavy-stage transitions also check for another active job inside the same immediate transaction.

## 7. Restart recovery

On Worker startup, interrupted active stages move to a safe boundary:

| Interrupted state | Recovered state | Meaning |
| --- | --- | --- |
| verifying | uploading | re-run whole-source checksum and decode verification |
| preprocessing | queued | restart the current preprocessing unit |
| transcribing | queued | retry only the active ASR chunk |
| aligning | queued | retry the active alignment unit |
| diarizing | queued | retry diarization |
| structuring | queued | rebuild from durable evidence |
| quality check | queued | rerun quality gates |
| publishing | processed | preserve artifacts and wait for a new publisher |

Paused, waiting, failed, partial, processed, published, uploading, queued, and created jobs are not silently changed by startup recovery.

Recovery:

- increments the job revision;
- records `job.recovered`;
- records reason `worker_restart`;
- leaves all checkpoints intact.

The scheduler inspects the recovered queue and takes a fresh resource snapshot before reclaiming work.

## 8. Resource preflight

The current policy evaluates current disk and memory before model work.

### 8.1 Disk

The reserve is the larger of:

- 20 GiB;
- 10% of the target volume.

The operation is blocked when:

```text
current free - estimated operation bytes < reserve
```

A warning is returned when projected free space is within 5 GiB of the reserve.

The intake estimate includes:

- source staging copy;
- decoded 16 kHz mono PCM;
- two additional working-audio equivalents;
- the larger of 256 MiB or 10% of source size for artifacts.

This estimate will be calibrated with real long recordings.

The scheduler does not count the already verified Worker source as a second staging copy. It estimates only additional normalized audio, working copies, and artifact headroom.

### 8.2 Memory

Initial profile minimums:

- accuracy profile: 16 GiB installed memory;
- speed profile: 8 GiB installed memory.

Current pressure blocks a new job below 2 GiB available or at 95% used. It warns below 6 GiB available, at 85% used, or when swap use reaches 4 GiB.

These are conservative starting values, not final universal thresholds. Sustained-pressure monitoring and safe pause during inference remain to be implemented.

### 8.3 Actionable result

Every issue includes:

- stable code;
- `warning` or `blocked`;
- safe explanation;
- recommended human action.

Speech Capture never deletes unrelated files in response.

## 9. Durable media intake

### 9.1 Manifest and chunk policy

The client declares source size, media type, and whole-file SHA-256. The Worker chooses an 8 MiB default part size and may increase it for very large files while remaining within the supported part-count and 64 MiB per-part limits.

Part numbers are 1-based. Each accepted part must:

- fall within the manifest's part range;
- have the exact expected length, including the shorter final part;
- match the client-declared part SHA-256;
- either be new or match the checksum already bound to that part number.

Parts can arrive out of order. Repeating an identical part is idempotent. A reconnecting client reads received-byte and part counts plus the exact missing part numbers.

### 9.2 Completion

Completion first verifies that every database receipt exists. The Worker then:

1. streams every part in order into a private temporary file;
2. rechecks each persisted part's length and SHA-256;
3. verifies assembled byte length and whole-file SHA-256;
4. runs FFprobe and requires at least one positive-duration audio stream;
5. atomically replaces the final Worker-owned source;
6. marks the manifest `complete`.

A corrupt persisted part has its receipt removed and returns the manifest to `uploading`, allowing only that part to be sent again. A whole-source checksum or media failure preserves all parts and records a stable safe error.

### 9.3 Restart recovery

If the Worker stops during assembly or media probing, startup recovery moves `verifying` back to `uploading`, preserves every accepted part, removes only Worker-owned incomplete assembly files, and allows completion to be retried.

## 10. Verified job binding and scheduling

### 10.1 Atomic source binding

Creating a production job from an upload requires:

- upload state `complete`;
- matching Vault ID, display name, byte length, and whole-file SHA-256;
- an existing Worker-owned source with the expected byte length.

The job stores the upload ID as a foreign key. One transaction creates the job and records the already completed intake path:

```text
revision 0  created
revision 1  uploading  — verified upload attached
revision 2  verifying  — prior verification reused
revision 3  queued     — source ready
```

Repeating the same idempotent request returns the original queued or later-state job without duplicating these events.

### 10.2 One-active-job scheduler

One scheduler pass:

1. returns `busy` when a heavy stage is already active;
2. selects the oldest queued job with a complete upload;
3. estimates additional processing disk without counting the staged source twice;
4. evaluates current disk, installed memory, memory pressure, and swap;
5. persists the complete resource report as a private scheduler checkpoint;
6. pauses a blocked job with `RESOURCE_PREFLIGHT_BLOCKED`;
7. atomically claims a safe job by moving it to `preprocessing`.

The heavy active set is `preprocessing`, `transcribing`, `aligning`, `diarizing`, `structuring`, and `quality_check`. The invariant is enforced inside SQLite transactions, including manual state transitions, so two scheduler connections cannot activate two jobs.

Warnings remain visible in the checkpoint but do not block a claim. If the verified source disappears or its byte length changes, the queued job becomes `failed` with a stable storage error instead of repeatedly attempting model work.

### 10.3 Restart behavior

An interrupted active job recovers to `queued`. Its resource checkpoint survives. A later scheduler pass takes a fresh resource snapshot before reclaiming it, so an old ready result cannot override current disk or memory pressure.

## 11. Progressive transcript and reconnect contract

### 11.1 Stable timeline outcomes

A Worker pipeline commits stable timeline outcomes only during `transcribing`. Commits:

- are ordered by source time;
- are non-overlapping and bounded by verified media duration;
- use a caller-owned commit key so retry after interruption cannot duplicate a segment;
- distinguish transcript text from inaudible, non-speech, and failed ranges;
- preserve a stable segment ID for later evidence links.

The job cannot advance successfully from `transcribing` to `aligning` while an unresolved provisional tail remains. Later metadata revisions align timestamps during `aligning` and add, confirm, or remove speaker attribution during `diarizing`. They use a segment revision guard and preserve the original stable text.

### 11.2 Provisional tail

Only `transcribing` may expose an unstable tail. There is at most one tail, its writes use an expected generation, and it cannot overlap stable content. The client must present it as provisional. It remains durable across Worker restart but is never confused with a committed segment.

### 11.3 Progress and snapshot

Progress cannot move backward within a processing stage. Processed media time and elapsed time are monotonic across the job. The bounded snapshot contains:

- current job state and revision;
- latest progress and diarization status;
- a page of stable segments;
- the provisional tail;
- latest scheduler resource report;
- latest event sequence.

Stable segments paginate independently from the event feed. A reconnecting client reads a consistent snapshot, renders its page, continues segment pagination if needed, and then requests updates after `latest_event_sequence`.

### 11.4 Update privacy

`job_updates` tells a client what changed but not what was said. For example, `transcript.segment_committed` carries the segment ID, time range, outcome, and text length. The client then refreshes the bounded snapshot to read authorized transcript content. This keeps routine event handling and diagnostics free of transcript text.

## 12. Normalization and durable ASR execution

### 12.1 Deterministic normalized audio

`preprocessing` converts the verified Worker source to:

- 16 kHz;
- mono;
- signed 16-bit little-endian PCM;
- metadata-free WAV;
- one Worker-owned atomic file with a persisted SHA-256.

The normalized duration must remain within the larger of 250 ms or 1% of the media-verified container duration. The original source remains unchanged.

### 12.2 Complete frame-based chunk plan

The initial policy uses a 30-second maximum chunk, five-second minimum tail, three-second backward boundary search, and 100 ms energy windows. The quietest candidate near the limit is selected without exceeding the maximum.

Chunk boundaries are stored as exact PCM frame offsets plus display milliseconds. Validation requires:

- zero-based ordered chunk indices;
- positive durations;
- no overlap or gap;
- no frame beyond the normalized file;
- final coverage ending at the exact final frame.

This plan is independent of later model-library chunking changes and can be replayed after restart.

### 12.3 Raw evidence before visible text

For each planned chunk, the Worker:

1. runs a fresh resource boundary check;
2. safely pauses before model work if blocked;
3. reads only the planned PCM frame range;
4. invokes the configured MLX Qwen3-ASR profile with forced alignment;
5. atomically stores the complete raw result;
6. records immutable checksummed attempt metadata;
7. rejects empty, truncated, discontinuous, or invalid-timestamp output;
8. commits aligned stable text segments only after raw evidence is durable;
9. updates progress and marks the chunk materialized.

A restart after raw-attempt commit but before segment materialization replays the raw file. It does not call the model again. A restart after some stable segments is also idempotent because their commit keys are deterministic.

### 12.4 Retry and partial outcome

Rejected output and model exceptions retain separate raw attempt records. The default limit is three attempts. Exhaustion commits the exact chunk range as failed and moves the job to `partial` with `ASR_CHUNK_RETRIES_EXHAUSTED`; earlier text remains readable.

When container and normalized PCM duration differ by codec rounding, visible segment endings and progress are clamped to the verified container duration. Exact normalized frame coverage remains preserved in the private plan and raw attempts.

`run-asr-all` calls the same single-chunk executor repeatedly until
transcription completes, a chunk becomes partial, resources block, or an
optional per-run chunk limit is reached. Each iteration is restart-safe: an
interrupted batch can be resumed with the same command, and chunks that already
have raw evidence replay without another model call.

### 12.5 Whole-transcript alignment and completeness gate

After all ASR chunks are materialized, `finalize-alignment` evaluates the complete
job rather than trusting the last successful model call. It verifies:

- every normalized-audio chunk has a matching successful immutable attempt;
- every materialization checkpoint still references existing stable segments;
- each referenced raw attempt still passes its file checksum;
- every transcribed stable segment has aligned rather than estimated timing;
- stable outcomes are non-overlapping and account for the full verified source;
- no `inaudible` or `failed` outcome is being presented as complete;
- a fully accounted timeline containing reviewed `inaudible` ranges may proceed
  to diarization while retaining `transcript_complete = false`, but a `failed`
  range still blocks the exit gate.

The resulting `transcript_alignment_report` checkpoint is deterministic and
contains only counts, durations, ranges, booleans, and stable issue codes. It
does not contain transcript text. Uncovered ranges, estimated timing, and failed
outcomes keep the job in `aligning`. A fully verified and fully accounted report
advances the job to `diarizing`; reviewed `inaudible` ranges preserve partial
transcript semantics without blocking that transition. Calling the finalizer
again after restart returns the same report without a second model call or a
duplicate state transition.

### 12.6 Conservative PCM gap evidence

While a job remains in `aligning`, `analyze-gaps` reads the exact unresolved
ranges from the durable alignment report and measures them against the
checksummed normalized WAV. The resulting `gap_audio_evidence` checkpoint stores:

- source and PCM frame boundaries;
- duration, peak amplitude, RMS, and nonzero-sample ratio;
- the ratio of quiet measurement windows;
- either `definite_silence` or `unresolved`;
- one stable reason code;
- the source alignment-report generation and payload SHA-256.

Only a range at least 100 ms long whose every PCM sample stays at or below the
default near-digital-silence threshold is classified as definite silence.
Short, audible, unavailable, or otherwise uncertain PCM remains unresolved.
The report contains no transcript text, filenames, or absolute paths. Repeating
the same analysis is idempotent; revising the source alignment report creates a
new anchored gap-evidence generation.

### 12.7 Definite-silence timeline backfill

`materialize-silence` reruns gap analysis with the fixed 20 ms window, 100 ms
minimum duration, and near-digital peak threshold of 8. Diagnostic runs with
custom thresholds cannot authorize timeline changes.

For every current `definite_silence` range, the Worker:

1. verifies the current gap checkpoint generation and SHA-256;
2. verifies that it still references the current alignment report;
3. revalidates the exact range, PCM frame count, peak, quiet-window ratio,
   classification, and reason code;
4. proves that the complete source range maps to available PCM frames;
5. rejects any overlap with existing stable outcomes;
6. appends an aligned `non_speech` segment with no text or speaker attribution;
7. persists a range-specific evidence checkpoint linking the segment to the
   gap report, alignment report, normalized WAV checksum, and PCM measurements;
8. reruns whole-transcript alignment immediately.

This dedicated backfill path can insert source-time ranges before or between
existing text without changing existing segment IDs or sequence cursors.
Retries before alignment refresh reuse the same commit key and evidence
fingerprint. A crash after segment insertion but before its evidence checkpoint
or alignment refresh is repaired idempotently by the next invocation.

### 12.8 Explicit reviewed-gap outcomes

`review-gap` provides a separate strong-evidence path when a person has listened
to one unresolved range. It accepts only:

- one opaque review key;
- the exact start and end of one complete range in the current alignment report;
- `non_speech` or `inaudible`.

The Worker persists an `explicit_human_review` checkpoint bound to the current
alignment generation and payload SHA-256. It stores a stable reason code, not
reviewer identity or free-form text. A dedicated store transaction revalidates
the checkpoint and current report, rejects overlaps, appends an aligned
text-free segment, and writes a separate materialization checkpoint before
rerunning whole-transcript alignment.

Repeating the same review key and decision returns the original segment.
Changing the range or outcome is an explicit conflict. A reviewed `inaudible`
range accounts for source time and may proceed to diarization, but keeps
`transcript_complete` false so later artifacts remain explicitly partial. This
path does not classify audible content automatically and does not treat an
empty ASR result as review evidence.

### 12.9 Controlled forced-alignment fallback

`force-align-next` starts from the current whole-transcript report and processes
at most one stable transcribed segment with `estimated` timing. The command:

1. verifies that the segment appears as `UNALIGNED_TRANSCRIBED_SEGMENT`;
2. requires stable language metadata or a job language hint;
3. verifies the normalized WAV plan and runs a fresh resource boundary check;
4. passes only the segment's exact PCM range and unchanged stable text to the
   pinned `Qwen/Qwen3-ForcedAligner-0.6B`;
5. rejects missing words, text additions or omissions, non-finite timing,
   backwards movement, overlap, out-of-bounds timing, and zero-duration outer
   results;
6. atomically writes the private word result with `0600` permissions;
7. persists a safe checkpoint bound to the pre-update alignment report,
   segment revision and text SHA-256, normalized-audio SHA-256, model, frame
   range, and raw evidence SHA-256;
8. updates only start, end, timing status, and segment revision;
9. reruns whole-transcript alignment immediately.

Segment ID, commit key, text, language, confidence, and speaker metadata remain
unchanged. If a crash occurs after the evidence checkpoint but before metadata
revision, the next call replays the private result without loading the model
again. Finalization rechecks the raw file and its semantic binding. A manually
changed `aligned` flag without evidence, missing evidence, or later checksum
failure produces `MISSING_FORCED_ALIGNMENT_EVIDENCE` and cannot pass the
diarization gate.

### 12.10 Revision-pinned speech-activity evaluation evidence

`analyze-speech-activity` provides an evidence-only boundary for evaluating
audible unresolved ranges with `pyannote.audio`. It deliberately does not
implement automatic gap classification.

The command:

1. reruns conservative gap analysis and selects only ranges still marked
   `unresolved`;
2. requires a full immutable commit SHA for `pyannote/segmentation`;
3. records the pyannote runtime version and fixed DIHARD3 VAD configuration;
4. runs a fresh resource boundary check before loading or invoking the model;
5. validates that every returned speech region is finite, ordered,
   non-overlapping, non-empty, and inside the normalized audio;
6. intersects speech regions with exact current gap PCM ranges;
7. persists timing-only observations bound to the gap report, alignment report,
   and normalized-audio checksum.

The report distinguishes `speech_detected` from `no_speech_detected`, but both
the report and every range state `materialization_authorized: false`. Absence of
VAD speech is not yet proof of `non_speech`, and VAD cannot by itself identify
`inaudible`. No stable transcript segment or job state changes. Model failure
cannot leave a partial evidence report, and resource pressure safely pauses
before the detector runs.

### 12.11 Private VAD gold-set evaluation

`speech-capture-vad-probe` evaluates the same revision-pinned detector against
a local manifest stored under `test-data-private/`; its cache and report must
remain inside the manifest directory. Each sample uses an opaque ID,
a relative audio path, and ordered, non-overlapping millisecond ranges labeled
only as `speech` or `non_speech`. Unlabeled time is excluded from scoring.

The probe normalizes each source in memory to 16 kHz mono PCM, rejects path
escape, symlinked audio, changed source bytes, excessive duration, out-of-bounds
labels, and invalid detector regions. It reports duration-weighted:

- speech recall and speech miss rate;
- non-speech specificity and false-speech rate;
- true-positive, false-negative, false-positive, and true-negative durations;
- the number of samples containing any missed speech or false speech.

No default quality thresholds are embedded. An acceptance result remains
unapproved unless the owner supplies both rate limits and both minimum labeled
durations. All four policy values are reported verbatim. Even a passing policy
sets `automatic_materialization_authorized: false`; benchmark success does not
silently create a production decision rule.

The private report is written atomically with `0600` permissions. It contains
source hashes and opaque IDs, not audio paths, filenames, transcript text, or
model credentials.

## 13. Developer CLI

From `services/speech-worker/`:

```bash
uv run speech-capture-worker init \
  --data-dir runtime/dev-worker

uv run speech-capture-worker preflight \
  --storage-path runtime/dev-worker \
  --profile accuracy \
  --source-size-bytes 536870912 \
  --duration-sec 3600

uv run speech-capture-worker create-job \
  --data-dir runtime/dev-worker \
  --vault-id vault_primary \
  --source-name meeting.m4a \
  --source-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --source-size-bytes 536870912 \
  --idempotency-key local-test-001

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

uv run speech-capture-worker finalize-alignment \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker analyze-gaps \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker force-align-next \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker review-gap \
  --data-dir runtime/dev-worker \
  job_example \
  --review-key review-0001 \
  --start-ms 12500 \
  --end-ms 13900 \
  --outcome inaudible

uv run speech-capture-worker analyze-speech-activity \
  --data-dir runtime/dev-worker \
  job_example \
  --model-revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

uv run speech-capture-worker materialize-silence \
  --data-dir runtime/dev-worker \
  job_example

uv run speech-capture-worker list-asr-attempts \
  --data-dir runtime/dev-worker \
  job_example
```

Additional development commands store upload parts, report missing parts, complete and media-verify uploads, list jobs, apply guarded transitions, record progress, revise a provisional tail, commit stable timeline outcomes, update alignment or speaker metadata, read cursors, run recovery, and check database integrity.

The CLI is an engineering tool. The Obsidian plugin will eventually call the versioned Worker API rather than shell commands.

## 14. Test evidence

The Worker package currently tests:

- every state has explicit outgoing rules;
- valid success paths and invalid jumps;
- terminal states;
- request and path validation;
- idempotent creation and conflicts;
- stale revision rejection;
- event ordering;
- reopen persistence;
- checkpoint idempotency and generations;
- recovery with checkpoint preservation;
- publication recovery;
- concurrent identical creation across two database connections;
- database permissions and integrity;
- disk ready, warning, and blocked states;
- memory and swap warning and blocked states;
- model-profile memory minimums;
- CLI initialization, idempotency, listing, and stable errors;
- schema-one through schema-five migration and state-event backfill;
- idempotent upload creation and manifest conflicts;
- out-of-order part receipt and exact resume status;
- part checksum, size, number, and replacement conflicts;
- incomplete-upload reporting;
- whole-source checksum and FFprobe gates;
- corrupt persisted-part recovery;
- interrupted verification recovery;
- concurrent identical upload creation;
- upload storage permissions and symbolic-link escape rejection;
- redaction of FFprobe paths and diagnostic text;
- end-to-end synthetic M4A intake through the real local FFprobe binary;
- incomplete-upload scheduling rejection;
- atomic verified-source job history and metadata matching;
- exclusion of unbound developer jobs;
- ready, warning, and blocked scheduler decisions;
- resource-checkpoint persistence;
- missing verified-source failure;
- one-winner scheduling across two SQLite connections;
- restart recovery and safe scheduler reclaim.
- idempotent stable-segment commits and commit-key conflicts;
- non-overlapping, media-bounded, timeline-ordered outcomes;
- explicit non-speech and failed-range persistence;
- provisional generation conflicts and stable-commit clearing;
- text-preserving alignment and speaker revisions;
- monotonic progress and reopen persistence;
- bounded segment pagination and reconnect event cursors;
- transcript-text exclusion from routine update payloads;
- progressive preview preservation through restart recovery;
- CLI snapshot and update-feed reconstruction.
- deterministic PCM normalization and normalized-file recovery;
- exact frame coverage and low-energy chunk-boundary selection;
- raw-attempt idempotency, immutability, permissions, checksum verification, and concurrent commit;
- succeeded-attempt replay without a second model call;
- rejected-result retry and raw-evidence retention;
- retry exhaustion with an explicit failed range and `partial` state;
- safe resource pause before the next model call;
- container-versus-PCM duration clamping;
- real local MLX Qwen3-ASR execution on synthetic Chinese speech through aligned stable text.
- synthetic multi-chunk end-to-end intake through transcription completion and
  alignment advancement to `diarizing`, idempotent across Worker restart.
- revision-pinned speaker-diarization adapter with anonymous attribution,
  safe degradation to `unavailable`, and restart-idempotent evidence.
- content-type classification and bounded-batch evidence-linked extraction
  with unsupported-finding accounting and restart-idempotent evidence.
- deterministic backend artifact package with raw transcript, evidence
  transcript, structured record, human note, and a checksummed manifest.
- complete aligned transcript advancement to `diarizing`;
- durable uncovered-range reporting without transcript text;
- estimated-timing and missing-evidence blocking;
- alignment-finalization idempotency across Worker restart;
- machine-readable `finalize-alignment` CLI output.
- stable-text-preserving forced alignment of estimated segments;
- rejection of incomplete, non-monotonic, and out-of-bounds word evidence;
- resource blocking before forced-aligner model work;
- private evidence permissions, checksum validation, and restart replay;
- missing or tampered forced evidence blocking the alignment exit gate;
- machine-readable `force-align-next` output without transcript text.
- conservative definite-silence classification and short-gap rejection;
- audible-PCM preservation as unresolved evidence;
- alignment-report validation, generation anchoring, and idempotent replay;
- normalized-audio tamper rejection;
- machine-readable `analyze-gaps` CLI output.
- evidence-bound silence backfill before and between existing text;
- rejection of audible, stale, custom-policy, and overlapping evidence;
- rejection of source ranges not fully backed by available PCM frames;
- range-specific materialization checkpoints and idempotent retry;
- immediate whole-transcript alignment refresh;
- successful advancement to `diarizing` when proven silence is the final gap;
- machine-readable `materialize-silence` CLI output.
- exact-range human-review enforcement and stale-report rejection;
- immutable review-key behavior for range and outcome decisions;
- reviewed `inaudible` timeline accounting without a false complete result;
- reviewed-segment interruption repair and machine-readable `review-gap` output.
- immutable VAD model revision and fixed-configuration identity;
- finite, ordered, non-overlapping, in-bounds VAD region validation;
- current gap/alignment/normalized-audio evidence anchoring;
- resource blocking before VAD model invocation and no model load for empty gaps;
- explicit rejection of automatic materialization from either VAD observation.
- strict private gold-manifest schema, path containment, and label validation;
- duration-weighted VAD confusion metrics and aggregate/sample accounting;
- all-or-none owner-supplied acceptance policy with no invented defaults;
- path-free, transcript-free, `0600` atomic evaluation reports;
- passing evaluation still cannot authorize stable timeline materialization.

A separate CLI integration run advanced a job to `transcribing`, closed the store, recovered it to `queued`, and verified all seven events and database integrity.

An additional CLI integration generated a one-second synthetic M4A, received it as an upload part, assembled it, verified its checksum, and accepted one audio stream with a one-second duration.

The scheduler integration continued that source into a bound revision-three queued job, persisted a ready resource report, and atomically claimed it into `preprocessing`.

## 15. Next implementation boundary

The next layer will add:

1. atomic Vault publication with leases, retries, and verified package hashes;
2. add FastAPI only after the core behavior is stable under integration tests.

Continuous multi-chunk ASR batches are now available through `run-asr-all`; a
full packaged background stage loop will still be needed for the network
service. Anonymous speaker attribution is implemented, covered by
deterministic-engine tests, and validated on a real 60-second excerpt with
three anonymous speakers. Content classification and evidence-linked
extraction are implemented with deterministic-engine tests; real Ollama model
acceptance is pending download confirmation.
