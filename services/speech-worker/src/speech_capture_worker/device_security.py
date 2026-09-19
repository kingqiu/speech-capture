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
    CredentialRotationConflict,
    CredentialRotationExpired,
    CredentialRotationInvalid,
    CredentialRotationNotFound,
    DeviceAlreadyPaired,
    DeviceNotFound,
    InvalidJobRequest,
    PairingCodeInvalid,
    PairingSessionExpired,
    PairingSessionNotFound,
)

SECURITY_SCHEMA_VERSION = 2
DEFAULT_PAIRING_TTL_SECONDS = 300
MAX_PAIRING_TTL_SECONDS = 900
MAX_PAIRING_ATTEMPTS = 5
PAIRING_TICKET_PREFIX = "scpair1"
DEFAULT_ROTATION_TTL_SECONDS = 600
MAX_ROTATION_TTL_SECONDS = 3600


@dataclass(frozen=True)
class PairingSessionSecret:
    session_id: str
    pairing_code: str
    pairing_ticket: str
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


@dataclass(frozen=True)
class PreparedCredentialRotation:
    rotation_id: str
    device_id: str
    bearer_token: str
    generation: int
    expires_at: str


@dataclass(frozen=True)
class ActivatedCredentialRotation:
    rotation_id: str
    device_id: str
    credential_id: str
    generation: int
    activated_at: str


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
            pairing_ticket=create_pairing_ticket(session_id, pairing_code),
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

    def confirm_pairing_ticket(self, pairing_ticket: str) -> IssuedDeviceCredential:
        session_id, pairing_code = parse_pairing_ticket(pairing_ticket)
        return self.confirm_pairing(
            session_id=session_id,
            pairing_code=pairing_code,
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
                """
                SELECT credentials.*
                FROM device_credentials AS credentials
                INNER JOIN (
                    SELECT device_id, MAX(generation) AS generation
                    FROM device_credentials
                    GROUP BY device_id
                ) AS latest
                ON latest.device_id = credentials.device_id
                AND latest.generation = credentials.generation
                ORDER BY credentials.created_at, credentials.credential_id
                """
            ).fetchall()
            return [_row_to_device(row) for row in rows]

    def quick_check(self) -> bool:
        with self._lock:
            row = self._connection.execute("PRAGMA quick_check").fetchone()
            return row is not None and str(row[0]).lower() == "ok"

    def get_device(self, device_id: str) -> PairedDevice:
        _validate_device_id(device_id)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM device_credentials
                WHERE device_id = ?
                ORDER BY generation DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if row is None:
                raise DeviceNotFound("The paired device does not exist.")
            return _row_to_device(row)

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

    def prepare_credential_rotation(
        self,
        device_id: str,
        *,
        ttl_seconds: int = DEFAULT_ROTATION_TTL_SECONDS,
    ) -> PreparedCredentialRotation:
        _validate_device_id(device_id)
        if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= MAX_ROTATION_TTL_SECONDS:
            raise InvalidJobRequest("Rotation TTL must be between 60 and 3600 seconds.")
        rotation_id = f"rot_{uuid4().hex}"
        replacement_token = f"scw_{secrets.token_urlsafe(32)}"
        replacement_credential_id = f"cred_{uuid4().hex}"
        now = _utc_now()
        expires_at = (datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)).isoformat()
        generation: int | None = None
        with self._transaction():
            active = self._connection.execute(
                "SELECT * FROM device_credentials "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            if active is None:
                raise DeviceNotFound("The paired device does not exist.")
            generation = int(active["generation"]) + 1
            self._connection.execute(
                "DELETE FROM credential_rotations WHERE device_id = ?",
                (device_id,),
            )
            self._connection.execute(
                """
                INSERT INTO credential_rotations (
                    rotation_id, device_id, current_credential_id,
                    replacement_credential_id, replacement_token_sha256,
                    allowed_vault_ids_json, generation, expires_at, created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    rotation_id,
                    device_id,
                    str(active["credential_id"]),
                    replacement_credential_id,
                    _sha256(replacement_token),
                    str(active["allowed_vault_ids_json"]),
                    generation,
                    expires_at,
                    now,
                ),
            )
        assert generation is not None
        return PreparedCredentialRotation(
            rotation_id=rotation_id,
            device_id=device_id,
            bearer_token=replacement_token,
            generation=generation,
            expires_at=expires_at,
        )

    def activate_credential_rotation(
        self,
        *,
        device_id: str,
        replacement_token: str,
    ) -> ActivatedCredentialRotation:
        _validate_device_id(device_id)
        if (
            not isinstance(replacement_token, str)
            or not replacement_token.startswith("scw_")
            or len(replacement_token) > 512
        ):
            raise CredentialRotationInvalid("The replacement credential was not accepted.")
        digest = _sha256(replacement_token)
        activated: tuple[str, str, str, int, str] | None = None
        with self._transaction():
            rotation = self._connection.execute(
                "SELECT * FROM credential_rotations WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if rotation is None:
                raise CredentialRotationNotFound("The credential rotation does not exist.")
            if not secrets.compare_digest(
                digest,
                str(rotation["replacement_token_sha256"]),
            ):
                raise CredentialRotationInvalid("The replacement credential was not accepted.")
            activated_at = rotation["activated_at"]
            if activated_at is not None:
                replacement = self._connection.execute(
                    "SELECT 1 FROM device_credentials "
                    "WHERE credential_id = ? AND token_sha256 = ?",
                    (str(rotation["replacement_credential_id"]), digest),
                ).fetchone()
                if replacement is None:
                    raise CredentialRotationConflict(
                        "The credential rotation is no longer consistent."
                    )
                activated = (
                    str(rotation["rotation_id"]),
                    device_id,
                    str(rotation["replacement_credential_id"]),
                    int(rotation["generation"]),
                    str(activated_at),
                )
            else:
                now = _utc_now()
                if str(rotation["expires_at"]) <= now:
                    raise CredentialRotationExpired("The credential rotation has expired.")
                current = self._connection.execute(
                    "SELECT * FROM device_credentials "
                    "WHERE credential_id = ? AND device_id = ? AND revoked_at IS NULL",
                    (str(rotation["current_credential_id"]), device_id),
                ).fetchone()
                if current is None:
                    raise CredentialRotationConflict(
                        "The active device credential changed before rotation activation."
                    )
                self._connection.execute(
                    "UPDATE device_credentials SET revoked_at = ? WHERE credential_id = ?",
                    (now, str(current["credential_id"])),
                )
                self._connection.execute(
                    """
                    INSERT INTO device_credentials (
                        credential_id, device_id, token_sha256, allowed_vault_ids_json,
                        generation, created_at, last_used_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        str(rotation["replacement_credential_id"]),
                        device_id,
                        digest,
                        str(rotation["allowed_vault_ids_json"]),
                        int(rotation["generation"]),
                        now,
                    ),
                )
                self._connection.execute(
                    "UPDATE credential_rotations SET activated_at = ? WHERE rotation_id = ?",
                    (now, str(rotation["rotation_id"])),
                )
                activated = (
                    str(rotation["rotation_id"]),
                    device_id,
                    str(rotation["replacement_credential_id"]),
                    int(rotation["generation"]),
                    now,
                )
        assert activated is not None
        return ActivatedCredentialRotation(*activated)

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
                CREATE TABLE credential_rotations (
                    rotation_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL UNIQUE,
                    current_credential_id TEXT NOT NULL,
                    replacement_credential_id TEXT NOT NULL UNIQUE,
                    replacement_token_sha256 TEXT NOT NULL UNIQUE,
                    allowed_vault_ids_json TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 1),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT
                );
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
        elif current == 1:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE credential_rotations (
                    rotation_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL UNIQUE,
                    current_credential_id TEXT NOT NULL,
                    replacement_credential_id TEXT NOT NULL UNIQUE,
                    replacement_token_sha256 TEXT NOT NULL UNIQUE,
                    allowed_vault_ids_json TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 1),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT
                );
                PRAGMA user_version = 2;
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


def create_pairing_ticket(session_id: str, pairing_code: str) -> str:
    if (
        not session_id.startswith("pair_")
        or len(session_id) != 37
        or any(character not in "0123456789abcdef" for character in session_id[5:])
        or not pairing_code
        or len(pairing_code) > 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in pairing_code
        )
    ):
        raise PairingCodeInvalid("The pairing code was not accepted.")
    return f"{PAIRING_TICKET_PREFIX}.{session_id[5:]}.{pairing_code}"


def parse_pairing_ticket(pairing_ticket: str) -> tuple[str, str]:
    if not isinstance(pairing_ticket, str) or len(pairing_ticket) > 192:
        raise PairingCodeInvalid("The pairing code was not accepted.")
    parts = pairing_ticket.strip().split(".")
    if len(parts) != 3 or parts[0] != PAIRING_TICKET_PREFIX:
        raise PairingCodeInvalid("The pairing code was not accepted.")
    session_id = f"pair_{parts[1]}"
    pairing_code = parts[2]
    create_pairing_ticket(session_id, pairing_code)
    return session_id, pairing_code


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
