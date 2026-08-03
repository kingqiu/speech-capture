# Speech Capture Obsidian Plugin

Status: Stage I implementation has started. The plugin now has a versioned manifest, reproducible TypeScript/
esbuild toolchain, system-secret credential boundary, persistent non-secret settings, and the first approved
workbench shell. It is not yet ready for real task submission.

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

See the root [product requirements](../../docs/product-requirements.md), [architecture](../../docs/architecture.md), and [UI direction](../../docs/ui-direction.md).
