"""Worker Manager CLI lifecycle output and redaction tests."""

from __future__ import annotations

import json

from speech_capture_worker.errors import ServiceCommandFailed
from speech_capture_worker.launchd_service import LaunchdServiceStatus
from speech_capture_worker.manager_cli import main


def _executable(tmp_path):
    executable = tmp_path / "speech-capture-worker"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_manager_cli_returns_content_free_service_status(tmp_path, monkeypatch, capsys) -> None:
    class FakeManager:
        def status(self, _config):
            return LaunchdServiceStatus(
                label="com.speechcapture.worker",
                installed=True,
                loaded=True,
                running=True,
                state="running",
                pid=8123,
                runs=2,
                last_exit_status=0,
            )

    monkeypatch.setattr(
        "speech_capture_worker.manager_cli.LaunchdServiceManager",
        FakeManager,
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_cli.collect_manager_status",
        lambda _config, service: SimpleStatus(service),
    )
    result = main([
        "status",
        "--data-dir",
        str(tmp_path / "runtime"),
        "--executable",
        str(_executable(tmp_path)),
    ])
    output = capsys.readouterr()

    assert result == 0
    assert output.err == ""
    assert json.loads(output.out)["status"]["service"]["running"] is True
    assert str(tmp_path) not in output.out


def test_manager_cli_redacts_launchd_failure_details(tmp_path, monkeypatch, capsys) -> None:
    private_path = "/Users/private/customer/recording.wav"

    class FailingManager:
        def restart(self, _config):
            raise ServiceCommandFailed(f"launchd failed for {private_path}")

    monkeypatch.setattr(
        "speech_capture_worker.manager_cli.LaunchdServiceManager",
        FailingManager,
    )
    result = main([
        "restart",
        "--data-dir",
        str(tmp_path / "runtime"),
        "--executable",
        str(_executable(tmp_path)),
    ])
    output = capsys.readouterr()

    assert result == 2
    assert private_path not in output.err
    assert json.loads(output.err)["error"] == {
        "code": "SERVICE_COMMAND_FAILED",
        "message": "launchd failed for [redacted-path]",
    }


class SimpleStatus:
    def __init__(self, service: LaunchdServiceStatus) -> None:
        self.service = service

    def to_dict(self):
        return {"service": self.service.to_dict(), "issue_codes": ()}
