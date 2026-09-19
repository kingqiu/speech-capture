"""Atomic model activation, switching, rollback, and resolution tests."""

from __future__ import annotations

import json
import stat

import pytest

from speech_capture_worker.errors import (
    ModelActivationFailed,
    ModelRollbackUnavailable,
)
from speech_capture_worker.model_activation import (
    ModelActivationManager,
    resolve_active_model_target,
)
from speech_capture_worker.model_validation import (
    ModelFileValidation,
    ModelValidationReport,
)


def test_first_activation_is_private_atomic_and_idempotent(tmp_path) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())

    first = manager.activate("accuracy")
    second = manager.activate("accuracy")
    payload = json.loads(manager.state_path.read_text(encoding="utf-8"))

    assert first.action == "activated"
    assert first.changed is True
    assert first.state.generation == 1
    assert first.state.active is not None
    assert first.state.active.profile == "accuracy"
    assert first.state.rollback is None
    assert second.action == "unchanged"
    assert second.changed is False
    assert second.state.generation == 1
    assert stat.S_IMODE(manager.models_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(manager.state_path.stat().st_mode) == 0o600
    assert payload["active"]["models"][0]["revision"] == "a" * 40
    assert str(tmp_path) not in manager.state_path.read_text(encoding="utf-8")


def test_switch_keeps_previous_profile_and_rollback_swaps_atomically(tmp_path) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())
    manager.activate("accuracy")

    switched = manager.switch("speed")
    rolled_back = manager.rollback()

    assert switched.action == "switched"
    assert switched.state.generation == 2
    assert switched.state.active is not None
    assert switched.state.active.profile == "speed"
    assert switched.state.rollback is not None
    assert switched.state.rollback.profile == "accuracy"
    assert rolled_back.action == "rolled_back"
    assert rolled_back.state.generation == 3
    assert rolled_back.state.active is not None
    assert rolled_back.state.active.profile == "accuracy"
    assert rolled_back.state.rollback is not None
    assert rolled_back.state.rollback.profile == "speed"


def test_switch_requires_an_existing_active_profile(tmp_path) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())

    with pytest.raises(ModelRollbackUnavailable):
        manager.switch("speed")
    with pytest.raises(ModelRollbackUnavailable):
        manager.rollback()


def test_failed_validation_preserves_current_activation(tmp_path) -> None:
    validator = _validator()
    manager = ModelActivationManager(tmp_path / "runtime", validator=validator)
    manager.activate("accuracy")
    before = manager.state_path.read_bytes()

    def invalid_validator(profile, _approvals):
        return ModelValidationReport(profile=profile, valid=False, models=())

    manager._validator = invalid_validator
    with pytest.raises(ModelActivationFailed):
        manager.switch("speed")

    assert manager.state_path.read_bytes() == before
    assert manager.status().active is not None
    assert manager.status().active.profile == "accuracy"


def test_atomic_write_failure_preserves_current_activation(
    tmp_path,
    monkeypatch,
) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())
    manager.activate("accuracy")
    before = manager.state_path.read_bytes()

    def fail_write(_path, _content):
        raise OSError("simulated")

    monkeypatch.setattr(
        "speech_capture_worker.model_activation._write_private_atomic",
        fail_write,
    )
    with pytest.raises(ModelActivationFailed):
        manager.switch("speed")

    assert manager.state_path.read_bytes() == before


def test_rollback_revalidates_recorded_revision_and_preserves_state_on_failure(
    tmp_path,
) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())
    manager.activate("accuracy")
    manager.switch("speed")
    before = manager.state_path.read_bytes()

    def changed_rollback_validator(profile, approvals):
        if approvals is not None:
            changed = {model_id: "f" * len(revision) for model_id, revision in approvals.items()}
            return _report(profile, changed)
        return _report(profile)

    manager._validator = changed_rollback_validator
    with pytest.raises(ModelActivationFailed):
        manager.rollback()

    assert manager.state_path.read_bytes() == before


def test_resolver_returns_pinned_snapshot_only_for_active_profile(tmp_path) -> None:
    data_dir = tmp_path / "runtime"
    hf_cache = tmp_path / "huggingface"
    manager = ModelActivationManager(
        data_dir,
        huggingface_cache=hf_cache,
        validator=_validator(),
    )
    manager.activate("speed")
    snapshot = (
        hf_cache
        / "models--Qwen--Qwen3-ASR-0.6B"
        / "snapshots"
        / ("b" * 40)
    )
    snapshot.mkdir(parents=True)

    resolved = resolve_active_model_target(
        data_dir,
        profile="speed",
        key="asr_speed",
        fallback="Qwen/Qwen3-ASR-0.6B",
        huggingface_cache=hf_cache,
    )
    fallback = resolve_active_model_target(
        data_dir,
        profile="accuracy",
        key="asr_accuracy",
        fallback="Qwen/Qwen3-ASR-1.7B",
        huggingface_cache=hf_cache,
    )

    assert resolved == str(snapshot.resolve())
    assert fallback == "Qwen/Qwen3-ASR-1.7B"


def test_tampered_activation_model_identity_is_rejected(tmp_path) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())
    manager.activate("speed")
    payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
    payload["active"]["models"][0]["model_id"] = "../../private"
    manager.state_path.write_text(json.dumps(payload), encoding="utf-8")
    manager.state_path.chmod(0o600)

    with pytest.raises(ModelActivationFailed):
        manager.status()


def test_activation_state_symlink_is_rejected(tmp_path) -> None:
    manager = ModelActivationManager(tmp_path / "runtime", validator=_validator())
    manager.activate("accuracy")
    outside = tmp_path / "outside.json"
    outside.write_text(manager.state_path.read_text(encoding="utf-8"), encoding="utf-8")
    manager.state_path.unlink()
    manager.state_path.symlink_to(outside)

    with pytest.raises(ModelActivationFailed):
        manager.status()


def _validator():
    def validate(profile, approvals):
        return _report(profile, approvals)

    return validate


def _report(profile, approvals=None) -> ModelValidationReport:
    catalog = {
        "accuracy": (
            ("asr_accuracy", "Qwen/Qwen3-ASR-1.7B", "mlx", "a" * 40),
            ("aligner", "Qwen/Qwen3-ForcedAligner-0.6B", "mlx", "c" * 40),
            ("ollama_accuracy", "qwen3:14b", "ollama", "d" * 64),
            ("ollama_editor", "qwen3:8b", "ollama", "e" * 64),
        ),
        "speed": (
            ("asr_speed", "Qwen/Qwen3-ASR-0.6B", "mlx", "b" * 40),
            ("aligner", "Qwen/Qwen3-ForcedAligner-0.6B", "mlx", "c" * 40),
            ("ollama_editor", "qwen3:8b", "ollama", "e" * 64),
        ),
        "all": (
            ("asr_accuracy", "Qwen/Qwen3-ASR-1.7B", "mlx", "a" * 40),
            ("asr_speed", "Qwen/Qwen3-ASR-0.6B", "mlx", "b" * 40),
            ("aligner", "Qwen/Qwen3-ForcedAligner-0.6B", "mlx", "c" * 40),
            ("ollama_accuracy", "qwen3:14b", "ollama", "d" * 64),
            ("ollama_editor", "qwen3:8b", "ollama", "e" * 64),
        ),
    }[profile]
    models = []
    for key, model_id, provider, default_revision in catalog:
        revision = approvals.get(model_id, default_revision) if approvals else default_revision
        models.append(
            ModelFileValidation(
                key=key,
                model_id=model_id,
                provider=provider,
                state="valid",
                revision=revision,
                checked_file_count=1,
                checked_bytes=1,
                full_hash_verified=True,
                issue_codes=(),
            )
        )
    return ModelValidationReport(profile=profile, valid=True, models=tuple(models))
