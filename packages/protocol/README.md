# Speech Capture Protocol

Status: contract direction only; schemas and generated types have not been added.

This package will be the compatibility boundary between the Obsidian plugin and Speech Worker. It will contain:

- versioned OpenAPI documents;
- artifact JSON Schemas;
- stable job-state and error-code enums;
- generated TypeScript and Python types;
- compatibility fixtures and contract tests.

The protocol must remain independent from Obsidian sync, Google Drive, Tailscale, and individual model providers.

See the [Worker API direction](../../docs/worker-api.md) and [data model](../../docs/data-model-and-output.md).
