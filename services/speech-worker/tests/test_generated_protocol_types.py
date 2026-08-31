"""Cross-language generated protocol contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import get_args, get_type_hints

from speech_capture_worker.protocol_contract import PROTOCOL_VERSION, ProtocolCapability

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_ROOT = REPOSITORY_ROOT / "packages" / "protocol"
GENERATOR = PROTOCOL_ROOT / "scripts" / "generate_types.py"
GENERATED_PYTHON = (
    PROTOCOL_ROOT / "generated" / "python" / "speech_capture_protocol.py"
)
GENERATED_TYPESCRIPT = (
    PROTOCOL_ROOT / "generated" / "typescript" / "speech-capture-protocol.ts"
)


def _load_generated_python():
    spec = importlib.util.spec_from_file_location("speech_capture_protocol", GENERATED_PYTHON)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_generated_types_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_generation_is_deterministic_in_an_empty_directory(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(GENERATOR),
        "--output-root",
        str(tmp_path),
    ]
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
    first = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
    second = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    assert first == second
    assert first[Path("generated/python/speech_capture_protocol.py")] == (
        GENERATED_PYTHON.read_bytes()
    )
    assert first[Path("generated/typescript/speech-capture-protocol.ts")] == (
        GENERATED_TYPESCRIPT.read_bytes()
    )


def test_check_mode_detects_a_stale_generated_file(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, str(GENERATOR), "--output-root", str(tmp_path)],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    stale_python = tmp_path / "generated/python/speech_capture_protocol.py"
    stale_python.write_text(
        stale_python.read_text(encoding="utf-8") + "# stale\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--output-root",
            str(tmp_path),
            "--check",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert str(stale_python) in result.stderr


def test_python_types_preserve_versions_enums_and_optional_fields() -> None:
    generated = _load_generated_python()
    openapi_sha256 = hashlib.sha256((PROTOCOL_ROOT / "openapi.json").read_bytes()).hexdigest()

    assert generated.OPENAPI_SHA256 == openapi_sha256
    assert generated.PROTOCOL_VERSION == PROTOCOL_VERSION
    assert set(get_args(generated.ProtocolCapability)) == {
        capability.value for capability in ProtocolCapability
    }
    request_hints = get_type_hints(generated.CompatibilityRequestSchema)
    assert set(request_hints) == {"artifact_schema", "protocol", "required_features"}
    assert generated.CompatibilityRequestSchema.__required_keys__ == {
        "artifact_schema",
        "protocol",
    }
    assert generated.CompatibilityRequestSchema.__optional_keys__ == {"required_features"}


def test_typescript_types_preserve_wire_names_and_readonly_arrays() -> None:
    source = GENERATED_TYPESCRIPT.read_text(encoding="utf-8")

    assert f'export const PROTOCOL_VERSION = "{PROTOCOL_VERSION}" as const;' in source
    assert "export interface CompatibilityRequestSchema" in source
    assert "readonly required_features?: ReadonlyArray<string>;" in source
    assert "readonly protocol_version: string | null;" in source
    for capability in ProtocolCapability:
        assert f'"{capability.value}"' in source
