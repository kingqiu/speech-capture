"""Fail-closed network server configuration for the versioned Worker API."""

from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from speech_capture_worker.api import create_app
from speech_capture_worker.device_security import DeviceSecurityStore
from speech_capture_worker.errors import InvalidJobRequest
from speech_capture_worker.job_store import JobStore

DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8765


@dataclass(frozen=True)
class ServerConfig:
    data_dir: Path
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None

    def validated(self) -> ServerConfig:
        host = self.host.strip()
        if not host:
            raise InvalidJobRequest("The Worker listen host cannot be empty.")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise InvalidJobRequest("The Worker listen port must be between 1 and 65535.")
        if (self.ssl_certfile is None) != (self.ssl_keyfile is None):
            raise InvalidJobRequest("TLS requires both a certificate and a private key.")
        if not _is_loopback(host):
            _validate_private_bind_address(host)
            if self.ssl_certfile is None:
                raise InvalidJobRequest("Non-loopback Worker access requires TLS.")
        certfile = _validated_file(self.ssl_certfile, "TLS certificate")
        keyfile = _validated_file(self.ssl_keyfile, "TLS private key")
        if keyfile is not None:
            try:
                mode = stat.S_IMODE(keyfile.stat().st_mode)
            except OSError as exc:
                raise InvalidJobRequest("The TLS private key could not be inspected.") from exc
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise InvalidJobRequest(
                    "The TLS private key must not be accessible to other users."
                )
        return ServerConfig(
            data_dir=self.data_dir.resolve(),
            host=host,
            port=self.port,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )


def serve(config: ServerConfig, *, runner: Any | None = None) -> None:
    """Run one Worker API process and close durable stores on every exit path."""

    validated = config.validated()
    if runner is None:
        import uvicorn

        runner = uvicorn.run
    validated.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    from speech_capture_worker.resources import require_upload_storage_capacity

    jobs = JobStore(
        validated.data_dir / "worker.sqlite3",
        upload_capacity_check=require_upload_storage_capacity,
    )
    jobs.recover_interrupted_uploads()
    security = DeviceSecurityStore(validated.data_dir / "security.sqlite3")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from speech_capture_worker.background_processing import BackgroundProcessingService

    background = BackgroundProcessingService(validated.data_dir)
    background.start()
    try:

        def regenerate_summary(job_id: str) -> None:
            from speech_capture_worker.model_activation import (
                resolve_active_model_target,
            )
            from speech_capture_worker.structuring_execution import (
                OllamaStructuringEngine,
                StructuringExecutor,
            )

            job = jobs.get_job(job_id)
            profile = job.model_profile.value
            main_key = "ollama_accuracy" if profile == "accuracy" else "ollama_editor"
            StructuringExecutor(
                jobs,
                OllamaStructuringEngine(
                    model=resolve_active_model_target(
                        validated.data_dir,
                        profile=profile,
                        key=main_key,
                        fallback="qwen3:14b" if profile == "accuracy" else "qwen3:8b",
                    ),
                    editor_model=resolve_active_model_target(
                        validated.data_dir,
                        profile=profile,
                        key="ollama_editor",
                        fallback="qwen3:8b",
                    ),
                ),
            ).resynthesize_document(job_id)

        runner(
            create_app(
                store=jobs,
                credential_verifier=security,
                device_security_store=security,
                summary_regenerator=regenerate_summary,
                endpoint_mode=("local_only" if _is_loopback(validated.host) else "private_tls"),
                tls_enabled=validated.ssl_certfile is not None,
            ),
            host=validated.host,
            port=validated.port,
            ssl_certfile=(str(validated.ssl_certfile) if validated.ssl_certfile else None),
            ssl_keyfile=(str(validated.ssl_keyfile) if validated.ssl_keyfile else None),
            proxy_headers=False,
            server_header=False,
            date_header=False,
            access_log=False,
        )
    finally:
        background.stop()
        security.close()
        jobs.close()


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_private_bind_address(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise InvalidJobRequest(
            "A non-loopback Worker must bind to an explicit private-network IP address."
        ) from exc
    if address.is_unspecified or address.is_multicast or address.is_global:
        raise InvalidJobRequest("The Worker cannot bind to a public or wildcard address.")


def _validated_file(path: Path | None, label: str) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise InvalidJobRequest(f"The {label} is not a readable regular file.")
    return resolved
