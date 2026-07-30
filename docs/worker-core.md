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
- stable machine-readable errors;
- a developer CLI.

This is not yet the network Worker service. It does not run the ASR pipeline as a queued job, expose FastAPI, authenticate devices, or publish to a Vault.

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

## 3. SQLite layout

Schema version two contains five tables:

### 3.1 `jobs`

Stores:

- Worker job ID;
- Vault identity;
- source display name, size, and SHA-256;
- current state and revision;
- selected model profile;
- language and content-type hints;
- canonical JSON options;
- idempotency key and request fingerprint;
- latest safe error;
- created and updated timestamps.

It does not store a source absolute path, device credential, pairing token, or cloud key.

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

A stale caller receives `JOB_REVISION_CONFLICT` and must fetch a new snapshot. Concurrent creation and transition behavior is covered by tests using separate SQLite connections.

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

The future scheduler will inspect checkpoints and select the exact work unit to retry.

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

The first job estimate includes:

- source staging copy;
- decoded 16 kHz mono PCM;
- two additional working-audio equivalents;
- the larger of 256 MiB or 10% of source size for artifacts.

This estimate will be calibrated with real long recordings.

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

A corrupt persisted part has its receipt removed and returns the manifest to `uploading`, allowing only that part to be sent again. A whole-source checksum or media failure preserves all parts and records a stable safe error. No transcription state can use this source yet; verified-upload-to-job binding is the next boundary.

### 9.3 Restart recovery

If the Worker stops during assembly or media probing, startup recovery moves `verifying` back to `uploading`, preserves every accepted part, removes only Worker-owned incomplete assembly files, and allows completion to be retried.

## 10. Developer CLI

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
```

Additional development commands store upload parts, report missing parts, complete and media-verify uploads, list jobs, apply guarded transitions, read events, run recovery, and check database integrity.

The CLI is an engineering tool. The Obsidian plugin will eventually call the versioned Worker API rather than shell commands.

## 11. Test evidence

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
- CLI initialization, idempotency, listing, and stable errors.
- schema-one to schema-two migration;
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
- end-to-end synthetic M4A intake through the real local FFprobe binary.

A separate CLI integration run advanced a job to `transcribing`, closed the store, recovered it to `queued`, and verified all seven events and database integrity.

An additional CLI integration generated a one-second synthetic M4A, received it as an upload part, assembled it, verified its checksum, and accepted one audio stream with a one-second duration.

## 12. Next implementation boundary

The next layer will add:

1. bind only verified complete uploads to jobs;
2. a one-active-job scheduler;
3. ASR chunk execution using the existing probe integration;
4. progress snapshots and bounded event cursors;
5. safe pause at chunk boundaries;
6. FastAPI only after the core behavior is stable under integration tests.
