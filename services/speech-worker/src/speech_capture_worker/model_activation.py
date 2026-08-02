"""Atomic validated model-profile activation and one-step rollback."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.errors import (
    ModelActivationFailed,
    ModelRollbackUnavailable,
)
from speech_capture_worker.model_budget import ModelProfileName, model_catalog_for_profile
from speech_capture_worker.model_validation import (
    ModelValidationReport,
    validate_model_profile,
)

ACTIVATION_SCHEMA_VERSION = "1.0.0"
MODEL_RELEASE_ID = "2026-08-02.1"
SAFE_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True)
class ActivatedModel:
    key: str
    model_id: str
    provider: str
    revision: str


@dataclass(frozen=True)
class ActivationSnapshot:
    profile: ModelProfileName
    release_id: str
    models: tuple[ActivatedModel, ...]


@dataclass(frozen=True)
class ModelActivationState:
    schema_version: str
    generation: int
    active: ActivationSnapshot | None
    rollback: ActivationSnapshot | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelActivationResult:
    action: str
    changed: bool
    state: ModelActivationState

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProfileValidator = Callable[[ModelProfileName, dict[str, str] | None], ModelValidationReport]


class ModelActivationManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        huggingface_cache: Path | None = None,
        ollama_models_dir: Path | None = None,
        validator: ProfileValidator | None = None,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.models_dir = self.data_dir / "models"
        self.state_path = self.models_dir / "activation.json"
        self.lock_path = self.models_dir / "activation.lock"
        self.huggingface_cache = huggingface_cache
        self.ollama_models_dir = ollama_models_dir
        self._validator = validator or self._validate

    def status(self) -> ModelActivationState:
        with self._locked():
            return self._read_state()

    def activate(self, profile: ModelProfileName) -> ModelActivationResult:
        return self._activate(profile, require_existing=False)

    def switch(self, profile: ModelProfileName) -> ModelActivationResult:
        return self._activate(profile, require_existing=True)

    def rollback(self) -> ModelActivationResult:
        with self._locked():
            current = self._read_state()
            previous = current.rollback
            if previous is None:
                raise ModelRollbackUnavailable(
                    "No previously active model profile is available for rollback."
                )
            approvals = {model.model_id: model.revision for model in previous.models}
            report = self._validator(previous.profile, approvals)
            self._require_valid(report, previous.profile)
            restored = _snapshot_from_report(previous.profile, report)
            if restored != previous:
                raise ModelActivationFailed(
                    "The rollback candidate no longer matches its recorded model identity."
                )
            state = ModelActivationState(
                schema_version=ACTIVATION_SCHEMA_VERSION,
                generation=current.generation + 1,
                active=previous,
                rollback=current.active,
            )
            self._write_state(state)
            return ModelActivationResult("rolled_back", True, state)

    def _activate(
        self,
        profile: ModelProfileName,
        *,
        require_existing: bool,
    ) -> ModelActivationResult:
        with self._locked():
            current = self._read_state()
            if require_existing and current.active is None:
                raise ModelRollbackUnavailable(
                    "No active model profile exists to switch from."
                )
            report = self._validator(profile, None)
            self._require_valid(report, profile)
            candidate = _snapshot_from_report(profile, report)
            if candidate == current.active:
                return ModelActivationResult("unchanged", False, current)
            action = "activated" if current.active is None else "switched"
            state = ModelActivationState(
                schema_version=ACTIVATION_SCHEMA_VERSION,
                generation=current.generation + 1,
                active=candidate,
                rollback=current.active,
            )
            self._write_state(state)
            return ModelActivationResult(action, True, state)

    def _validate(
        self,
        profile: ModelProfileName,
        approvals: dict[str, str] | None,
    ) -> ModelValidationReport:
        return validate_model_profile(
            profile,
            huggingface_cache=self.huggingface_cache,
            ollama_models_dir=self.ollama_models_dir,
            approved_revisions=approvals,
        )

    @staticmethod
    def _require_valid(
        report: ModelValidationReport,
        profile: ModelProfileName,
    ) -> None:
        if report.profile != profile or not report.valid:
            raise ModelActivationFailed(
                "The requested model profile did not pass full local validation."
            )
        if not report.models or any(
            model.state != "valid"
            or not model.full_hash_verified
            or not model.revision
            for model in report.models
        ):
            raise ModelActivationFailed(
                "The requested model profile has incomplete validation evidence."
            )

    def _read_state(self) -> ModelActivationState:
        try:
            payload = _read_private_json(self.state_path)
        except FileNotFoundError:
            return ModelActivationState(ACTIVATION_SCHEMA_VERSION, 0, None, None)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelActivationFailed(
                "The model activation state could not be read safely."
            ) from exc
        try:
            return _state_from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelActivationFailed(
                "The model activation state is invalid."
            ) from exc

    def _write_state(self, state: ModelActivationState) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.models_dir, 0o700)
            content = json.dumps(
                state.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            _write_private_atomic(self.state_path, content)
        except OSError as exc:
            raise ModelActivationFailed(
                "The model activation state could not be committed atomically."
            ) from exc

    def _locked(self):
        return _ActivationLock(self.models_dir, self.lock_path)


class _ActivationLock:
    def __init__(self, models_dir: Path, lock_path: Path) -> None:
        self.models_dir = models_dir
        self.lock_path = lock_path
        self.stream = None

    def __enter__(self):
        try:
            if self.models_dir.is_symlink():
                raise ModelActivationFailed("The model activation directory is unsafe.")
            self.models_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.models_dir, 0o700)
            if self.lock_path.is_symlink():
                raise ModelActivationFailed("The model activation lock is unsafe.")
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            self.stream = os.fdopen(descriptor, "rb+")
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            raise ModelActivationFailed(
                "The model activation lock could not be acquired."
            ) from exc
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def resolve_active_model_target(
    data_dir: Path,
    *,
    profile: ModelProfileName,
    key: str,
    fallback: str,
    huggingface_cache: Path | None = None,
) -> str:
    manager = ModelActivationManager(data_dir, huggingface_cache=huggingface_cache)
    state = manager.status()
    active = state.active
    if active is None or active.profile != profile:
        return fallback
    selected = next((model for model in active.models if model.key == key), None)
    if selected is None:
        return fallback
    if selected.provider == "ollama":
        return selected.model_id
    if selected.provider != "mlx":
        raise ModelActivationFailed("The active model provider is unsupported.")
    cache = (
        huggingface_cache.expanduser().resolve()
        if huggingface_cache is not None
        else _default_huggingface_cache()
    )
    snapshot = (
        cache
        / f"models--{selected.model_id.replace('/', '--')}"
        / "snapshots"
        / selected.revision
    )
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ModelActivationFailed("The active model snapshot is unavailable.")
    return str(snapshot.resolve())


def _snapshot_from_report(
    profile: ModelProfileName,
    report: ModelValidationReport,
) -> ActivationSnapshot:
    return ActivationSnapshot(
        profile=profile,
        release_id=MODEL_RELEASE_ID,
        models=tuple(
            ActivatedModel(
                key=model.key,
                model_id=model.model_id,
                provider=model.provider,
                revision=str(model.revision),
            )
            for model in report.models
        ),
    )


def _state_from_payload(payload: Any) -> ModelActivationState:
    if not isinstance(payload, dict):
        raise ValueError
    if payload.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        raise ValueError
    generation = payload.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError
    return ModelActivationState(
        schema_version=ACTIVATION_SCHEMA_VERSION,
        generation=generation,
        active=_snapshot_from_payload(payload.get("active")),
        rollback=_snapshot_from_payload(payload.get("rollback")),
    )


def _snapshot_from_payload(payload: Any) -> ActivationSnapshot | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError
    profile = payload.get("profile")
    if profile not in {"accuracy", "speed", "all"}:
        raise ValueError
    release_id = payload.get("release_id")
    models_payload = payload.get("models")
    if (
        not isinstance(release_id, str)
        or SAFE_RELEASE_ID.fullmatch(release_id) is None
        or not isinstance(models_payload, list)
    ):
        raise ValueError
    models = tuple(_activated_model_from_payload(model) for model in models_payload)
    expected = tuple(
        (item.key, item.model_id, item.provider)
        for item in model_catalog_for_profile(profile)
    )
    actual = tuple((model.key, model.model_id, model.provider) for model in models)
    if actual != expected:
        raise ValueError
    return ActivationSnapshot(profile, release_id, models)


def _activated_model_from_payload(payload: Any) -> ActivatedModel:
    if not isinstance(payload, dict):
        raise ValueError
    values = tuple(payload.get(key) for key in ("key", "model_id", "provider", "revision"))
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError
    key, model_id, provider, revision = values
    if provider not in {"mlx", "ollama"} or SAFE_REVISION.fullmatch(revision) is None:
        raise ValueError
    return ActivatedModel(key, model_id, provider, revision)


def _read_private_json(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size > 1024 * 1024
        ):
            raise OSError("Unsafe activation state file.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise OSError("Oversized activation state file.")
        return json.loads(content)
    finally:
        os.close(descriptor)


def _write_private_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _default_huggingface_cache() -> Path:
    override = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home).expanduser().resolve() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"
