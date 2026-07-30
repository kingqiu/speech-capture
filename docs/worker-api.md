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

## 7. Job snapshot

A job snapshot is bounded and contains:

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

## 8. Event stream

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

## 9. Error model

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
DISK_RESERVE_TOO_LOW
MEMORY_PRESSURE_PAUSED
MODEL_NOT_READY
DIARIZATION_AUTH_REQUIRED
ASR_TRUNCATED
WORKER_VERSION_INCOMPATIBLE
PUBLICATION_TARGET_UNAVAILABLE
PUBLICATION_CONFLICT
```

Error payloads never include secrets or transcript content.

## 10. Compatibility

The plugin and Worker exchange:

- protocol version range;
- artifact schema version range;
- supported features;
- model capabilities.

A compatible older feature set may continue. An incompatible client is blocked before upload with a clear upgrade instruction.

## 11. Provider independence

No endpoint assumes Google Drive or another sync provider.

An optional synchronized-folder transport can produce and consume the same job manifests, but it is an adapter around the core job protocol rather than a different processing model.
