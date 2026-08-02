"""Private-content-free runtime status for the future native Worker Manager."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from speech_capture_worker.asr_probe import (
    ACCURACY_MODEL_ID,
    ALIGNER_MODEL_ID,
    SPEED_MODEL_ID,
)
from speech_capture_worker.diarization_execution import DIARIZATION_MODEL_ID
from speech_capture_worker.gap_speech_activity import PYANNOTE_SEGMENTATION_MODEL_ID
from speech_capture_worker.launchd_service import (
    LaunchdCommandResult,
    LaunchdServiceConfig,
    LaunchdServiceStatus,
)

STATUS_SCHEMA_VERSION = "1.0.0"
OLLAMA_ACCURACY_MODEL = "qwen3:14b"
OLLAMA_EDITOR_MODEL = "qwen3:8b"


@dataclass(frozen=True)
class ModelStatus:
    model_id: str
    provider: str
    cache_present: bool


@dataclass(frozen=True)
class ResourceStatus:
    disk_total_bytes: int
    disk_free_bytes: int
    memory_total_bytes: int
    memory_available_bytes: int
    memory_used_percent: float
    worker_data_directory_ready: bool


@dataclass(frozen=True)
class EndpointStatus:
    mode: str
    tls_enabled: bool
    configured_port: int
    port_reachable: bool


@dataclass(frozen=True)
class NetworkStatus:
    tailscale_app_installed: bool
    tailscale_cli_available: bool
    tailscale_state: str
    tailscale_online: bool | None


@dataclass(frozen=True)
class OllamaStatus:
    cli_available: bool
    service_reachable: bool
    accuracy_model_present: bool
    editor_model_present: bool


@dataclass(frozen=True)
class ManagerStatusSnapshot:
    schema_version: str
    service: LaunchdServiceStatus
    resources: ResourceStatus
    endpoint: EndpointStatus
    network: NetworkStatus
    ollama: OllamaStatus
    models: tuple[ModelStatus, ...]
    issue_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CommandRunner = Callable[[Sequence[str]], LaunchdCommandResult]
PortProbe = Callable[[str, int], bool]


def collect_manager_status(
    config: LaunchdServiceConfig,
    service: LaunchdServiceStatus,
    *,
    home: Path | None = None,
    huggingface_cache: Path | None = None,
    command_runner: CommandRunner | None = None,
    port_probe: PortProbe | None = None,
) -> ManagerStatusSnapshot:
    validated = config.validated()
    user_home = (home or Path.home()).expanduser().resolve()
    hf_cache = (
        huggingface_cache.expanduser().resolve()
        if huggingface_cache is not None
        else _default_huggingface_cache(user_home)
    )
    runner = command_runner or _run_command
    disk = _disk_usage(validated.data_dir)
    memory = psutil.virtual_memory()
    resources = ResourceStatus(
        disk_total_bytes=int(disk.total),
        disk_free_bytes=int(disk.free),
        memory_total_bytes=int(memory.total),
        memory_available_bytes=int(memory.available),
        memory_used_percent=float(memory.percent),
        worker_data_directory_ready=_directory_ready(validated.data_dir),
    )
    endpoint = EndpointStatus(
        mode="local_only" if _is_loopback(validated.host) else "private_tls",
        tls_enabled=validated.ssl_certfile is not None,
        configured_port=validated.port,
        port_reachable=(port_probe or _port_reachable)(validated.host, validated.port),
    )
    tailscale = _tailscale_status(user_home, runner)
    ollama = _ollama_status(runner, user_home)
    models = (
        _huggingface_model_status(hf_cache, ACCURACY_MODEL_ID, "mlx"),
        _huggingface_model_status(hf_cache, SPEED_MODEL_ID, "mlx"),
        _huggingface_model_status(hf_cache, ALIGNER_MODEL_ID, "mlx"),
        _huggingface_model_status(
            validated.data_dir / "models" / "pyannote",
            PYANNOTE_SEGMENTATION_MODEL_ID,
            "pyannote",
        ),
        _huggingface_model_status(
            validated.data_dir / "models" / "pyannote",
            DIARIZATION_MODEL_ID,
            "pyannote",
        ),
    )
    issues: list[str] = []
    if not service.installed:
        issues.append("SERVICE_NOT_INSTALLED")
    elif not service.running:
        issues.append("SERVICE_NOT_RUNNING")
    if not resources.worker_data_directory_ready:
        issues.append("WORKER_DATA_DIRECTORY_UNAVAILABLE")
    if resources.disk_free_bytes < 20 * 1024**3:
        issues.append("DISK_RESERVE_LOW")
    if resources.memory_available_bytes < 2 * 1024**3:
        issues.append("MEMORY_PRESSURE_HIGH")
    if service.running and not endpoint.port_reachable:
        issues.append("WORKER_PORT_UNREACHABLE")
    if not models[0].cache_present:
        issues.append("ASR_ACCURACY_MODEL_MISSING")
    if not models[1].cache_present:
        issues.append("ASR_SPEED_MODEL_MISSING")
    if not models[2].cache_present:
        issues.append("ALIGNER_MODEL_MISSING")
    if not ollama.cli_available:
        issues.append("OLLAMA_NOT_INSTALLED")
    elif not ollama.service_reachable:
        issues.append("OLLAMA_NOT_RUNNING")
    else:
        if not ollama.accuracy_model_present:
            issues.append("OLLAMA_ACCURACY_MODEL_MISSING")
        if not ollama.editor_model_present:
            issues.append("OLLAMA_EDITOR_MODEL_MISSING")
    return ManagerStatusSnapshot(
        schema_version=STATUS_SCHEMA_VERSION,
        service=service,
        resources=resources,
        endpoint=endpoint,
        network=tailscale,
        ollama=ollama,
        models=models,
        issue_codes=tuple(issues),
    )


def _huggingface_model_status(cache: Path, model_id: str, provider: str) -> ModelStatus:
    repository = cache / f"models--{model_id.replace('/', '--')}" / "snapshots"
    present = False
    try:
        present = repository.is_dir() and any(child.is_dir() for child in repository.iterdir())
    except OSError:
        present = False
    return ModelStatus(model_id=model_id, provider=provider, cache_present=present)


def _ollama_status(runner: CommandRunner, home: Path) -> OllamaStatus:
    models_dir = _default_ollama_models_dir(home)
    local_accuracy = _ollama_manifest_present(models_dir, OLLAMA_ACCURACY_MODEL)
    local_editor = _ollama_manifest_present(models_dir, OLLAMA_EDITOR_MODEL)
    executable = shutil.which("ollama")
    if executable is None:
        return OllamaStatus(False, False, local_accuracy, local_editor)
    result = runner((executable, "list"))
    if result.returncode != 0:
        return OllamaStatus(True, False, local_accuracy, local_editor)
    names = {
        line.split()[0]
        for line in result.stdout.splitlines()[1:]
        if line.strip() and line.split()
    }
    return OllamaStatus(
        cli_available=True,
        service_reachable=True,
        accuracy_model_present=local_accuracy or OLLAMA_ACCURACY_MODEL in names,
        editor_model_present=local_editor or OLLAMA_EDITOR_MODEL in names,
    )


def _ollama_manifest_present(models_dir: Path, model_id: str) -> bool:
    try:
        name, tag = model_id.split(":", 1)
    except ValueError:
        return False
    manifest = (
        models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag
    )
    try:
        return manifest.is_file() and not manifest.is_symlink()
    except OSError:
        return False


def _tailscale_status(home: Path, runner: CommandRunner) -> NetworkStatus:
    app_installed = (Path("/Applications") / "Tailscale.app").exists() or (
        home / "Applications" / "Tailscale.app"
    ).exists()
    executable = shutil.which("tailscale")
    if executable is None:
        return NetworkStatus(app_installed, False, "unavailable", None)
    result = runner((executable, "status", "--json"))
    if result.returncode != 0:
        return NetworkStatus(app_installed, True, "unavailable", None)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return NetworkStatus(app_installed, True, "invalid_response", None)
    backend_state = payload.get("BackendState")
    self_state = payload.get("Self")
    online = self_state.get("Online") if isinstance(self_state, dict) else None
    if not isinstance(online, bool):
        online = None
    state = str(backend_state).lower() if isinstance(backend_state, str) else "unknown"
    return NetworkStatus(app_installed, True, state, online)


def _default_huggingface_cache(home: Path) -> Path:
    hub_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub_cache:
        return Path(hub_cache).expanduser().resolve()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return (Path(hf_home).expanduser().resolve() / "hub")
    return home / ".cache" / "huggingface" / "hub"


def _default_ollama_models_dir(home: Path) -> Path:
    models_dir = os.environ.get("OLLAMA_MODELS")
    if models_dir:
        return Path(models_dir).expanduser().resolve()
    return home / ".ollama" / "models"


def _disk_usage(path: Path):
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return shutil.disk_usage(existing)


def _directory_ready(path: Path) -> bool:
    if path.exists():
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _is_loopback(host: str) -> bool:
    return host == "localhost" or host.startswith("127.") or host == "::1"


def _port_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _run_command(arguments: Sequence[str]) -> LaunchdCommandResult:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return LaunchdCommandResult(returncode=1)
    return LaunchdCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr="",
    )
