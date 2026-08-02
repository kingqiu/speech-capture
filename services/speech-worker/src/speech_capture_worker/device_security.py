"""Durable pairing sessions and digest-only per-device credentials."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from speech_capture_worker.api_auth import ApiPrincipal
from speech_capture_worker.domain import SAFE_IDENTIFIER_PATTERN
from speech_capture_worker.errors import (
    DeviceAlreadyPaired,
    DeviceNotFound,
    InvalidJobRequest,
    PairingCodeInvalid,
    PairingSessionExpired,
    PairingSessionNotFound,
)

SECURITY_SCHEMA_VERSION = 1
DEFAULT_PAIRING_TTL_SECONDS = 300
MAX_PAIRING_TTL_SECONDS = 900
MAX_PAIRING_ATTEMPTS = 5


@dataclass(frozen=True)
class PairingSessionSecret:
    session_id: str
    pairing_code: str
    device_id: str
    allowed_vault_ids: tuple[str, ...]
    expires_at: str


@dataclass(frozen=True)
class IssuedDeviceCredential:
    credential_id: str
    device_id: str
    bearer_token: str
    allowed_vault_ids: tuple[str, ...]
    generation: int
    created_at: str


@dataclass(frozen=True)
class PairedDevice:
    credential_id: str
    device_id: str
    allowed_vault_ids: tuple[str, ...]
    generation: int
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class DeviceSecurityStore:
    """Private SQLite store that never persists plaintext bearer credentials."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = DELETE")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._migrate()
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def __enter__(self) -> DeviceSecurityStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_pairing_session(
        self,
        *,
        device_id: str,
        allowed_vault_ids: tuple[str, ...],
        ttl_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
    ) -> PairingSessionSecret:
        vaults = _validate_identity_scope(device_id, allowed_vault_ids)
        if not isinstance(ttl_seconds, int) or not 30 <= ttl_seconds <= MAX_PAIRING_TTL_SECONDS:
            raise InvalidJobRequest("Pairing TTL must be between 30 and 900 seconds.")
        session_id = f"pair_{uuid4().hex}"
        pairing_code = secrets.token_urlsafe(12)
        now = _utc_now()
        expires_at = (datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._transaction():
            active = self._connection.execute(
                "SELECT 1 FROM device_credentials WHERE device_id = ? AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            if active is not None:
                raise DeviceAlreadyPaired("The device already has an active credential.")
            self._connection.execute(
                """
                INSERT INTO pairing_sessions (
                    session_id, code_sha256, device_id, allowed_vault_ids_json,
                    expires_at, failed_attempts, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    session_id,
                    _sha256(pairing_code),
                    device_id,
                    _vaults_json(vaults),
                    expires_at,
                    now,
                ),
            )
        return PairingSessionSecret(
            session_id=session_id,
            pairing_code=pairing_code,
            device_id=device_id,
            allowed_vault_ids=vaults,
            expires_at=expires_at,
        )

    def confirm_pairing(self, *, session_id: str, pairing_code: str) -> IssuedDeviceCredential:
        if not session_id.startswith("pair_") or len(session_id) != 37:
            raise PairingSessionNotFound("The pairing session does not exist.")
        if not isinstance(pairing_code, str) or not pairing_code:
            raise PairingCodeInvalid("The pairing code was not accepted.")
        invalid_code = False
        issued: tuple[str, str, str, tuple[str, ...], int, str] | None = None
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM pairing_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise PairingSessionNotFound("The pairing session does not exist.")
            now = _utc_now()
            if row["consumed_at"] is not None or str(row["expires_at"]) <= now:
                raise PairingSessionExpired("The pairing session is no longer active.")
            failed_attempts = int(row["failed_attempts"])
            if failed_attempts >= MAX_PAIRING_ATTEMPTS:
                raise PairingSessionExpired("The pairing session is no longer active.")
            if not secrets.compare_digest(_sha256(pairing_code), str(row["code_sha256"])):
                self._connection.execute(
                    "UPDATE pairing_sessions SET failed_attempts = ? WHERE session_id = ?",
                    (failed_attempts + 1, session_id),
                )
                invalid_code = True
            else:
                device_id = str(row["device_id"])
                active = self._connection.execute(
                    "SELECT 1 FROM device_credentials "
                    "WHERE device_id = ? AND revoked_at IS NULL",
                    (device_id,),
                ).fetchone()
                if active is not None:
                    raise DeviceAlreadyPaired("The device already has an active credential.")
                generation = int(
                    self._connection.execute(
                        "SELECT COALESCE(MAX(generation), 0) "
                        "FROM device_credentials WHERE device_id = ?",
                        (device_id,),
                    ).fetchone()[0]
                ) + 1
                token = f"scw_{secrets.token_urlsafe(32)}"
                credential_id = f"cred_{uuid4().hex}"
                vaults = tuple(json.loads(str(row["allowed_vault_ids_json"])))
                self._connection.execute(
                    """
                    INSERT INTO device_credentials (
                        credential_id, device_id, token_sha256, allowed_vault_ids_json,
                        generation, created_at, last_used_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        credential_id,
                        device_id,
                        _sha256(token),
                        _vaults_json(vaults),
                        generation,
                        now,
                    ),
                )
                self._connection.execute(
                    "UPDATE pairing_sessions SET consumed_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
                issued = (credential_id, device_id, token, vaults, generation, now)
        if invalid_code:
            raise PairingCodeInvalid("The pairing code was not accepted.")
        assert issued is not None
        credential_id, device_id, token, vaults, generation, now = issued
        return IssuedDeviceCredential(
            credential_id=credential_id,
            device_id=device_id,
            bearer_token=token,
            allowed_vault_ids=vaults,
            generation=generation,
            created_at=now,
        )

    def authenticate(self, token: str) -> ApiPrincipal | None:
        if not isinstance(token, str) or not token.startswith("scw_") or len(token) > 512:
            return None
        digest = _sha256(token)
        with self._transaction():
            rows = self._connection.execute(
                "SELECT * FROM device_credentials WHERE revoked_at IS NULL"
            ).fetchall()
            matched = None
            for row in rows:
                if secrets.compare_digest(digest, str(row["token_sha256"])):
                    matched = row
            if matched is None:
                return None
            self._connection.execute(
                "UPDATE device_credentials SET last_used_at = ? WHERE credential_id = ?",
                (_utc_now(), str(matched["credential_id"])),
            )
            return ApiPrincipal(
                device_id=str(matched["device_id"]),
                allowed_vault_ids=frozenset(
                    json.loads(str(matched["allowed_vault_ids_json"]))
                ),
            )

    def list_devices(self) -> list[PairedDevice]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM device_credentials ORDER BY created_at, credential_id"
            ).fetchall()
            return [_row_to_device(row) for row in rows]

    def revoke_device(self, device_id: str) -> bool:
        _validate_device_id(device_id)
        with self._transaction():
            cursor = self._connection.execute(
                "UPDATE device_credentials SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (_utc_now(), device_id),
            )
            if cursor.rowcount == 0:
                exists = self._connection.execute(
                    "SELECT 1 FROM device_credentials WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if exists is None:
                    raise DeviceNotFound("The paired device does not exist.")
                return False
            return True

    def _migrate(self) -> None:
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SECURITY_SCHEMA_VERSION:
            raise RuntimeError("The device security database is newer than this Worker.")
        if current == 0:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE pairing_sessions (
                    session_id TEXT PRIMARY KEY,
                    code_sha256 TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    allowed_vault_ids_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    failed_attempts INTEGER NOT NULL CHECK (failed_attempts >= 0),
                    consumed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE device_credentials (
                    credential_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    token_sha256 TEXT NOT NULL UNIQUE,
                    allowed_vault_ids_json TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT,
                    UNIQUE (device_id, generation)
                );
                CREATE UNIQUE INDEX device_credentials_active_device_idx
                ON device_credentials (device_id) WHERE revoked_at IS NULL;
                PRAGMA user_version = 1;
                COMMIT;
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")


def _validate_identity_scope(device_id: str, vault_ids: tuple[str, ...]) -> tuple[str, ...]:
    _validate_device_id(device_id)
    if not vault_ids or len(vault_ids) > 64:
        raise InvalidJobRequest("A pairing session requires 1 to 64 Vault identities.")
    normalized = tuple(dict.fromkeys(vault_ids))
    if len(normalized) != len(vault_ids) or any(
        not SAFE_IDENTIFIER_PATTERN.fullmatch(vault_id) for vault_id in normalized
    ):
        raise InvalidJobRequest("Pairing Vault identities must be unique safe identifiers.")
    return tuple(sorted(normalized))


def _validate_device_id(device_id: str) -> None:
    if not isinstance(device_id, str) or not SAFE_IDENTIFIER_PATTERN.fullmatch(device_id):
        raise InvalidJobRequest("device_id must be a safe identifier.")


def _row_to_device(row: sqlite3.Row) -> PairedDevice:
    return PairedDevice(
        credential_id=str(row["credential_id"]),
        device_id=str(row["device_id"]),
        allowed_vault_ids=tuple(json.loads(str(row["allowed_vault_ids_json"]))),
        generation=int(row["generation"]),
        created_at=str(row["created_at"]),
        last_used_at=str(row["last_used_at"]) if row["last_used_at"] is not None else None,
        revoked_at=str(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )


def _vaults_json(vaults: tuple[str, ...]) -> str:
    return json.dumps(vaults, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
