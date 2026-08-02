"""Local macOS Worker Manager command surface for launchd lifecycle operations."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from speech_capture_worker.errors import InvalidJobRequest, WorkerCoreError
from speech_capture_worker.launchd_service import (
    DEFAULT_LAUNCHD_LABEL,
    LaunchdServiceConfig,
    LaunchdServiceManager,
    default_agent_path,
    default_data_dir,
)
from speech_capture_worker.manager_status import collect_manager_status
from speech_capture_worker.redaction import public_cli_error_payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        manager = LaunchdServiceManager()
        if args.command == "status":
            service = manager.status(config)
            status = collect_manager_status(config, service)
            print(json.dumps({"status": status.to_dict()}, sort_keys=True))
        else:
            operation = getattr(manager, args.command)
            service = operation(config)
            print(json.dumps({"service": service.to_dict()}, sort_keys=True))
        return 0
    except WorkerCoreError as exc:
        print(
            json.dumps({"error": public_cli_error_payload(exc.code, exc.message)}),
            file=sys.stderr,
        )
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speech-capture-manager",
        description="Install and control the per-user Speech Capture Worker service.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "start", "stop", "restart", "status", "uninstall"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--data-dir", type=Path, default=default_data_dir())
        subparser.add_argument("--executable", type=Path)
        subparser.add_argument("--host", default="127.0.0.1")
        subparser.add_argument("--port", type=int, default=8765)
        subparser.add_argument("--ssl-certfile", type=Path)
        subparser.add_argument("--ssl-keyfile", type=Path)
    return parser


def _config_from_args(args: argparse.Namespace) -> LaunchdServiceConfig:
    executable = args.executable or _find_worker_executable()
    return LaunchdServiceConfig(
        executable=executable,
        data_dir=args.data_dir,
        agent_path=default_agent_path(label=DEFAULT_LAUNCHD_LABEL),
        label=DEFAULT_LAUNCHD_LABEL,
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


def _find_worker_executable() -> Path:
    candidate = shutil.which("speech-capture-worker")
    if candidate is None:
        raise InvalidJobRequest(
            "The Worker executable was not found; provide --executable explicitly."
        )
    return Path(candidate)


if __name__ == "__main__":
    raise SystemExit(main())
