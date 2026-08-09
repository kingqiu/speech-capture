#!/usr/bin/env python3
"""Build and verify a self-contained macOS arm64 Worker command runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path, PurePosixPath

RUNTIME_NAME = "SpeechCaptureWorker"
RUNTIME_SCHEMA_VERSION = "1.0.0"
DYNAMIC_SUBMODULE_PACKAGES = (
    "uvicorn",
    "mlx",
    "mlx_qwen3_asr",
    "pyannote.audio",
)
DATA_PACKAGES = ("pyannote.audio",)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist") / RUNTIME_NAME,
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.check and args.verify:
        parser.error("--check and --verify cannot be combined")
    if args.verify:
        output = _safe_output(project, args.output_dir)
        if not output.is_dir():
            raise SystemExit("The standalone runtime output does not exist.")
        _verify_manifest(output)
        _verify_runtime(output, project)
        print(json.dumps(_public_result(output), sort_keys=True))
        return 0
    prerequisites = _prerequisites(project)
    if args.check:
        print(json.dumps(prerequisites, sort_keys=True))
        return 0 if all(prerequisites.values()) else 2
    if not all(prerequisites.values()):
        raise SystemExit("Standalone runtime prerequisites are incomplete.")

    output = _safe_output(project, args.output_dir)
    build_root = project / "build" / "standalone-runtime"
    pyinstaller_dist = build_root / "dist"
    _remove_owned_directory(build_root, project / "build")
    _remove_owned_directory(output, project / "dist")
    build_root.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(build_root / "pyinstaller-cache")
    os.environ["MPLCONFIGDIR"] = str(build_root / "matplotlib-cache")

    from PyInstaller.__main__ import run as run_pyinstaller

    entry = project / "src" / "speech_capture_worker" / "runtime_entry.py"
    arguments = [
        str(entry),
        "--name",
        "speech-capture-runtime",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--paths",
        str(project / "src"),
        "--copy-metadata",
        "speech-capture-worker",
        "--copy-metadata",
        "mlx-qwen3-asr",
        "--copy-metadata",
        "pyannote.audio",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
    ]
    for package in DYNAMIC_SUBMODULE_PACKAGES:
        arguments.extend(("--collect-submodules", package))
    for package in DATA_PACKAGES:
        arguments.extend(("--collect-data", package))
    purelib = Path(sysconfig.get_paths()["purelib"])
    qwen_package = purelib / "mlx_qwen3_asr"
    mlx_package = purelib / "mlx"
    arguments.extend(("--add-data", f"{qwen_package / 'assets'}:mlx_qwen3_asr/assets"))
    arguments.extend(("--add-data", f"{mlx_package / 'lib' / 'mlx.metallib'}:mlx/lib"))
    for binary in ("ffmpeg", "ffprobe"):
        arguments.extend(("--add-binary", f"{shutil.which(binary)}:."))
    run_pyinstaller(arguments)

    frozen = pyinstaller_dist / "speech-capture-runtime"
    if not (frozen / "speech-capture-runtime").is_file():
        raise SystemExit("PyInstaller did not produce the expected onedir runtime.")
    libexec = output / "libexec"
    shutil.copytree(frozen, libexec, symlinks=True)
    _remove_private_build_metadata(libexec)
    bin_dir = output / "bin"
    bin_dir.mkdir(mode=0o755)
    _write_launcher(bin_dir / "speech-capture-worker", "worker")
    _write_launcher(bin_dir / "speech-capture-manager", "manager")
    _write_text(
        output / "README.txt",
        "Speech Capture Worker standalone runtime\n"
        "Use bin/speech-capture-worker and bin/speech-capture-manager.\n"
        "Models and private Worker data are stored outside this runtime.\n",
        0o644,
    )
    _write_manifest(output)
    _verify_manifest(output)
    _verify_runtime(output, project)
    print(json.dumps(_public_result(output), sort_keys=True))
    return 0


def _prerequisites(project: Path) -> dict[str, bool]:
    try:
        import PyInstaller  # noqa: F401

        pyinstaller = True
    except ImportError:
        pyinstaller = False
    return {
        "darwin": platform.system() == "Darwin",
        "arm64": platform.machine() == "arm64",
        "python_3_11": sys.version_info[:2] == (3, 11),
        "pyinstaller": pyinstaller,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "entrypoint": (
            project / "src" / "speech_capture_worker" / "runtime_entry.py"
        ).is_file(),
    }


def _safe_output(project: Path, requested: Path) -> Path:
    output = requested if requested.is_absolute() else project / requested
    output = output.resolve()
    allowed = (project / "dist").resolve()
    if output == allowed or allowed not in output.parents:
        raise SystemExit("Standalone output must be a child of the project dist directory.")
    return output


def _remove_owned_directory(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    allowed = parent.resolve()
    if resolved == allowed or allowed not in resolved.parents:
        raise SystemExit("Refusing to remove a directory outside the owned build roots.")
    if resolved.exists():
        shutil.rmtree(resolved)


def _write_launcher(path: Path, mode: str) -> None:
    content = (
        "#!/bin/sh\n"
        "set -eu\n"
        'runtime_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        f'exec "$runtime_root/libexec/speech-capture-runtime" {mode} "$@"\n'
    )
    _write_text(path, content, 0o755)


def _write_text(path: Path, content: str, mode: int) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _write_manifest(root: Path) -> None:
    files = []
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() or item.is_symlink()
    ):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
            if root.resolve() not in resolved.parents:
                raise SystemExit("The runtime contains an escaping symbolic link.")
            files.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "target": target,
                    "sha256": hashlib.sha256(target.encode()).hexdigest(),
                }
            )
        else:
            files.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": path.stat().st_size,
                    "mode": oct(stat.S_IMODE(path.stat().st_mode)),
                    "sha256": _sha256(path),
                }
            )
    manifest = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "platform": "macos-arm64",
        "files": files,
    }
    _write_text(
        root / "runtime-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        0o644,
    )


def _verify_runtime(root: Path, project: Path) -> None:
    minimal_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment = {
        "PATH": minimal_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    commands = (
        ((root / "bin" / "speech-capture-worker", "--help"), 60),
        ((root / "bin" / "speech-capture-manager", "--help"), 60),
        (
            (root / "bin" / "speech-capture-worker", "verify-model-runtime"),
            180,
        ),
    )
    for command, timeout_seconds in commands:
        completed = subprocess.run(
            command,
            cwd="/private/tmp",
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit("A frozen runtime entrypoint failed its isolated import check.")
    with tempfile.TemporaryDirectory(
        prefix="speech-capture-runtime-smoke-",
        dir="/private/tmp",
    ) as temporary:
        data_dir = Path(temporary) / "runtime"
        smoke_commands = (
            (
                root / "bin" / "speech-capture-worker",
                "init",
                "--data-dir",
                str(data_dir),
            ),
            (
                root / "bin" / "speech-capture-manager",
                "model-activation-status",
                "--data-dir",
                str(data_dir),
                "--executable",
                str(root / "bin" / "speech-capture-worker"),
            ),
        )
        for command in smoke_commands:
            completed = subprocess.run(
                command,
                cwd="/private/tmp",
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                raise SystemExit("A frozen runtime failed its isolated state smoke test.")
    _scan_forbidden_paths(root, project)
    signed = subprocess.run(
        (
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            str(root / "libexec" / "speech-capture-runtime"),
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if signed.returncode != 0:
        raise SystemExit("The frozen runtime failed the local macOS code-signature check.")


def _verify_manifest(root: Path) -> None:
    manifest_path = root / "runtime-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or payload.get("platform") != "macos-arm64"
        or not isinstance(payload.get("files"), list)
    ):
        raise SystemExit("The runtime manifest is invalid.")
    seen: set[str] = set()
    for entry in payload["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SystemExit("The runtime manifest contains an invalid entry.")
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in seen:
            raise SystemExit("The runtime manifest contains an unsafe path.")
        seen.add(relative.as_posix())
        path = root.joinpath(*relative.parts)
        if entry.get("type") == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry.get("target"):
                raise SystemExit("The runtime manifest symlink does not match the package.")
            target = os.readlink(path)
            if hashlib.sha256(target.encode()).hexdigest() != entry.get("sha256"):
                raise SystemExit("The runtime manifest symlink hash is invalid.")
            resolved = path.resolve(strict=True)
            if root.resolve() not in resolved.parents:
                raise SystemExit("The runtime contains an escaping symbolic link.")
        elif entry.get("type") == "file":
            if path.is_symlink() or not path.is_file():
                raise SystemExit("The runtime manifest file does not match the package.")
            if (
                path.stat().st_size != entry.get("bytes")
                or oct(stat.S_IMODE(path.stat().st_mode)) != entry.get("mode")
                or _sha256(path) != entry.get("sha256")
            ):
                raise SystemExit("The runtime manifest file hash is invalid.")
        else:
            raise SystemExit("The runtime manifest contains an unknown entry type.")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink()) and path != manifest_path
    }
    if actual != seen:
        raise SystemExit("The runtime manifest file set does not match the package.")


def _remove_private_build_metadata(root: Path) -> None:
    for path in root.rglob("direct_url.json"):
        path.unlink()


def _scan_forbidden_paths(root: Path, project: Path) -> None:
    forbidden = tuple(
        value.encode()
        for value in (str(project), str(Path.home()), ".venv/")
    )
    for path in (item for item in root.rglob("*") if item.is_file()):
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if root.resolve() not in resolved.parents:
                raise SystemExit("The runtime contains an escaping symbolic link.")
            continue
        with path.open("rb") as stream:
            tail = b""
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                content = tail + chunk
                if any(marker in content for marker in forbidden):
                    raise SystemExit("The runtime contains a private build path.")
                tail = content[-1024:]


def _public_result(root: Path) -> dict[str, object]:
    files = [path for path in root.rglob("*") if path.is_file()]
    return {
        "created": True,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "platform": "macos-arm64",
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "manifest_sha256": _sha256(root / "runtime-manifest.json"),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
