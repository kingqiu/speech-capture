# Speech Capture Protocol

This directory contains the versioned Worker protocol artifacts shared with future clients.

- `openapi.json` is generated from the FastAPI schemas and checked into Git.
- Protocol and artifact compatibility are negotiated before upload.
- `generated/python/speech_capture_protocol.py` provides dependency-free Python wire types.
- `generated/typescript/speech-capture-protocol.ts` provides readonly TypeScript wire types.

Regenerate the canonical OpenAPI document from the repository root:

```bash
services/speech-worker/.venv/bin/python services/speech-worker/scripts/export_openapi.py
services/speech-worker/.venv/bin/python packages/protocol/scripts/generate_types.py
services/speech-worker/.venv/bin/python packages/protocol/scripts/generate_types.py --check
```

The generated files include the source OpenAPI SHA-256 and must not be edited manually. Regenerate them after
changing the API schema; the test suite fails if either language output drifts from `openapi.json`.
