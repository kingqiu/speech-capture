# Speech Capture Protocol

This directory contains the versioned Worker protocol artifacts shared with future clients.

- `openapi.json` is generated from the FastAPI schemas and checked into Git.
- Protocol and artifact compatibility are negotiated before upload.
- Generated Python and TypeScript types will be added in the next Stage F work item.

Regenerate the canonical OpenAPI document from the repository root:

```bash
services/speech-worker/.venv/bin/python services/speech-worker/scripts/export_openapi.py
```
