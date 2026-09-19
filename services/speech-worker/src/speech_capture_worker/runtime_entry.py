"""Dispatch the frozen standalone runtime without importing both CLIs eagerly."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn


def main() -> int:
    _prepare_bundled_binary_path()
    if len(sys.argv) < 2 or sys.argv[1] not in {"worker", "manager"}:
        _fail("The standalone runtime requires worker or manager mode.")
    mode = sys.argv.pop(1)
    if mode == "worker":
        from speech_capture_worker.worker_cli import main as selected_main
    else:
        from speech_capture_worker.manager_cli import main as selected_main
    return selected_main()


def _prepare_bundled_binary_path() -> None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(bundle_root, str) or not bundle_root:
        return
    existing = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        value for value in (bundle_root, existing) if value
    )


def _fail(message: str) -> NoReturn:
    executable = Path(sys.argv[0]).name
    print(f"{executable}: {message}", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
