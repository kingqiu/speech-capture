# Speech Capture Obsidian Plugin

Status: the Stage I local workflow has passed two separately authorized real-recording acceptances, including a
multi-speaker meeting. Stage J remote personal Alpha is in progress. Remote Worker configuration, private-HTTPS
validation, per-Worker Vault authorization, and legacy settings migration are implemented; real laptop-to-home-Mac
private-network health, pairing persistence, resumable two-part upload, detached home processing, remote reconnect,
and atomic Vault publication have passed. Large-file upload hardening is now in progress before a formal Vault is
used.

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

Implementation: TypeScript, Obsidian API, shared generated protocol types, and `pnpm`.

Development commands from this directory:

```bash
pnpm install
pnpm test
pnpm build
```

`main.js` is a local build output and remains ignored by Git. Worker bearer credentials use Obsidian's
`secretStorage`; `data.json` contains only non-secret preferences and Worker endpoint metadata.

## Personal Alpha package

Build the desktop-only personal Alpha package on macOS:

```bash
pnpm package:alpha
```

The command runs the production build and writes a generated archive plus SHA-256 file under `dist/`. The archive
contains exactly `speech-capture/main.js`, `speech-capture/manifest.json`, and `speech-capture/styles.css`; it never
includes plugin settings, Vault IDs, credentials, audio, transcripts, Notes, databases, models, or source maps.

For a manual personal-Alpha installation, extract the `speech-capture` directory into the target Vault's
`.obsidian/plugins/` directory, then enable **Speech Capture** under Obsidian's Community plugins settings. Open
**Speech Capture: 管理处理设备** to choose the local Worker or add a home Worker through its private HTTPS URL.
Remote HTTP endpoints and URLs containing credentials, query parameters, or fragments are rejected before a
connection is attempted. Desktop remote HTTPS uses Node's certificate-validating transport because Obsidian's
`requestUrl` closes Tailscale Serve connections; local loopback requests continue to use the Obsidian API. Large
binary transfers use a longer inactivity timeout and report progress inside each Worker upload part. Pairing
requests reuse a private HTTPS connection, respect network backpressure, and retry a disconnected part at one-minute
intervals up to three times before exposing the existing manual retry. Pairing credentials are saved only through
Obsidian Secret Storage. Worker upload staging reserves disk for both resumable parts and atomic assembly, then
releases redundant part bytes after the verified source is committed.

See the root [product requirements](../../docs/product-requirements.md), [architecture](../../docs/architecture.md), and [UI direction](../../docs/ui-direction.md).
