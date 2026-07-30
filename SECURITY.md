# Security Policy

## Current status

Speech Capture is in the design phase and does not yet have a supported release.

## Reporting a vulnerability

Do not open a public issue containing credentials, private audio, transcripts, private Vault paths, pairing tokens, or other sensitive data.

When a supported release exists, this document will be updated with a private reporting channel. Until then, report only a minimal, redacted description through the repository owner's private contact channel.

## Data that must never enter the repository

- Private audio or video recordings.
- Real transcripts or generated notes containing private material.
- Google, Hugging Face, Ollama provider, Tailscale, or other credentials.
- Worker pairing tokens and device secrets.
- macOS signing identities, certificates, or provisioning files.
- Runtime databases, logs, model weights, or local model caches.
- Personal absolute paths, email addresses, or device identifiers.

## Security posture

- Local processing is the default.
- Remote access is private-network only by default.
- Cloud fallback is disabled by default and requires explicit consent.
- Tokens are stored outside the Vault in operating-system-protected storage.
- Worker access is scoped by paired device and Vault identity.
- Diagnostic exports are user initiated and redacted.
