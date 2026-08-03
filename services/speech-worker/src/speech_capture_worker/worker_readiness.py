"""Authenticated, private-content-free Worker readiness for plugin decisions."""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from speech_capture_worker import __version__
from speech_capture_worker.domain import ModelProfile, ResourceStatus
from speech_capture_worker.errors import ModelActivationFailed
from speech_capture_worker.protocol_contract import PROTOCOL_VERSION
from speech_capture_worker.resources import (
    DiskSnapshot,
    MemorySnapshot,
    ResourcePolicy,
    snapshot_disk,
    snapshot_memory,
)

READINESS_SCHEMA_VERSION = "1.0.0"


class ReadinessState(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProfileReadiness:
    model_profile: str
    state: ReadinessState
    can_start: bool
    issue_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerReadinessSnapshot:
    schema_version: str
    checked_at: str
    worker_version: str
    protocol_version: str
    state: ReadinessState
    endpoint_mode: str
    tls_enabled: bool
    storage_ready: bool
    worker_database_ok: bool
    security_database_ok: bool
    ffmpeg_available: bool
    ffprobe_available: bool
    ollama_reachable: bool
    active_model_profile: str | None
    disk_total_bytes: int
    disk_free_bytes: int
    disk_reserve_bytes: int
    memory_total_bytes: int
    memory_available_bytes: int
    memory_used_percent: float
    swap_used_bytes: int
    profiles: tuple[ProfileReadiness, ...]
    issue_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["profiles"] = [profile.to_dict() for profile in self.profiles]
        return payload


def collect_worker_readiness(
    data_dir: Path,
    *,
    worker_database_ok: bool,
    security_database_ok: bool,
    endpoint_mode: str,
    tls_enabled: bool,
    disk: DiskSnapshot | None = None,
    memory: MemorySnapshot | None = None,
    storage_ready: bool | None = None,
    ffmpeg_available: bool | None = None,
    ffprobe_available: bool | None = None,
    ollama_reachable: bool | None = None,
    active_model_profile: str | None = None,
    inspect_activation: bool = True,
) -> WorkerReadinessSnapshot:
    """Collect only stable facts needed for an authenticated UI readiness decision."""

    root = data_dir.expanduser().resolve()
    disk_snapshot = disk or snapshot_disk(root)
    memory_snapshot = memory or snapshot_memory()
    directory_ready = _directory_ready(root) if storage_ready is None else storage_ready
    has_ffmpeg = (
        shutil.which("ffmpeg") is not None
        if ffmpeg_available is None
        else ffmpeg_available
    )
    has_ffprobe = (
        shutil.which("ffprobe") is not None
        if ffprobe_available is None
        else ffprobe_available
    )
    ollama_ready = _ollama_reachable() if ollama_reachable is None else ollama_reachable
    activation_issue: str | None = None
    selected_profile = active_model_profile
    if inspect_activation and selected_profile is None:
        from speech_capture_worker.model_activation import ModelActivationManager

        try:
            activation = ModelActivationManager(root).status().active
        except ModelActivationFailed:
            activation_issue = "MODEL_ACTIVATION_STATE_INVALID"
        else:
            selected_profile = activation.profile if activation is not None else None
    if selected_profile not in {None, "accuracy", "speed", "all"}:
        activation_issue = "MODEL_ACTIVATION_STATE_INVALID"
        selected_profile = None

    general_issues: list[str] = []
    if not directory_ready:
        general_issues.append("WORKER_DATA_DIRECTORY_UNAVAILABLE")
    if not worker_database_ok:
        general_issues.append("WORKER_DATABASE_UNAVAILABLE")
    if not security_database_ok:
        general_issues.append("SECURITY_DATABASE_UNAVAILABLE")
    if not has_ffmpeg:
        general_issues.append("FFMPEG_UNAVAILABLE")
    if not has_ffprobe:
        general_issues.append("FFPROBE_UNAVAILABLE")
    if not ollama_ready:
        general_issues.append("OLLAMA_UNREACHABLE")
    if activation_issue is not None:
        general_issues.append(activation_issue)
    elif selected_profile is None:
        general_issues.append("MODEL_PROFILE_NOT_ACTIVE")

    policy = ResourcePolicy()
    profiles = tuple(
        _profile_readiness(
            profile,
            policy=policy,
            disk=disk_snapshot,
            memory=memory_snapshot,
            active_model_profile=selected_profile,
            general_issues=tuple(general_issues),
        )
        for profile in ModelProfile
    )
    available = tuple(profile for profile in profiles if profile.can_start)
    if not available:
        state = ReadinessState.BLOCKED
    elif any(profile.state is ReadinessState.WARNING for profile in available):
        state = ReadinessState.WARNING
    else:
        state = ReadinessState.READY
    issue_codes = tuple(
        dict.fromkeys(
            (*general_issues, *(code for profile in profiles for code in profile.issue_codes))
        )
    )
    reserve = max(
        policy.minimum_disk_reserve_bytes,
        int(disk_snapshot.total_bytes * policy.disk_reserve_fraction),
    )
    return WorkerReadinessSnapshot(
        schema_version=READINESS_SCHEMA_VERSION,
        checked_at=datetime.now(UTC).isoformat(),
        worker_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        state=state,
        endpoint_mode=endpoint_mode,
        tls_enabled=tls_enabled,
        storage_ready=directory_ready,
        worker_database_ok=worker_database_ok,
        security_database_ok=security_database_ok,
        ffmpeg_available=has_ffmpeg,
        ffprobe_available=has_ffprobe,
        ollama_reachable=ollama_ready,
        active_model_profile=selected_profile,
        disk_total_bytes=disk_snapshot.total_bytes,
        disk_free_bytes=disk_snapshot.free_bytes,
        disk_reserve_bytes=reserve,
        memory_total_bytes=memory_snapshot.total_bytes,
        memory_available_bytes=memory_snapshot.available_bytes,
        memory_used_percent=memory_snapshot.used_percent,
        swap_used_bytes=memory_snapshot.swap_used_bytes,
        profiles=profiles,
        issue_codes=issue_codes,
    )


def _profile_readiness(
    profile: ModelProfile,
    *,
    policy: ResourcePolicy,
    disk: DiskSnapshot,
    memory: MemorySnapshot,
    active_model_profile: str | None,
    general_issues: tuple[str, ...],
) -> ProfileReadiness:
    report = policy.evaluate(
        disk=disk,
        memory=memory,
        estimated_required_bytes=0,
        model_profile=profile,
    )
    issues = list(general_issues)
    if active_model_profile not in {profile.value, "all"}:
        issues.append(f"{profile.value.upper()}_PROFILE_NOT_ACTIVE")
    issues.extend(issue.code for issue in report.issues)
    blocked = bool(general_issues) or active_model_profile not in {profile.value, "all"}
    blocked = blocked or report.status is ResourceStatus.BLOCKED
    if blocked:
        state = ReadinessState.BLOCKED
    elif report.status is ResourceStatus.WARNING:
        state = ReadinessState.WARNING
    else:
        state = ReadinessState.READY
    return ProfileReadiness(
        model_profile=profile.value,
        state=state,
        can_start=not blocked,
        issue_codes=tuple(dict.fromkeys(issues)),
    )


def _directory_ready(path: Path) -> bool:
    if path.exists():
        return path.is_dir() and not path.is_symlink() and os.access(
            path,
            os.R_OK | os.W_OK | os.X_OK,
        )
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def _ollama_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.25):
            return True
    except OSError:
        return False
