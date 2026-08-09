"""Standalone runtime dispatcher and deterministic package metadata tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from speech_capture_worker import runtime_entry


@pytest.mark.parametrize(
    ("mode", "module_name"),
    [
        ("worker", "speech_capture_worker.worker_cli"),
        ("manager", "speech_capture_worker.manager_cli"),
    ],
)
def test_runtime_dispatches_mode_without_leaving_mode_in_argv(
    mode,
    module_name,
    monkeypatch,
) -> None:
    selected = ModuleType(module_name)

    def selected_main():
        assert sys.argv == ["speech-capture-runtime", "--help"]
        return 7

    selected.main = selected_main
    monkeypatch.setitem(sys.modules, module_name, selected)
    monkeypatch.setattr(sys, "argv", ["speech-capture-runtime", mode, "--help"])

    assert runtime_entry.main() == 7


def test_runtime_rejects_missing_mode(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["speech-capture-runtime"])

    with pytest.raises(SystemExit) as raised:
        runtime_entry.main()

    assert raised.value.code == 2
    assert "requires worker or manager mode" in capsys.readouterr().err


def test_runtime_prepends_frozen_internal_directory_to_path(monkeypatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", "/private/frozen", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    runtime_entry._prepare_bundled_binary_path()

    assert runtime_entry.os.environ["PATH"] == "/private/frozen:/usr/bin:/bin"


def test_build_manifest_records_files_without_private_absolute_paths(tmp_path) -> None:
    build = _load_build_module()
    root = tmp_path / "SpeechCaptureWorker"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "speech-capture-worker").write_text("launcher", encoding="utf-8")
    (root / "README.txt").write_text("safe", encoding="utf-8")

    build._write_manifest(root)
    manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "1.0.0"
    assert manifest["platform"] == "macos-arm64"
    assert [entry["path"] for entry in manifest["files"]] == [
        "README.txt",
        "bin/speech-capture-worker",
    ]
    assert all(entry["type"] == "file" for entry in manifest["files"])
    assert str(tmp_path) not in json.dumps(manifest)


def test_build_manifest_records_internal_symlinks_and_rejects_escape(tmp_path) -> None:
    build = _load_build_module()
    root = tmp_path / "SpeechCaptureWorker"
    libraries = root / "libexec" / "_internal"
    libraries.mkdir(parents=True)
    target = libraries / "libmodel.dylib"
    target.write_bytes(b"model")
    link = libraries / "libactive.dylib"
    link.symlink_to("libmodel.dylib")

    build._write_manifest(root)
    manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
    link_entry = next(
        entry
        for entry in manifest["files"]
        if entry["path"].endswith("libactive.dylib")
    )
    assert link_entry["type"] == "symlink"
    assert link_entry["target"] == "libmodel.dylib"

    outside = tmp_path / "outside.dylib"
    outside.write_bytes(b"outside")
    link.unlink()
    link.symlink_to(outside)
    with pytest.raises(SystemExit, match="escaping symbolic link"):
        build._write_manifest(root)


def test_build_manifest_verification_detects_changed_file(tmp_path) -> None:
    build = _load_build_module()
    root = tmp_path / "SpeechCaptureWorker"
    root.mkdir()
    payload = root / "payload.bin"
    payload.write_bytes(b"before")
    build._write_manifest(root)
    build._verify_manifest(root)

    payload.write_bytes(b"after")
    with pytest.raises(SystemExit, match="file hash is invalid"):
        build._verify_manifest(root)


def test_build_output_cannot_escape_project_dist(tmp_path) -> None:
    build = _load_build_module()
    project = tmp_path / "project"
    (project / "dist").mkdir(parents=True)

    assert build._safe_output(project, Path("dist/runtime")) == (
        project / "dist" / "runtime"
    ).resolve()
    with pytest.raises(SystemExit, match="child of the project dist"):
        build._safe_output(project, tmp_path / "outside")


def test_standalone_build_collects_dynamic_model_backends() -> None:
    build = _load_build_module()

    assert build.DYNAMIC_SUBMODULE_PACKAGES == (
        "uvicorn",
        "mlx",
        "mlx_qwen3_asr",
        "pyannote.audio",
    )
    assert build.DATA_PACKAGES == ("pyannote.audio",)


def _load_build_module():
    path = Path(__file__).parents[1] / "scripts" / "build_standalone_runtime.py"
    specification = importlib.util.spec_from_file_location(
        "speech_capture_build_standalone_runtime",
        path,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
