# macOS Worker background service

## 1. Current Stage G boundary

The implemented service core installs Speech Capture as a per-user macOS LaunchAgent. It uses current `launchctl`
`bootstrap`, `bootout`, `kickstart`, and `print` operations and a private launchd property list. The future native
Worker Manager can call the same lifecycle core; the current command interface is for development and verification.

The LaunchAgent:

- starts the authenticated Worker API at user login;
- is kept alive by launchd and throttled if it crashes repeatedly;
- receives `SIGTERM` and a bounded shutdown window when stopped;
- uses an absolute executable path and argument array without a shell;
- stores no device credential, pairing secret, source filename, transcript, prompt, or Vault content;
- creates Worker logs and service configuration with a `077` umask;
- preserves Worker databases and pairing state when stopped, restarted, or uninstalled.

## 2. Development lifecycle commands

From `services/speech-worker`:

```bash
uv run speech-capture-manager install \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager status \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager model-budget \
  --profile accuracy \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager restart \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager stop \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager start \
  --executable "$PWD/.venv/bin/speech-capture-worker"

uv run speech-capture-manager uninstall \
  --executable "$PWD/.venv/bin/speech-capture-worker"
```

The default application-data directory is
`~/Library/Application Support/Speech Capture Worker`. Uninstalling the service removes only its LaunchAgent; it
does not delete databases, models, uploaded sources, generated artifacts, or user files.

`model-budget` is a read-only preflight. It subtracts models already reported as present, adds 15% download
headroom, and requires the filesystem to retain at least 20 GiB or 10% of total capacity, whichever is larger.
It reports an estimate and shortfall before a future download action; it does not download or activate a model.

## 3. Restart and login conditions

| Machine condition | Expected Worker behavior |
| --- | --- |
| Manager window closes | Worker continues under launchd. |
| Worker process crashes | launchd starts a replacement process; durable jobs and pairing remain on disk. |
| Service is restarted | New process opens the same Worker and security databases; no re-pairing. |
| User logs out | Per-user LaunchAgent stops. |
| User logs in | LaunchAgent loads and starts the Worker. |
| Mac restarts with FileVault enabled | Worker cannot start before the encrypted volume is unlocked and the user login session exists. |
| Mac is booted but no user session exists | This per-user LaunchAgent is not running. |
| Sync-backed Vault is unavailable | Uploaded work may continue in local Worker storage, but publication must remain pending and must not be reported as published. |
| Tailscale starts before user login | Network availability does not bypass the Worker login and disk-unlock boundary. |

“No graphical window required” is supported after login. “Process private user data before FileVault unlock” is not
a safe or achievable promise.

## 4. Verified and pending evidence

Automated tests cover private deterministic property-list generation, configuration conflict protection, idempotent
install/start/stop/restart/uninstall behavior, safe status parsing, macOS-only enforcement, and redacted Manager
output. An isolated real macOS smoke test loaded a temporary Worker, observed a healthy API process, terminated that
test process with `SIGKILL`, observed a different replacement PID and increased launch count, and confirmed health
recovery before unloading the test service.

A real logout/login and cold-reboot acceptance test remains pending because it would interrupt the active user
session. It must be performed explicitly during a planned maintenance window; the project must not reboot or log out
the user's Mac automatically.
