"""Disk and memory preflight decisions for local model work."""

from __future__ import annotations

import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from speech_capture_worker.domain import ModelProfile, ResourceStatus
from speech_capture_worker.errors import InvalidJobRequest, ResourceBlocked

GIB = 1024**3
MIB = 1024**2


@dataclass(frozen=True)
class DiskSnapshot:
    total_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int
    used_percent: float
    swap_used_bytes: int


@dataclass(frozen=True)
class ResourceIssue:
    code: str
    status: ResourceStatus
    message: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceReport:
    status: ResourceStatus
    estimated_required_bytes: int
    disk_reserve_bytes: int
    disk_free_after_bytes: int
    disk: DiskSnapshot
    memory: MemorySnapshot
    issues: tuple[ResourceIssue, ...]

    @property
    def can_start(self) -> bool:
        return self.status is not ResourceStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "can_start": self.can_start,
            "estimated_required_bytes": self.estimated_required_bytes,
            "disk_reserve_bytes": self.disk_reserve_bytes,
            "disk_free_after_bytes": self.disk_free_after_bytes,
            "disk": asdict(self.disk),
            "memory": asdict(self.memory),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class ResourcePolicy:
    minimum_disk_reserve_bytes: int = 20 * GIB
    disk_reserve_fraction: float = 0.10
    disk_warning_margin_bytes: int = 5 * GIB
    memory_warning_available_bytes: int = 6 * GIB
    memory_block_available_bytes: int = 2 * GIB
    memory_warning_used_percent: float = 85.0
    memory_block_used_percent: float = 95.0
    swap_warning_bytes: int = 4 * GIB
    accuracy_minimum_total_memory_bytes: int = 16 * GIB
    speed_minimum_total_memory_bytes: int = 8 * GIB

    def evaluate(
        self,
        *,
        disk: DiskSnapshot,
        memory: MemorySnapshot,
        estimated_required_bytes: int,
        model_profile: ModelProfile,
    ) -> ResourceReport:
        _validate_snapshots(disk, memory, estimated_required_bytes)
        issues: list[ResourceIssue] = []
        disk_reserve_bytes = max(
            self.minimum_disk_reserve_bytes,
            int(disk.total_bytes * self.disk_reserve_fraction),
        )
        disk_free_after_bytes = disk.free_bytes - estimated_required_bytes

        if disk_free_after_bytes < disk_reserve_bytes:
            issues.append(
                ResourceIssue(
                    code="DISK_RESERVE_TOO_LOW",
                    status=ResourceStatus.BLOCKED,
                    message=(
                        "Starting this operation would reduce free disk below the "
                        "configured safety reserve."
                    ),
                    action="Free disk space manually, then run preflight again.",
                )
            )
        elif disk_free_after_bytes < disk_reserve_bytes + self.disk_warning_margin_bytes:
            issues.append(
                ResourceIssue(
                    code="DISK_RESERVE_WARNING",
                    status=ResourceStatus.WARNING,
                    message="Free disk would remain close to the configured safety reserve.",
                    action="Consider freeing disk space before starting a long job.",
                )
            )

        profile_minimum = (
            self.accuracy_minimum_total_memory_bytes
            if model_profile is ModelProfile.ACCURACY
            else self.speed_minimum_total_memory_bytes
        )
        if memory.total_bytes < profile_minimum:
            issues.append(
                ResourceIssue(
                    code="MODEL_PROFILE_MEMORY_TOO_LOW",
                    status=ResourceStatus.BLOCKED,
                    message="Installed memory is below the selected model profile minimum.",
                    action="Choose the speed profile or use a Worker with more memory.",
                )
            )

        if (
            memory.available_bytes < self.memory_block_available_bytes
            or memory.used_percent >= self.memory_block_used_percent
        ):
            issues.append(
                ResourceIssue(
                    code="MEMORY_PRESSURE_BLOCKED",
                    status=ResourceStatus.BLOCKED,
                    message="Current memory pressure is too high to start model work safely.",
                    action="Close memory-intensive applications or choose a lighter profile.",
                )
            )
        elif (
            memory.available_bytes < self.memory_warning_available_bytes
            or memory.used_percent >= self.memory_warning_used_percent
        ):
            issues.append(
                ResourceIssue(
                    code="MEMORY_PRESSURE_WARNING",
                    status=ResourceStatus.WARNING,
                    message=(
                        "Current memory pressure may make processing slow or trigger safe pause."
                    ),
                    action="Processing may continue; close other large applications if practical.",
                )
            )

        if memory.swap_used_bytes >= self.swap_warning_bytes:
            issues.append(
                ResourceIssue(
                    code="SWAP_USAGE_WARNING",
                    status=ResourceStatus.WARNING,
                    message="Swap usage is already high before model work starts.",
                    action=(
                        "Wait for memory pressure to fall or restart memory-intensive applications."
                    ),
                )
            )

        status = ResourceStatus.READY
        if any(issue.status is ResourceStatus.BLOCKED for issue in issues):
            status = ResourceStatus.BLOCKED
        elif issues:
            status = ResourceStatus.WARNING
        return ResourceReport(
            status=status,
            estimated_required_bytes=estimated_required_bytes,
            disk_reserve_bytes=disk_reserve_bytes,
            disk_free_after_bytes=disk_free_after_bytes,
            disk=disk,
            memory=memory,
            issues=tuple(issues),
        )


def check_resource_preflight(
    storage_path: Path,
    *,
    estimated_required_bytes: int,
    model_profile: ModelProfile,
    policy: ResourcePolicy | None = None,
) -> ResourceReport:
    """Capture current system resources and evaluate one start decision."""

    return (policy or ResourcePolicy()).evaluate(
        disk=snapshot_disk(storage_path),
        memory=snapshot_memory(),
        estimated_required_bytes=estimated_required_bytes,
        model_profile=model_profile,
    )


def require_resource_preflight(report: ResourceReport) -> None:
    if report.status is ResourceStatus.BLOCKED:
        raise ResourceBlocked(
            "Worker resource preflight blocked the operation.",
            details={
                "issues": [issue.to_dict() for issue in report.issues],
                "disk_reserve_bytes": report.disk_reserve_bytes,
                "disk_free_after_bytes": report.disk_free_after_bytes,
            },
        )


def estimate_job_disk_bytes(*, source_size_bytes: int, duration_sec: float) -> int:
    """Estimate source staging, PCM, working copies, and artifact headroom."""

    if (
        not isinstance(source_size_bytes, int)
        or isinstance(source_size_bytes, bool)
        or source_size_bytes <= 0
    ):
        raise InvalidJobRequest("source_size_bytes must be greater than zero.")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise InvalidJobRequest("duration_sec must be a positive finite number.")

    pcm_bytes = math.ceil(duration_sec * 16_000 * 2)
    working_audio_bytes = pcm_bytes * 3
    artifact_headroom_bytes = max(256 * MIB, math.ceil(source_size_bytes * 0.10))
    return source_size_bytes + working_audio_bytes + artifact_headroom_bytes


def snapshot_disk(path: Path) -> DiskSnapshot:
    existing = path.resolve()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    return DiskSnapshot(total_bytes=usage.total, free_bytes=usage.free)


def snapshot_memory() -> MemorySnapshot:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return MemorySnapshot(
        total_bytes=int(memory.total),
        available_bytes=int(memory.available),
        used_percent=float(memory.percent),
        swap_used_bytes=int(swap.used),
    )


def _validate_snapshots(
    disk: DiskSnapshot,
    memory: MemorySnapshot,
    estimated_required_bytes: int,
) -> None:
    if disk.total_bytes <= 0 or disk.free_bytes < 0 or disk.free_bytes > disk.total_bytes:
        raise InvalidJobRequest("Disk snapshot is invalid.")
    if (
        memory.total_bytes <= 0
        or memory.available_bytes < 0
        or memory.available_bytes > memory.total_bytes
        or not math.isfinite(memory.used_percent)
        or not 0 <= memory.used_percent <= 100
        or memory.swap_used_bytes < 0
    ):
        raise InvalidJobRequest("Memory snapshot is invalid.")
    if (
        not isinstance(estimated_required_bytes, int)
        or isinstance(estimated_required_bytes, bool)
        or estimated_required_bytes < 0
    ):
        raise InvalidJobRequest("estimated_required_bytes must be a non-negative integer.")
