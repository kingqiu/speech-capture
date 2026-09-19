"""Conservative model download budgets shown before any network transfer."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from speech_capture_worker.asr_probe import (
    ACCURACY_MODEL_ID,
    ALIGNER_MODEL_ID,
    MODEL_DOWNLOAD_BYTES,
    SPEED_MODEL_ID,
)
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.manager_status import ManagerStatusSnapshot

GIB = 1024**3
MINIMUM_DISK_RESERVE_BYTES = 20 * GIB
DISK_RESERVE_FRACTION = 0.10
DOWNLOAD_HEADROOM_FRACTION = 0.15
OLLAMA_ACCURACY_EXPECTED_BYTES = 9_300_000_000
OLLAMA_EDITOR_EXPECTED_BYTES = 5_200_000_000
ModelProfileName = Literal["accuracy", "speed", "all"]


@dataclass(frozen=True)
class ModelDownloadItem:
    key: str
    model_id: str
    provider: str
    expected_download_bytes: int
    present: bool


@dataclass(frozen=True)
class ModelCatalogItem:
    key: str
    model_id: str
    provider: str
    expected_download_bytes: int


@dataclass(frozen=True)
class ModelDownloadBudget:
    profile: ModelProfileName
    estimate_only: bool
    disk_total_bytes: int
    disk_free_bytes: int
    disk_reserve_bytes: int
    catalog_download_bytes: int
    missing_download_bytes: int
    download_headroom_bytes: int
    required_before_download_bytes: int
    projected_free_after_bytes: int
    shortfall_bytes: int
    can_download: bool
    items: tuple[ModelDownloadItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_model_download_budget(
    profile: ModelProfileName,
    *,
    disk_total_bytes: int,
    disk_free_bytes: int,
    present: dict[str, bool] | None = None,
) -> ModelDownloadBudget:
    if profile not in {"accuracy", "speed", "all"}:
        raise InvalidJobRequest("The model download profile is not supported.")
    if (
        not isinstance(disk_total_bytes, int)
        or isinstance(disk_total_bytes, bool)
        or not isinstance(disk_free_bytes, int)
        or isinstance(disk_free_bytes, bool)
        or disk_total_bytes <= 0
        or disk_free_bytes < 0
        or disk_free_bytes > disk_total_bytes
    ):
        raise InvalidJobRequest("The model download disk snapshot is invalid.")
    presence = present or {}
    items = tuple(
        ModelDownloadItem(
            key=item.key,
            model_id=item.model_id,
            provider=item.provider,
            expected_download_bytes=item.expected_download_bytes,
            present=bool(presence.get(item.key, False)),
        )
        for item in model_catalog_for_profile(profile)
    )
    catalog_bytes = sum(item.expected_download_bytes for item in items)
    missing_bytes = sum(
        item.expected_download_bytes for item in items if not item.present
    )
    headroom_bytes = math.ceil(missing_bytes * DOWNLOAD_HEADROOM_FRACTION)
    reserve_bytes = max(
        MINIMUM_DISK_RESERVE_BYTES,
        math.ceil(disk_total_bytes * DISK_RESERVE_FRACTION),
    )
    required_bytes = missing_bytes + headroom_bytes + reserve_bytes
    projected_free = disk_free_bytes - missing_bytes - headroom_bytes
    shortfall = max(0, required_bytes - disk_free_bytes)
    return ModelDownloadBudget(
        profile=profile,
        estimate_only=True,
        disk_total_bytes=disk_total_bytes,
        disk_free_bytes=disk_free_bytes,
        disk_reserve_bytes=reserve_bytes,
        catalog_download_bytes=catalog_bytes,
        missing_download_bytes=missing_bytes,
        download_headroom_bytes=headroom_bytes,
        required_before_download_bytes=required_bytes,
        projected_free_after_bytes=projected_free,
        shortfall_bytes=shortfall,
        can_download=shortfall == 0,
        items=items,
    )


def presence_from_status(snapshot: ManagerStatusSnapshot) -> dict[str, bool]:
    cached = {model.model_id: model.cache_present for model in snapshot.models}
    return {
        "asr_accuracy": cached.get(ACCURACY_MODEL_ID, False),
        "asr_speed": cached.get(SPEED_MODEL_ID, False),
        "aligner": cached.get(ALIGNER_MODEL_ID, False),
        "ollama_accuracy": snapshot.ollama.accuracy_model_present,
        "ollama_editor": snapshot.ollama.editor_model_present,
    }


def model_catalog_for_profile(
    profile: ModelProfileName,
) -> tuple[ModelCatalogItem, ...]:
    catalog = {
        "asr_accuracy": (
            "asr_accuracy",
            ACCURACY_MODEL_ID,
            "mlx",
            MODEL_DOWNLOAD_BYTES[ACCURACY_MODEL_ID],
        ),
        "asr_speed": (
            "asr_speed",
            SPEED_MODEL_ID,
            "mlx",
            MODEL_DOWNLOAD_BYTES[SPEED_MODEL_ID],
        ),
        "aligner": (
            "aligner",
            ALIGNER_MODEL_ID,
            "mlx",
            MODEL_DOWNLOAD_BYTES[ALIGNER_MODEL_ID],
        ),
        "ollama_accuracy": (
            "ollama_accuracy",
            "qwen3:14b",
            "ollama",
            OLLAMA_ACCURACY_EXPECTED_BYTES,
        ),
        "ollama_editor": (
            "ollama_editor",
            "qwen3:8b",
            "ollama",
            OLLAMA_EDITOR_EXPECTED_BYTES,
        ),
    }
    keys = {
        "accuracy": ("asr_accuracy", "aligner", "ollama_accuracy", "ollama_editor"),
        "speed": ("asr_speed", "aligner", "ollama_editor"),
        "all": (
            "asr_accuracy",
            "asr_speed",
            "aligner",
            "ollama_accuracy",
            "ollama_editor",
        ),
    }[profile]
    return tuple(ModelCatalogItem(*catalog[key]) for key in keys)
