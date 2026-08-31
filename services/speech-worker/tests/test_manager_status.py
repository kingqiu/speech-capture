"""Content-free Worker Manager status snapshot tests."""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

from speech_capture_worker.launchd_service import (
    LaunchdCommandResult,
    LaunchdServiceConfig,
    LaunchdServiceStatus,
)
from speech_capture_worker.manager_status import collect_manager_status


def _config(tmp_path: Path) -> LaunchdServiceConfig:
    label = "com.speechcapture.worker.status-test"
    executable = tmp_path / "speech-capture-worker"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return LaunchdServiceConfig(
        executable=executable,
        data_dir=tmp_path / "private-customer-runtime",
        agent_path=tmp_path / f"{label}.plist",
        label=label,
    )


def _service(*, running: bool = True) -> LaunchdServiceStatus:
    return LaunchdServiceStatus(
        label="com.speechcapture.worker.status-test",
        installed=True,
        loaded=running,
        running=running,
        state="running" if running else "stopped",
        pid=7001 if running else None,
        runs=3,
        last_exit_status=0,
    )


def test_status_reports_resources_models_endpoint_and_network_without_private_paths(
    tmp_path,
    monkeypatch,
) -> None:
    hf_cache = tmp_path / "private-huggingface-cache"
    for model in (
        "Qwen--Qwen3-ASR-1.7B",
        "Qwen--Qwen3-ASR-0.6B",
        "Qwen--Qwen3-ForcedAligner-0.6B",
    ):
        (hf_cache / f"models--{model}" / "snapshots" / "revision").mkdir(
            parents=True
        )
    config = _config(tmp_path)
    pyannote_cache = config.data_dir / "models" / "pyannote"
    (
        pyannote_cache
        / "models--pyannote--segmentation"
        / "snapshots"
        / "revision"
    ).mkdir(parents=True)
    disk_usage = namedtuple("disk_usage", "total used free")
    monkeypatch.setattr(
        "speech_capture_worker.manager_status._disk_usage",
        lambda _path: disk_usage(100 * 1024**3, 40 * 1024**3, 60 * 1024**3),
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_status.psutil.virtual_memory",
        lambda: SimpleNamespace(
            total=32 * 1024**3,
            available=12 * 1024**3,
            percent=62.5,
        ),
    )

    def which(name: str) -> str | None:
        return f"/test-bin/{name}" if name in {"ollama", "tailscale"} else None

    monkeypatch.setattr("speech_capture_worker.manager_status.shutil.which", which)

    def runner(arguments) -> LaunchdCommandResult:
        if arguments[-1] == "list":
            return LaunchdCommandResult(
                0,
                "NAME ID SIZE MODIFIED\nqwen3:14b abc 9GB today\nqwen3:8b def 5GB today\n",
            )
        return LaunchdCommandResult(
            0,
            json.dumps({"BackendState": "Running", "Self": {"Online": True}}),
        )

    snapshot = collect_manager_status(
        config,
        _service(),
        home=tmp_path / "private-home",
        huggingface_cache=hf_cache,
        command_runner=runner,
        port_probe=lambda _host, _port: True,
    )
    payload = snapshot.to_dict()
    serialized = json.dumps(payload)

    assert payload["resources"]["disk_free_bytes"] == 60 * 1024**3
    assert payload["resources"]["memory_available_bytes"] == 12 * 1024**3
    assert payload["endpoint"] == {
        "mode": "local_only",
        "tls_enabled": False,
        "configured_port": 8765,
        "port_reachable": True,
    }
    assert payload["network"]["tailscale_state"] == "running"
    assert payload["network"]["tailscale_online"] is True
    assert payload["ollama"] == {
        "cli_available": True,
        "service_reachable": True,
        "accuracy_model_present": True,
        "editor_model_present": True,
    }
    assert [model["cache_present"] for model in payload["models"]] == [
        True,
        True,
        True,
        True,
        False,
    ]
    assert payload["issue_codes"] == ()
    assert str(tmp_path) not in serialized
    assert "private-customer" not in serialized


def test_status_uses_actionable_codes_when_dependencies_are_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    disk_usage = namedtuple("disk_usage", "total used free")
    monkeypatch.setattr(
        "speech_capture_worker.manager_status._disk_usage",
        lambda _path: disk_usage(100 * 1024**3, 90 * 1024**3, 10 * 1024**3),
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_status.psutil.virtual_memory",
        lambda: SimpleNamespace(
            total=16 * 1024**3,
            available=1024**3,
            percent=95.0,
        ),
    )
    monkeypatch.setattr(
        "speech_capture_worker.manager_status.shutil.which",
        lambda _name: None,
    )

    snapshot = collect_manager_status(
        config,
        _service(running=False),
        home=tmp_path,
        huggingface_cache=tmp_path / "missing-model-cache",
        port_probe=lambda _host, _port: False,
    )

    assert snapshot.issue_codes == (
        "SERVICE_NOT_RUNNING",
        "DISK_RESERVE_LOW",
        "MEMORY_PRESSURE_HIGH",
        "ASR_ACCURACY_MODEL_MISSING",
        "ASR_SPEED_MODEL_MISSING",
        "ALIGNER_MODEL_MISSING",
        "OLLAMA_NOT_INSTALLED",
    )
    assert snapshot.network.tailscale_cli_available is False
    assert all(model.cache_present is False for model in snapshot.models)


def test_status_detects_local_ollama_manifests_when_service_is_unreachable(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    manifests = (
        tmp_path
        / ".ollama"
        / "models"
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / "qwen3"
    )
    manifests.mkdir(parents=True)
    (manifests / "14b").write_text("{}", encoding="utf-8")
    (manifests / "8b").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "speech_capture_worker.manager_status.shutil.which",
        lambda name: "/test-bin/ollama" if name == "ollama" else None,
    )

    snapshot = collect_manager_status(
        config,
        _service(running=False),
        home=tmp_path,
        huggingface_cache=tmp_path / "missing-model-cache",
        command_runner=lambda _arguments: LaunchdCommandResult(returncode=1),
        port_probe=lambda _host, _port: False,
    )

    assert snapshot.ollama.service_reachable is False
    assert snapshot.ollama.accuracy_model_present is True
    assert snapshot.ollama.editor_model_present is True
    assert "OLLAMA_NOT_RUNNING" in snapshot.issue_codes
