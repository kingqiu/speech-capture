"""macOS LaunchAgent generation and lifecycle tests."""

from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from speech_capture_worker.errors import ServiceInstallConflict, ServiceUnsupported
from speech_capture_worker.launchd_service import (
    LaunchdCommandResult,
    LaunchdServiceConfig,
    LaunchdServiceManager,
    parse_launchctl_print,
    render_launch_agent,
)


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded = False
        self.running = False
        self.pid = 4100
        self.runs = 0
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments) -> LaunchdCommandResult:
        command = tuple(arguments)
        self.calls.append(command)
        if command[0] == "print":
            if not self.loaded:
                return LaunchdCommandResult(returncode=113, stderr="not loaded")
            state = "running" if self.running else "exited"
            pid = f"\n\tpid = {self.pid}" if self.running else ""
            return LaunchdCommandResult(
                returncode=0,
                stdout=(
                    f"service = {{\n\tstate = {state}{pid}\n\truns = {self.runs}"
                    "\n\tlast exit code = 0\n}\n"
                ),
            )
        if command[0] == "bootstrap":
            self.loaded = True
            self.running = True
            self.runs += 1
            return LaunchdCommandResult(returncode=0)
        if command[0] == "bootout":
            self.loaded = False
            self.running = False
            return LaunchdCommandResult(returncode=0)
        if command[0] == "kickstart":
            self.loaded = True
            self.running = True
            self.pid += 1
            self.runs += 1
            return LaunchdCommandResult(returncode=0)
        raise AssertionError(f"Unexpected launchctl command: {command}")


def _config(tmp_path: Path) -> LaunchdServiceConfig:
    label = "com.speechcapture.worker.test"
    executable = tmp_path / "speech-capture-worker"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return LaunchdServiceConfig(
        executable=executable,
        data_dir=tmp_path / "Application Support" / "Worker",
        agent_path=tmp_path / "LaunchAgents" / f"{label}.plist",
        label=label,
    )


def test_launch_agent_is_private_restartable_and_contains_no_credentials(tmp_path) -> None:
    config = _config(tmp_path)
    content = render_launch_agent(config)
    payload = plistlib.loads(content)

    assert payload["Label"] == config.label
    assert payload["Program"] == str(config.executable.resolve())
    assert payload["ProgramArguments"] == [
        str(config.executable.resolve()),
        "serve",
        "--data-dir",
        str(config.data_dir.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 10
    assert payload["ExitTimeOut"] == 30
    assert payload["Umask"] == 0o077
    serialized = content.decode("utf-8").lower()
    assert "bearer" not in serialized
    assert "pairing" not in serialized
    assert "scw_" not in serialized


def test_install_start_restart_stop_and_uninstall_are_idempotent(tmp_path) -> None:
    runner = FakeLaunchctl()
    manager = LaunchdServiceManager(uid=501, platform="darwin", runner=runner)
    config = _config(tmp_path)

    installed = manager.install(config)
    installed_again = manager.install(config)
    restarted = manager.restart(config)
    stopped = manager.stop(config)
    started = manager.start(config)
    uninstalled = manager.uninstall(config)

    assert installed.running is True
    assert installed_again.running is True
    assert restarted.running is True
    assert restarted.pid == 4101
    assert stopped.loaded is False
    assert started.running is True
    assert uninstalled.installed is False
    assert not config.agent_path.exists()
    assert stat.S_IMODE(config.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((config.data_dir / "logs").stat().st_mode) == 0o700
    assert ("bootstrap", "gui/501", str(config.agent_path.resolve())) in runner.calls
    assert (
        "kickstart",
        "-k",
        "gui/501/com.speechcapture.worker.test",
    ) in runner.calls


def test_install_refuses_to_replace_an_unrelated_launch_agent(tmp_path) -> None:
    config = _config(tmp_path)
    config.agent_path.parent.mkdir(parents=True)
    config.agent_path.write_text("unrelated configuration", encoding="utf-8")
    manager = LaunchdServiceManager(
        uid=501,
        platform="darwin",
        runner=FakeLaunchctl(),
    )

    with pytest.raises(ServiceInstallConflict):
        manager.install(config)

    assert config.agent_path.read_text(encoding="utf-8") == "unrelated configuration"


def test_launchctl_status_parser_ignores_paths_and_unrecognized_lines() -> None:
    parsed = parse_launchctl_print(
        """
        gui/501/com.speechcapture.worker = {
            path = /Users/private/Library/LaunchAgents/private.plist
            state = running
            pid = 9123
            runs = 4
            last exit status = 9
            private transcript = do not retain
        }
        """
    )

    assert parsed == {
        "state": "running",
        "pid": "9123",
        "runs": "4",
        "last_exit_status": "9",
    }


def test_active_launchd_state_with_pid_is_treated_as_running(tmp_path) -> None:
    config = _config(tmp_path)
    config.agent_path.parent.mkdir(parents=True)
    config.agent_path.write_bytes(render_launch_agent(config))

    def active_runner(_arguments) -> LaunchdCommandResult:
        return LaunchdCommandResult(
            returncode=0,
            stdout="state = active\npid = 7221\nruns = 1\n",
        )

    status = LaunchdServiceManager(
        uid=501,
        platform="darwin",
        runner=active_runner,
    ).status(config)

    assert status.running is True
    assert status.pid == 7221


def test_service_management_fails_closed_off_macos(tmp_path) -> None:
    manager = LaunchdServiceManager(platform="linux", runner=FakeLaunchctl())

    with pytest.raises(ServiceUnsupported):
        manager.status(_config(tmp_path))
