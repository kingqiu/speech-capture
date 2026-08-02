# Speech Capture Worker Manager

Status: the tested macOS LaunchAgent lifecycle core and development Manager CLI exist; the native SwiftUI Manager
and installable packaged runtime remain to be built.

The Worker Manager is a native macOS companion for setup and maintenance. It will:

- validate Apple Silicon, memory, and disk readiness;
- install and control the background Worker service;
- download, activate, update, and roll back model profiles;
- guide local Ollama and pyannote setup;
- display pairing codes and revoke devices;
- report resource, queue, and service health;
- export redacted diagnostics;
- uninstall Worker-owned data safely and explicitly.

Closing the Manager does not stop the Worker.

Planned implementation: SwiftUI and supported macOS service-management APIs.

See [security, privacy, and recovery](../../docs/security-privacy-and-recovery.md) and the [roadmap](../../docs/roadmap.md).
