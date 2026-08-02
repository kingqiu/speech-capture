# Standalone macOS Worker runtime

## 1. Scope

Stage G provides a local `macOS arm64` onedir runtime that does not require a repository checkout, Python, uv, a
virtual environment, Homebrew FFmpeg, or development dependencies on the machine where it runs. Models and private
Worker data remain outside the application directory so an application update never overwrites either.

The bundle includes:

- a private Python 3.11 runtime and the Worker/Manager Python dependencies;
- MLX Qwen3-ASR and ForcedAligner code and assets;
- pyannote/torch dependencies for VAD and speaker diarization;
- FastAPI/Uvicorn and SQLite support;
- FFmpeg and FFprobe plus their non-system dynamic libraries;
- `speech-capture-worker` and `speech-capture-manager` launchers;
- a SHA-256 manifest covering every regular file and every internal symlink target.

Ollama and the model files are not embedded. The Manager validates or downloads models into their normal local
stores, and Ollama continues to run as its own local service.

## 2. Build

From `services/speech-worker` on an Apple Silicon Mac:

```bash
uv sync --all-extras
.venv/bin/python scripts/build_standalone_runtime.py --check
.venv/bin/python scripts/build_standalone_runtime.py
.venv/bin/python scripts/build_standalone_runtime.py --verify
```

The generated directory is `dist/SpeechCaptureWorker/`. Both `dist/` and `build/` are ignored by Git; the generated
runtime, model files, audio, transcripts, Notes and private runtime databases are never committed.

The build is intentionally fail-closed. It requires macOS arm64, Python 3.11, pinned PyInstaller 6.21.0, FFmpeg and
FFprobe. It deletes only its own validated `build/standalone-runtime` and `dist/SpeechCaptureWorker` children before
a rebuild. The output manifest rejects symbolic links that escape the package. The path audit rejects the repository
path, home directory and virtual-environment path in every packaged regular file.

## 3. Verification performed by the builder

The build does not stop after PyInstaller reports success. It also:

- starts both launchers with only `/usr/bin:/bin:/usr/sbin:/sbin` in `PATH` and `/private/tmp` as the working
  directory;
- creates a fresh Worker SQLite database through the frozen Worker;
- reads model activation status through the frozen Manager;
- verifies the frozen main executable's local macOS code signature;
- validates manifest paths, permissions, file hashes and internal symlinks;
- removes temporary smoke-test data automatically.

The final local build produced 3,392 packaged entries, occupies about 703 MiB on disk, and has runtime manifest
SHA-256 `3ad1d8e85ffdda0b1eca85e674dd6b88892ec1ca3cc6010a23ecee4bb5984f1b`. Additional real checks confirmed:

- bundled FFmpeg 8.0.1 and FFprobe run with the minimal system `PATH`;
- FFmpeg/FFprobe link only to package-relative `@rpath` libraries or macOS system frameworks/libraries;
- the frozen archive includes MLX ASR/aligner modules and pyannote audio pipelines;
- the packaged Manager fully hash-validates all five installed model identities;
- the packaged Worker starts on loopback and `/v1/health` reports Worker `0.1.0a0`, protocol `1.0.0`;
- shutdown is clean and all temporary runtime data was removed.

## 4. Distribution boundary

The local build is ad-hoc signed and verified for development on this Mac. A release sent to another Mac still needs
a project-controlled Apple Developer ID signature and notarization so Gatekeeper can establish publisher trust. No
Developer ID credential is stored in this repository, and the build must not invent or export one. Release signing
and notarization are distribution operations; they do not change the standalone runtime's code or private-data
boundary.

Cold reboot and logout/login acceptance also remain a manual maintenance-window check because an automated run would
interrupt the project owner's active macOS session.
