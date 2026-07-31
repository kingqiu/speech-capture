# Worker API Direction

## 1. Purpose

The Worker API is developed as part of Speech Capture. It is not a Google Drive, Obsidian, Ollama, or cloud-provider API.

It gives the plugin one consistent interface for local and remote processing while hiding model-process details.

## 2. Design constraints

- Versioned under `/v1`.
- HTTPS for remote use; loopback is allowed locally.
- Asynchronous jobs for long audio.
- Resumable uploads.
- Idempotent mutations.
- Durable event history.
- Explicit capability discovery.
- Device and Vault authorization.
- No arbitrary absolute-path access.
- Bounded request and response sizes.
- No transcript content in routine logs.

## 3. Planned resources

The exact OpenAPI schema will be produced during implementation. The design baseline reserves these resource groups:

```text
GET    /v1/health
GET    /v1/capabilities

POST   /v1/pairing/sessions
POST   /v1/pairing/confirm
GET    /v1/devices
DELETE /v1/devices/{device_id}

POST   /v1/uploads
GET    /v1/uploads/{upload_id}
PUT    /v1/uploads/{upload_id}/parts/{part_number}
POST   /v1/uploads/{upload_id}/complete

POST   /v1/jobs
GET    /v1/jobs
GET    /v1/jobs/{job_id}
POST   /v1/jobs/{job_id}/pause
POST   /v1/jobs/{job_id}/resume
POST   /v1/jobs/{job_id}/cancel
POST   /v1/jobs/{job_id}/retry

GET    /v1/jobs/{job_id}/snapshot
GET    /v1/jobs/{job_id}/events
GET    /v1/jobs/{job_id}/artifacts

POST   /v1/jobs/{job_id}/publication-claims
POST   /v1/jobs/{job_id}/publication-acknowledgements

GET    /v1/models
POST   /v1/models/{model_id}/download
POST   /v1/models/{model_id}/activate

GET    /v1/diagnostics/summary
POST   /v1/diagnostics/export
```

## 4. Authentication

### Local

Local calls still use an application credential. Loopback is not treated as proof of identity.

### Remote

Remote access requires:

1. private-network reachability;
2. HTTPS transport;
3. a paired-device credential;
4. an allowed Vault identity.

Pairing creates a per-device revocable credential. Restarting or upgrading the Worker does not require pairing again. Reinstalling or deleting Worker security state does.

Credentials are not stored in the synchronized Vault.

## 5. Idempotency

Every mutating request that may be retried includes an idempotency key.

Examples:

- upload creation;
- part upload;
- upload completion;
- job creation;
- retry creation;
- publication claim;
- publication acknowledgement.

The Worker returns the existing resource for a repeated successful request rather than creating a duplicate.

## 6. Upload contract

An upload manifest includes:

- source filename for display only;
- byte size;
- media type;
- whole-file checksum;
- optional media metadata;
- chunk size selected by the Worker;
- Vault and device identity.

The Worker records every accepted chunk checksum. Completion fails if any chunk is missing or if the assembled whole-file checksum differs.

The implemented core contract further establishes:

- part numbers are 1-based;
- the default part size is 8 MiB, with a Worker-selected increase for very large sources;
- the final part has the exact remaining length;
- identical part retries are idempotent;
- a part number cannot silently change checksum;
- upload status reports received bytes, received part count, and exact missing part numbers;
- persisted parts are rechecked during assembly rather than trusting database receipts alone;
- `complete` requires a positive-duration audio stream reported by FFprobe;
- assembled sources are atomically installed in private Worker storage;
- absolute Worker paths are not returned to clients.

Upload states are `uploading`, `verifying`, `complete`, and `failed`. An interrupted `verifying` state recovers to `uploading` while accepted parts remain valid.

## 7. Verified job creation and scheduling

The implemented core job contract requires a complete upload reference before a job can enter the real processing queue.

- The job's Vault ID, filename, byte length, and SHA-256 must match the upload.
- The private Worker source must still exist with the expected length.
- Creation records the complete intake path through ordered job revisions and finishes at `queued`.
- An idempotent retry returns the same job and does not duplicate intake events.
- Unbound developer jobs are excluded from scheduling.
- The Worker permits one active heavy-processing job by default.
- Every scheduler pass runs a fresh resource preflight and persists its evidence.
- Blocking resource pressure safely pauses the queued job; warnings remain visible and allow processing.

This is currently a core and CLI contract. The corresponding HTTP request and response schemas will be added with FastAPI.

### 7.1 Durable local ASR boundary

The implemented core can now continue a claimed `preprocessing` job through deterministic audio preparation and one restart-safe ASR chunk per call.

- The verified source is normalized privately to 16 kHz mono 16-bit PCM.
- A versioned energy-aware plan accounts for every normalized frame exactly once.
- Every model attempt writes immutable checksummed raw JSON before visible transcript segments.
- Empty, truncated, discontinuous, or invalid-timestamp results are retained as rejected attempts and retried.
- A succeeded raw attempt can be replayed after restart without invoking the model again.
- Resource pressure is checked before every new model call and can safely pause the job.
- Retry exhaustion records the exact failed range and produces `partial`, never a false complete state.

Raw payload locations and content are not part of routine job or event responses. Future artifact authorization will govern raw evidence download.

### 7.2 Durable alignment exit gate

The implemented core now creates a private whole-transcript alignment report
before entering diarization. The report independently states whether:

- all planned chunks have matching immutable raw attempts and materialization records;
- every transcribed segment has aligned timing;
- stable outcomes account for the full verified source timeline;
- the transcript contains any `inaudible` or `failed` range;
- the job is safe to advance to speaker diarization.

The report exposes only safe counts, durations, ranges, and issue codes. It does
not expose transcript text through routine events or diagnostics. Estimated
timing, missing evidence, uncovered ranges, and partial outcomes keep the job
out of diarization.

### 7.3 Conservative uncovered-range evidence

The core developer boundary can analyze the unresolved ranges in the latest
alignment report against the private normalized PCM. The persisted report is
anchored to the alignment-report generation and checksum and contains only
range boundaries, aggregate amplitude measurements, stable classifications,
and reason codes.

Only sufficiently long near-digital silence is reported as
`definite_silence`. Audible, short, missing, or uncertain PCM is reported as
`unresolved`; it is not assumed to be non-speech. This command does not expose
transcript text, change stable transcript outcomes, or advance the job.

### 7.4 Definite-silence timeline materialization

The core can backfill only default-policy `definite_silence` as an aligned
`non_speech` stable timeline outcome while the job is in `aligning`.

Before each insertion, the store verifies that:

- the exact range is marked `definite_silence` in the current gap checkpoint;
- the gap checkpoint uses the fixed conservative materialization policy;
- the gap checkpoint still references the current alignment report;
- the complete source range maps to available PCM frames;
- the inserted range does not overlap any stable segment.

Backfilled ranges may appear before or between previously committed transcript
segments. Their stable segment sequence remains append-only, while timeline
consumers order them by source time. Each range receives a private
materialization checkpoint linking the segment to the gap report, alignment
report, normalized-audio checksum, and aggregate PCM evidence. Alignment is
rerun after insertion so remaining unresolved ranges and stage readiness are
immediately current.

### 7.5 Explicit reviewed-gap materialization

The core developer boundary can record a human-reviewed decision for one exact
unresolved range as either aligned `non_speech` or aligned `inaudible`.

The request carries an opaque review idempotency key, exact range boundaries,
and the selected outcome. The Worker does not store reviewer identity or
free-form notes. Before insertion it verifies that the complete range occurs
exactly once in the current alignment report and binds the review checkpoint to
that report's generation and payload SHA-256.

A review key cannot later be rebound to another range or outcome. Per-range
evidence and materialization checkpoints make retries and interruption repair
idempotent. Alignment is refreshed immediately. An `inaudible` decision
accounts for the timeline but keeps transcript completeness false. This
boundary is explicit human evidence, not automatic audible-content
classification.

### 7.6 Controlled forced-alignment fallback

The core developer boundary can process the next stable transcribed segment
whose timing remains `estimated`. It does not retranscribe or replace text.

Before model work, the Worker requires the current alignment report, exact
normalized-audio plan, unchanged segment revision, and segment language or job
language hint. It performs a fresh resource boundary check and runs the pinned
`Qwen/Qwen3-ForcedAligner-0.6B` over only that segment's PCM and stable text.

The returned word units must:

- account for the normalized stable text without additions or omissions;
- be finite, monotonic, non-overlapping, and inside the original segment range;
- produce a positive outer range.

The private word result is atomically stored and checksummed before the stable
segment metadata changes. The Worker preserves segment ID, commit key, text,
language, and speaker state; only start, end, timing status, and revision
change. A durable checkpoint binds the evidence to the source alignment report,
segment revision and text hash, normalized-audio checksum, model, frame range,
and raw result checksum.

After interruption, durable evidence is replayed without another model call.
Whole-transcript finalization revalidates the private file; a manual `aligned`
flag without matching evidence, or later evidence tampering, blocks
diarization.

### 7.7 Evidence-only speech-activity evaluation

The developer boundary can run a revision-pinned `pyannote/segmentation` VAD
candidate over the normalized audio and persist timing-only observations for
the exact ranges that remain unresolved.

The report binds detector and model versions to the current gap checkpoint,
alignment checkpoint, and normalized-audio checksum. Returned regions must be
finite, ordered, non-overlapping, and in bounds. Resource blocking happens
before model work. The result never changes stable transcript outcomes or job
state, and `no_speech_detected` is explicitly not treated as proof of
`non_speech` or `inaudible`. Automatic materialization remains disabled until a
representative labeled evaluation establishes an accepted decision policy.

## 8. Job snapshot

The implemented core snapshot is an internally consistent, bounded read containing:

- identity and current revision;
- stage and status;
- progress by media duration and stage;
- active model profile;
- resource warnings;
- stable transcript segments;
- provisional tail;
- speaker-label status;
- actionable error or waiting reason;
- latest event cursor;
- processed and publication state.

Large artifacts are downloaded separately.

Stable segments use independent `after_segment_sequence` and `segment_limit` pagination. The current core limit is 500 segments per snapshot page. The response returns `next_after_segment_sequence`, `has_more_segments`, and `latest_event_sequence`.

Committed timeline outcomes are `transcribed`, `inaudible`, `non_speech`, or `failed`. Only `transcribed` carries text. Segment text stays stable while alignment and speaker metadata use guarded revisions.

## 9. Event stream

Each event has:

- monotonically increasing sequence number;
- event type;
- job revision;
- timestamp;
- bounded payload.

Representative event types:

```text
job.stage_changed
job.progress
resource.warning
resource.safe_paused
transcript.segment_committed
transcript.provisional_revised
speaker.attribution_updated
artifact.ready
publication.state_changed
job.waiting_user
job.failed
```

Clients reconnect with the last acknowledged sequence number. If history has been compacted, the Worker returns a fresh snapshot and cursor.

The implemented SQLite update feed is bounded to at most 1,000 events per request and returns whether more events remain. State transitions and progressive updates share one monotonically increasing cursor.

Routine update payloads do not contain transcript text. Segment and provisional events carry IDs, time ranges, generations, outcome, and text length; an authorized client reads actual text from the snapshot. This preserves reconnect behavior without leaking spoken content into routine event handling or diagnostics.

## 10. Error model

Errors contain:

- stable machine-readable code;
- safe user-facing message;
- retryability;
- recommended action;
- related job or resource ID;
- redacted diagnostic reference.

Examples:

```text
UPLOAD_CHECKSUM_MISMATCH
SOURCE_UNDECODABLE
AUDIO_NORMALIZATION_UNAVAILABLE
AUDIO_NORMALIZATION_FAILED
NORMALIZED_AUDIO_INVALID
DISK_RESERVE_TOO_LOW
MEMORY_PRESSURE_PAUSED
MODEL_NOT_READY
DIARIZATION_AUTH_REQUIRED
ASR_TRUNCATED
ASR_ATTEMPT_CONFLICT
ASR_RESULT_REJECTED
ASR_CHUNK_RETRIES_EXHAUSTED
TRANSCRIPT_COMMIT_CONFLICT
TRANSCRIPT_REVISION_CONFLICT
WORKER_VERSION_INCOMPATIBLE
PUBLICATION_TARGET_UNAVAILABLE
PUBLICATION_CONFLICT
```

Error payloads never include secrets or transcript content.

## 11. Compatibility

The plugin and Worker exchange:

- protocol version range;
- artifact schema version range;
- supported features;
- model capabilities.

A compatible older feature set may continue. An incompatible client is blocked before upload with a clear upgrade instruction.

## 12. Provider independence

No endpoint assumes Google Drive or another sync provider.

An optional synchronized-folder transport can produce and consume the same job manifests, but it is an adapter around the core job protocol rather than a different processing model.
