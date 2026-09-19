from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PLUGIN_ID = "speech-capture"
_RELEASE_FILES = ("main.js", "manifest.json", "styles.css")
_EXPECTED_ENTRIES = tuple(f"{_PLUGIN_ID}/{name}" for name in _RELEASE_FILES)
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_INSTALLER_BYTES = 256 * 1024
_MAX_RELEASE_FILE_BYTES = 8 * 1024 * 1024


class ClientReleaseError(ValueError):
    """Raised when a client release cannot be trusted or stored immutably."""


class ClientReleaseNotFound(ClientReleaseError):
    """Raised when an otherwise valid release version is not installed."""


@dataclass(frozen=True, slots=True)
class ClientPluginRelease:
    plugin_id: str
    version: str
    min_app_version: str
    archive_path: Path
    archive_sha256: str
    archive_size_bytes: int
    installer_path: Path
    installer_sha256: str
    installer_size_bytes: int
    main_sha256: str
    release_manifest_sha256: str


class ClientReleaseStore:
    """An immutable, task-independent store for verified client releases."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def import_release(self, release_manifest_path: Path) -> ClientPluginRelease:
        manifest_path = release_manifest_path.resolve()
        if release_manifest_path.is_symlink() or not manifest_path.is_file():
            raise ClientReleaseError("release manifest must be a regular file")
        manifest_bytes = _read_bounded(manifest_path, _MAX_MANIFEST_BYTES, "release manifest")
        payload = _load_strict_json(manifest_bytes)
        parsed = _parse_release_manifest(payload)
        expected_manifest_name = f"{_PLUGIN_ID}-{parsed['version']}-release.json"
        if manifest_path.name != expected_manifest_name:
            raise ClientReleaseError("release manifest filename does not match its version")

        source_root = manifest_path.parent
        archive_path = _source_file(source_root, parsed["archive_filename"])
        installer_path = _source_file(source_root, parsed["installer_filename"])
        _verify_regular_file(
            archive_path,
            expected_size=parsed["archive_size"],
            expected_sha256=parsed["archive_sha256"],
            maximum_size=_MAX_ARCHIVE_BYTES,
            label="archive",
        )
        _verify_regular_file(
            installer_path,
            expected_size=parsed["installer_size"],
            expected_sha256=parsed["installer_sha256"],
            maximum_size=_MAX_INSTALLER_BYTES,
            label="installer",
        )
        _verify_archive(archive_path, parsed)

        release_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        plugin_root = self.root / _PLUGIN_ID
        target = plugin_root / parsed["version"]
        if target.exists():
            existing = self.get_release(parsed["version"])
            if existing.release_manifest_sha256 != release_manifest_sha256:
                raise ClientReleaseError("release versions are immutable")
            return existing

        plugin_root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            shutil.copyfile(manifest_path, staging / expected_manifest_name)
            shutil.copyfile(archive_path, staging / parsed["archive_filename"])
            shutil.copyfile(installer_path, staging / parsed["installer_filename"])
            os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if target.exists():
                existing = self.get_release(parsed["version"])
                if existing.release_manifest_sha256 == release_manifest_sha256:
                    return existing
            raise
        return self.get_release(parsed["version"])

    def get_release(self, version: str) -> ClientPluginRelease:
        _parse_version(version)
        release_root = self.root / _PLUGIN_ID / version
        manifest_path = release_root / f"{_PLUGIN_ID}-{version}-release.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ClientReleaseNotFound(f"release {version} is not installed")
        manifest_bytes = _read_bounded(manifest_path, _MAX_MANIFEST_BYTES, "release manifest")
        payload = _load_strict_json(manifest_bytes)
        parsed = _parse_release_manifest(payload)
        if parsed["version"] != version:
            raise ClientReleaseError("stored release directory does not match manifest version")

        archive_path = _source_file(release_root, parsed["archive_filename"])
        installer_path = _source_file(release_root, parsed["installer_filename"])
        _verify_regular_file(
            archive_path,
            expected_size=parsed["archive_size"],
            expected_sha256=parsed["archive_sha256"],
            maximum_size=_MAX_ARCHIVE_BYTES,
            label="archive",
        )
        _verify_regular_file(
            installer_path,
            expected_size=parsed["installer_size"],
            expected_sha256=parsed["installer_sha256"],
            maximum_size=_MAX_INSTALLER_BYTES,
            label="installer",
        )
        _verify_archive(archive_path, parsed)
        return ClientPluginRelease(
            plugin_id=_PLUGIN_ID,
            version=version,
            min_app_version=parsed["min_app_version"],
            archive_path=archive_path,
            archive_sha256=parsed["archive_sha256"],
            archive_size_bytes=parsed["archive_size"],
            installer_path=installer_path,
            installer_sha256=parsed["installer_sha256"],
            installer_size_bytes=parsed["installer_size"],
            main_sha256=parsed["file_hashes"]["main.js"],
            release_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )

    def latest_release(self) -> ClientPluginRelease | None:
        plugin_root = self.root / _PLUGIN_ID
        if not plugin_root.is_dir():
            return None
        versions = [
            child.name
            for child in plugin_root.iterdir()
            if child.is_dir() and _VERSION_PATTERN.fullmatch(child.name)
        ]
        if not versions:
            return None
        latest = max(versions, key=_version_key)
        return self.get_release(latest)


def _parse_release_manifest(payload: Any) -> dict[str, Any]:
    root = _strict_object(payload, {"schema_version", "plugin", "archive", "installer", "files"})
    if root["schema_version"] != 1:
        raise ClientReleaseError("unsupported release manifest schema")

    plugin = _strict_object(
        root["plugin"], {"id", "version", "min_app_version", "desktop_only"}
    )
    if plugin["id"] != _PLUGIN_ID or plugin["desktop_only"] is not True:
        raise ClientReleaseError("release plugin identity or platform is invalid")
    version = _required_string(plugin["version"], "plugin.version")
    min_app_version = _required_string(plugin["min_app_version"], "plugin.min_app_version")
    _parse_version(version)
    _parse_version(min_app_version)

    archive = _strict_object(root["archive"], {"filename", "sha256", "size_bytes", "entries"})
    archive_filename = _required_filename(archive["filename"], "archive.filename")
    if archive_filename != f"{_PLUGIN_ID}-{version}-alpha.zip":
        raise ClientReleaseError("archive filename does not match release version")
    archive_sha256 = _required_sha256(archive["sha256"], "archive.sha256")
    archive_size = _required_size(archive["size_bytes"], _MAX_ARCHIVE_BYTES, "archive.size_bytes")
    if archive["entries"] != list(_EXPECTED_ENTRIES):
        raise ClientReleaseError("archive entries are not the fixed plugin whitelist")

    installer = _strict_object(root["installer"], {"filename", "sha256", "size_bytes"})
    installer_filename = _required_filename(installer["filename"], "installer.filename")
    if installer_filename != f"install-{_PLUGIN_ID}-{version}.zsh":
        raise ClientReleaseError("installer filename does not match release version")
    installer_sha256 = _required_sha256(installer["sha256"], "installer.sha256")
    installer_size = _required_size(
        installer["size_bytes"], _MAX_INSTALLER_BYTES, "installer.size_bytes"
    )

    files = _strict_object(root["files"], set(_RELEASE_FILES))
    file_hashes: dict[str, str] = {}
    file_sizes: dict[str, int] = {}
    for name in _RELEASE_FILES:
        item = _strict_object(files[name], {"sha256", "size_bytes"})
        file_hashes[name] = _required_sha256(item["sha256"], f"files.{name}.sha256")
        file_sizes[name] = _required_size(
            item["size_bytes"], _MAX_RELEASE_FILE_BYTES, f"files.{name}.size_bytes"
        )
    return {
        "version": version,
        "min_app_version": min_app_version,
        "archive_filename": archive_filename,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "installer_filename": installer_filename,
        "installer_sha256": installer_sha256,
        "installer_size": installer_size,
        "file_hashes": file_hashes,
        "file_sizes": file_sizes,
    }


def _verify_archive(path: Path, parsed: dict[str, Any]) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if [item.filename for item in infos] != list(_EXPECTED_ENTRIES):
                raise ClientReleaseError("archive contents are not the fixed plugin whitelist")
            for info, name in zip(infos, _RELEASE_FILES, strict=True):
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK or info.is_dir():
                    raise ClientReleaseError("archive entries must be regular files")
                if info.file_size != parsed["file_sizes"][name]:
                    raise ClientReleaseError(f"archive size mismatch for {name}")
                if info.file_size > _MAX_RELEASE_FILE_BYTES:
                    raise ClientReleaseError(f"archive entry too large for {name}")
                content = archive.read(info)
                if hashlib.sha256(content).hexdigest() != parsed["file_hashes"][name]:
                    raise ClientReleaseError(f"archive checksum mismatch for {name}")
            plugin_manifest = json.loads(archive.read(f"{_PLUGIN_ID}/manifest.json"))
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ClientReleaseError("archive is not a valid plugin package") from exc
    if not isinstance(plugin_manifest, dict):
        raise ClientReleaseError("plugin manifest must be an object")
    if (
        plugin_manifest.get("id") != _PLUGIN_ID
        or plugin_manifest.get("version") != parsed["version"]
        or plugin_manifest.get("minAppVersion") != parsed["min_app_version"]
        or plugin_manifest.get("isDesktopOnly") is not True
    ):
        raise ClientReleaseError("plugin manifest identity does not match release manifest")


def _strict_object(value: Any, expected_keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ClientReleaseError("release manifest contains missing or unknown fields")
    return value


def _load_strict_json(content: bytes) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientReleaseError("release manifest is not valid JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClientReleaseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClientReleaseError(f"{label} must be a non-empty string")
    return value


def _required_filename(value: Any, label: str) -> str:
    filename = _required_string(value, label)
    if Path(filename).name != filename or filename in {".", ".."}:
        raise ClientReleaseError(f"{label} must be a basename")
    return filename


def _required_sha256(value: Any, label: str) -> str:
    checksum = _required_string(value, label)
    if not _SHA256_PATTERN.fullmatch(checksum):
        raise ClientReleaseError(f"{label} must be lowercase SHA-256")
    return checksum


def _required_size(value: Any, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > maximum:
        raise ClientReleaseError(f"{label} is outside the permitted range")
    return value


def _parse_version(version: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ClientReleaseError("release version must be canonical semantic version")
    return tuple(int(value) for value in match.groups())


def _version_key(version: str) -> tuple[int, int, int]:
    return _parse_version(version)


def _source_file(root: Path, filename: str) -> Path:
    path = root / _required_filename(filename, "release filename")
    if path.is_symlink():
        raise ClientReleaseError("release files cannot be symbolic links")
    return path


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ClientReleaseError(f"{label} is unavailable") from exc
    if size <= 0 or size > maximum:
        raise ClientReleaseError(f"{label} size is outside the permitted range")
    return path.read_bytes()


def _verify_regular_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum_size: int,
    label: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise ClientReleaseError(f"{label} must be a regular file")
    content = _read_bounded(path, maximum_size, label)
    if len(content) != expected_size:
        raise ClientReleaseError(f"{label} size does not match release manifest")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ClientReleaseError(f"{label} checksum does not match release manifest")
