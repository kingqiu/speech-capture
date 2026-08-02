"""Worker Manager CLI lifecycle output and redaction tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from speech_capture_worker.errors import ServiceCommandFailed
from speech_capture_worker.launchd_service import LaunchdServiceStatus
from speech_capture_worker.manager_cli import main
from speech_capture_worker.manager_status import ModelStatus, OllamaStatus


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


def test_manager_cli_reports_pre_download_budget_without_starting_download(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    class FakeManager:
        def status(self, _config):
            return LaunchdServiceStatus(
                label="com.speechcapture.worker",
                installed=False,
                loaded=False,
                running=False,
                state="not_installed",
                pid=None,
                runs=None,
                last_exit_status=None,
            )

    status = SimpleNamespace(
        resources=SimpleNamespace(
            disk_total_bytes=500 * 1024**3,
            disk_free_bytes=100 * 1024**3,
        ),
        models=(
            ModelStatus("Qwen/Qwen3-ASR-1.7B", "mlx", True),
            ModelStatus("Qwen/Qwen3-ASR-0.6B", "mlx", False),
            ModelStatus("Qwen/Qwen3-ForcedAligner-0.6B", "mlx", True),
        ),
        ollama=OllamaStatus(True, True, False, True),
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_cli.LaunchdServiceManager",
        FakeManager,
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_cli.collect_manager_status",
        lambda _config, _service: status,
    )

    result = main([
        "model-budget",
        "--profile",
        "accuracy",
        "--data-dir",
        str(tmp_path / "runtime"),
        "--executable",
        str(_executable(tmp_path)),
    ])
    output = capsys.readouterr()
    budget = json.loads(output.out)["budget"]

    assert result == 0
    assert output.err == ""
    assert budget["profile"] == "accuracy"
    assert budget["estimate_only"] is True
    assert budget["can_download"] is True
    assert budget["missing_download_bytes"] == 9_300_000_000
    assert [item["present"] for item in budget["items"]] == [
        True,
        True,
        False,
        True,
    ]
    assert str(tmp_path) not in output.out


class SimpleStatus:
    def __init__(self, service: LaunchdServiceStatus) -> None:
        self.service = service

    def to_dict(self):
        return {"service": self.service.to_dict(), "issue_codes": ()}
