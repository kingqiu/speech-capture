"""Run an isolated macOS launchd crash/restart smoke test."""

from __future__ import annotations

import json
import os
import signal
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from speech_capture_worker.launchd_service import (
    LaunchdServiceConfig,
    LaunchdServiceManager,
)

START_TIMEOUT_SECONDS = 20.0
RESTART_TIMEOUT_SECONDS = 35.0


def main() -> None:
    executable = (
        Path(__file__).resolve().parents[1] / ".venv" / "bin" / "speech-capture-worker"
    )
    label = f"com.speechcapture.worker.restart-test.{uuid4().hex[:12]}"
    port = _available_loopback_port()
    manager = LaunchdServiceManager()
    with tempfile.TemporaryDirectory(prefix="speech-capture-launchd-test-") as temporary:
        root = Path(temporary)
        config = LaunchdServiceConfig(
            executable=executable,
            data_dir=root / "runtime",
            agent_path=root / "LaunchAgents" / f"{label}.plist",
            label=label,
            port=port,
        )
        installed = False
        try:
            manager.install(config)
            installed = True
            first = _wait_for_healthy_process(
                manager,
                config,
                port,
                timeout_seconds=START_TIMEOUT_SECONDS,
            )
            assert first.pid is not None
            os.kill(first.pid, signal.SIGKILL)
            restarted = _wait_for_healthy_process(
                manager,
                config,
                port,
                timeout_seconds=RESTART_TIMEOUT_SECONDS,
                previous_pid=first.pid,
            )
            if restarted.pid == first.pid:
                raise RuntimeError("launchd did not replace the crashed Worker process.")
            print(json.dumps({
                "launchd_restart_verified": True,
                "first_pid_observed": True,
                "replacement_pid_observed": True,
                "health_recovered": True,
                "runs_increased": (
                    first.runs is not None
                    and restarted.runs is not None
                    and restarted.runs > first.runs
                ),
            }, sort_keys=True))
        finally:
            if installed:
                manager.uninstall(config)


def _wait_for_healthy_process(
    manager: LaunchdServiceManager,
    config: LaunchdServiceConfig,
    port: int,
    *,
    timeout_seconds: float,
    previous_pid: int | None = None,
):
    deadline = time.monotonic() + timeout_seconds
    last_status = manager.status(config)
    while time.monotonic() < deadline:
        status = manager.status(config)
        last_status = status
        if (
            status.running
            and status.pid is not None
            and status.pid != previous_pid
            and _health_is_ready(port)
        ):
            return status
        time.sleep(0.25)
    raise RuntimeError(
        f"Worker service did not become healthy; final state={last_status.state}."
    )


def _health_is_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/health",
            timeout=1.0,
        ) as response:
            return response.status == 200
    except OSError:
        return False


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


if __name__ == "__main__":
    main()
