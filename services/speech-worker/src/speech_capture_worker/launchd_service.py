"""macOS per-user LaunchAgent lifecycle for the long-running Worker API."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from speech_capture_worker.errors import (
    InvalidJobRequest,
    ServiceCommandFailed,
    ServiceInstallConflict,
    ServiceNotInstalled,
    ServiceUnsupported,
)
from speech_capture_worker.server import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, ServerConfig

DEFAULT_LAUNCHD_LABEL = "com.speechcapture.worker"
SAFE_LAUNCHD_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$")


@dataclass(frozen=True)
class LaunchdCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class LaunchdServiceStatus:
    label: str
    installed: bool
    loaded: bool
    running: bool
    state: str
    pid: int | None
    runs: int | None
    last_exit_status: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaunchdServiceConfig:
    executable: Path
    data_dir: Path
    agent_path: Path
    label: str = DEFAULT_LAUNCHD_LABEL
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def validated(self) -> LaunchdServiceConfig:
        if not SAFE_LAUNCHD_LABEL.fullmatch(self.label):
            raise InvalidJobRequest("The launchd service label is invalid.")
        executable = self.executable.expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise InvalidJobRequest("The Worker executable is not an executable regular file.")
        agent_path = self.agent_path.expanduser().resolve()
        if agent_path.name != f"{self.label}.plist":
            raise InvalidJobRequest("The LaunchAgent filename must match its service label.")
        server = ServerConfig(
            data_dir=self.data_dir,
            host=self.host,
            port=self.port,
            ssl_certfile=self.ssl_certfile,
            ssl_keyfile=self.ssl_keyfile,
        ).validated()
        return LaunchdServiceConfig(
            executable=executable,
            data_dir=server.data_dir,
            agent_path=agent_path,
            label=self.label,
            host=server.host,
            port=server.port,
            ssl_certfile=server.ssl_certfile,
            ssl_keyfile=server.ssl_keyfile,
        )


CommandRunner = Callable[[Sequence[str]], LaunchdCommandResult]


class LaunchdServiceManager:
    def __init__(
        self,
        *,
        uid: int | None = None,
        platform: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.uid = os.getuid() if uid is None else uid
        self.platform = sys.platform if platform is None else platform
        self._runner = runner or _run_launchctl

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def service_target(self, label: str) -> str:
        return f"{self.domain}/{label}"

    def install(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        content = render_launch_agent(validated)
        if validated.agent_path.exists():
            try:
                existing = validated.agent_path.read_bytes()
            except OSError as exc:
                raise ServiceInstallConflict(
                    "The existing Worker LaunchAgent could not be inspected."
                ) from exc
            if existing != content:
                raise ServiceInstallConflict(
                    "A different Worker LaunchAgent is already installed."
                )
        else:
            validated.agent_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            validated.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            validated.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private_atomic(validated.agent_path, content)
        status = self.status(validated)
        if not status.loaded:
            self._require_success(("bootstrap", self.domain, str(validated.agent_path)))
        return self.status(validated)

    def start(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        if not validated.agent_path.is_file():
            raise ServiceNotInstalled("The Worker LaunchAgent is not installed.")
        status = self.status(validated)
        if not status.loaded:
            self._require_success(("bootstrap", self.domain, str(validated.agent_path)))
        elif not status.running:
            self._require_success(("kickstart", self.service_target(validated.label)))
        return self.status(validated)

    def stop(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        status = self.status(validated)
        if status.loaded:
            self._require_success(("bootout", self.service_target(validated.label)))
        return self.status(validated)

    def restart(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        if not validated.agent_path.is_file():
            raise ServiceNotInstalled("The Worker LaunchAgent is not installed.")
        status = self.status(validated)
        if not status.loaded:
            return self.start(validated)
        self._require_success(("kickstart", "-k", self.service_target(validated.label)))
        return self.status(validated)

    def uninstall(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        status = self.stop(validated)
        if validated.agent_path.exists():
            try:
                validated.agent_path.unlink()
            except OSError as exc:
                raise ServiceCommandFailed(
                    "The Worker LaunchAgent could not be removed."
                ) from exc
        return LaunchdServiceStatus(
            label=validated.label,
            installed=False,
            loaded=status.loaded,
            running=status.running,
            state="not_installed",
            pid=None,
            runs=status.runs,
            last_exit_status=status.last_exit_status,
        )

    def status(self, config: LaunchdServiceConfig) -> LaunchdServiceStatus:
        self._require_macos()
        validated = config.validated()
        result = self._runner(("print", self.service_target(validated.label)))
        installed = validated.agent_path.is_file()
        if result.returncode != 0:
            return LaunchdServiceStatus(
                label=validated.label,
                installed=installed,
                loaded=False,
                running=False,
                state="stopped" if installed else "not_installed",
                pid=None,
                runs=None,
                last_exit_status=None,
            )
        parsed = parse_launchctl_print(result.stdout)
        state = parsed.get("state", "loaded")
        pid = _optional_int(parsed.get("pid"))
        return LaunchdServiceStatus(
            label=validated.label,
            installed=installed,
            loaded=True,
            running=state in {"running", "active"} and pid is not None,
            state=state,
            pid=pid,
            runs=_optional_int(parsed.get("runs")),
            last_exit_status=_optional_int(parsed.get("last_exit_status")),
        )

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise ServiceUnsupported("Worker background-service management requires macOS.")

    def _require_success(self, arguments: Sequence[str]) -> None:
        result = self._runner(arguments)
        if result.returncode != 0:
            raise ServiceCommandFailed(
                "macOS launchd rejected the Worker service operation.",
                details={"return_code": result.returncode},
            )


def default_data_dir(home: Path | None = None) -> Path:
    root = (home or Path.home()).expanduser().resolve()
    return root / "Library" / "Application Support" / "Speech Capture Worker"


def default_agent_path(
    *,
    label: str = DEFAULT_LAUNCHD_LABEL,
    home: Path | None = None,
) -> Path:
    root = (home or Path.home()).expanduser().resolve()
    return root / "Library" / "LaunchAgents" / f"{label}.plist"


def render_launch_agent(config: LaunchdServiceConfig) -> bytes:
    validated = config.validated()
    arguments = [
        str(validated.executable),
        "serve",
        "--data-dir",
        str(validated.data_dir),
        "--host",
        validated.host,
        "--port",
        str(validated.port),
    ]
    if validated.ssl_certfile is not None and validated.ssl_keyfile is not None:
        arguments.extend((
            "--ssl-certfile",
            str(validated.ssl_certfile),
            "--ssl-keyfile",
            str(validated.ssl_keyfile),
        ))
    payload = {
        "Label": validated.label,
        "Program": str(validated.executable),
        "ProgramArguments": arguments,
        "WorkingDirectory": str(validated.data_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Standard",
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        "Umask": 0o077,
        "StandardOutPath": str(validated.log_dir / "worker.stdout.log"),
        "StandardErrorPath": str(validated.log_dir / "worker.stderr.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def parse_launchctl_print(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    patterns = {
        "state": re.compile(r"^\s*state\s*=\s*([^\s]+)\s*$"),
        "pid": re.compile(r"^\s*pid\s*=\s*(-?\d+)\s*$"),
        "runs": re.compile(r"^\s*runs\s*=\s*(\d+)\s*$"),
        "last_exit_status": re.compile(r"^\s*last exit (?:code|status)\s*=\s*(-?\d+)\s*$"),
    }
    for line in output.splitlines():
        for name, pattern in patterns.items():
            match = pattern.match(line)
            if match is not None:
                parsed[name] = match.group(1)
    return parsed


def _run_launchctl(arguments: Sequence[str]) -> LaunchdCommandResult:
    try:
        completed = subprocess.run(
            ("/bin/launchctl", *arguments),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceCommandFailed("macOS launchd could not be reached.") from exc
    return LaunchdCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _write_private_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None
