# Security, Privacy, and Recovery

## 1. Security objective

Speech Capture handles recordings that may contain private conversations, business information, and personal notes. Its default behavior keeps source audio, transcripts, model prompts, and results on devices controlled by the user.

Security controls must not depend on a particular Vault synchronization provider.

## 2. Trust boundaries

The system has four distinct trust boundaries:

- the submitting Obsidian device;
- the private network between client and Worker;
- the processing host and its local model services;
- the target Vault and its chosen synchronization system.

Synchronization makes files available to other devices, but does not authenticate Worker API requests.

## 3. Remote-access posture

V1 assumes a private network such as Tailscale.

- The Worker does not intentionally expose a public internet port.
- Public sharing features such as a tunnel or public funnel are outside the default setup.
- Remote connections use HTTPS and a per-device credential.
- Pairing is confirmed on the Worker host and can be revoked by device.
- A Worker restricts each device to approved Vault identities and operations.
- Routine Worker restart or upgrade preserves pairing.

A reinstall, explicit reset of security state, or loss of the processing host requires new pairing.

The implemented API foundation already enforces authentication and per-principal Vault allowlists for every private
resource and redacts request-validation input. Durable pairing stores only token digests in a private `0600`
security database; short-lived codes are attempt-limited and consumed once. Client-side OS-protected token storage
remains later Obsidian work. Worker-side credential rotation is implemented as a two-phase switch: preparing a
replacement does not revoke the current token, and activation atomically promotes the replacement so a lost HTTP
response cannot leave both credentials unusable.

## 4. Credential handling

- Pairing credentials stay in operating-system-protected storage.
- Hugging Face access tokens stay in the Worker Manager or service credential store.
- Optional cloud-provider keys stay outside the Vault.
- Credentials never appear in job manifests, Markdown properties, logs, diagnostic filenames, or repository fixtures.
- Revocation invalidates the affected device without rotating unrelated devices.
- Diagnostic exports replace stable identifiers with short-lived pseudonyms.

The implementation must document platform limitations if an Obsidian runtime cannot access the preferred native credential store directly.

## 5. File and path safety

- The plugin publishes only below a configured Vault-relative root.
- The Worker never accepts an arbitrary client-provided absolute output path.
- Path normalization rejects traversal, symlink escape, device paths, and unexpected mount changes.
- Source selection is read-only from the user's perspective.
- Temporary and final writes use atomic patterns.
- Existing files with unexpected hashes are preserved and surfaced as conflicts.
- Speech Capture never automatically cleans unrelated user files.

## 6. Cloud fallback

Cloud processing is disabled by default.

ASR and summary fallback have separate controls because they expose different data:

- ASR fallback may require source audio.
- Summary fallback needs only the selected transcript and context.

Before each cloud submission, the UI shows the provider, data categories, and purpose. Local failure never silently changes the privacy boundary.

Provider interfaces may exist before any provider is shipped.

## 7. Logging and diagnostics

Telemetry is off by default.

Local routine logs may contain:

- job and stage identifiers;
- durations and resource measurements;
- model and protocol versions;
- stable error codes;
- retry and recovery actions.

They must not contain transcript text, source bytes, prompts containing transcript material, credentials, full local paths, or original filenames when a pseudonym is sufficient.

The reconnect event feed follows the same boundary: it carries segment IDs, time ranges, outcomes, generations, speaker state, and text length, but transcript text is read only through the authorized bounded snapshot.

Normalized PCM and raw model-attempt JSON stay under the private Worker job directory with restrictive permissions, Worker-relative database paths, atomic writes, and SHA-256 verification. Routine attempt metadata exposes no transcript text or absolute path.

Diagnostic export is user initiated, previewable, redacted, and saved locally. Sending it anywhere is a separate user action.

## 8. Retention

The user's original audio is never deleted.

Worker-side copies follow these defaults:

| Data | Default retention |
| --- | --- |
| Successful source staging copy | 7 days after verified publication |
| Failed or partial source staging copy | retained until the user retries or explicitly removes it |
| Raw and structured artifacts | retained until verified publication, then according to a user-configurable recovery window |
| Runtime logs | size- and time-bounded local rotation |
| Model files | retained until explicit model removal |

Before deleting a Worker-owned copy, the Worker verifies that the target is inside its application-data directory and is not the user's original source.

Low disk space blocks new work and asks the user to choose a safe action. It does not trigger unannounced deletion.

## 9. Restart and pre-login behavior

The Worker and private-network agent should run as system services when pre-login processing is required.

There is an unavoidable disk-encryption boundary:

- when the encrypted system volume is unavailable after reboot, the Worker cannot start;
- after the disk is unlocked, the system service resumes without reopening Obsidian;
- when the disk is available without interactive login, the Worker can resume independently of the graphical session.

The Worker persists the queue and completed checkpoints before acknowledging state transitions.

## 10. Backup and loss scenarios

### 10.1 Processing host is reinstalled or lost

Vault-published packages remain available through the user's existing Vault backup or synchronization setup. Worker-only queued sources and unpublished artifacts are lost unless the Worker application-data directory was separately backed up.

Models can be downloaded again. Devices must pair with the replacement Worker.

### 10.2 Submitting laptop is lost

The lost device credential is revoked on the Worker. The Worker continues accepted jobs and retains results for another authorized publisher.

### 10.3 Vault sync provider changes

No Worker configuration changes are required if the same Vault is available locally to an authorized publisher. Only the Vault location or identity mapping may need confirmation.

### 10.4 Publication is interrupted

Atomic writes and artifact hashes prevent a half-written directory from being marked published. A new publisher can claim the task after the prior lease expires.

## 11. Update safety

- Plugin, Worker, Manager, protocol, schema, model, and prompt versions are recorded independently.
- Service updates wait for an idle boundary unless a security update explicitly requires a controlled interruption.
- Database and schema migrations create a recoverable backup first.
- Model activation is separate from model download.
- The previous working model remains available until the new profile passes a local health check.
- Rollback does not delete job evidence.

## 12. Security review gates

Before any friend-ready release:

- threat-model review;
- authorization and path-traversal tests;
- credential-storage review;
- dependency and secret scanning;
- clean-machine install and uninstall tests;
- remote pairing and revocation tests;
- recovery tests after restart, lost client, and lost Worker;
- verification that private test material never enters release artifacts.
