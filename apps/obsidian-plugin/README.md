# Speech Capture Obsidian Plugin

Status: design only; no installable build exists yet.

The plugin is the client and Vault publisher for Speech Capture. It will:

- submit Vault or external audio to a configured Worker;
- show resumable upload and durable job progress;
- display progressive transcript segments;
- surface disk, memory, model, and connection warnings;
- review speakers and transcript corrections;
- publish verified artifact packages atomically;
- protect user-authored note sections;
- support multiple Workers and Vault-specific defaults.

Large model inference does not run inside the Obsidian process.

Planned implementation: TypeScript, Obsidian API, shared generated protocol types, and `pnpm`.

See the root [product requirements](../../docs/product-requirements.md), [architecture](../../docs/architecture.md), and [UI direction](../../docs/ui-direction.md).
