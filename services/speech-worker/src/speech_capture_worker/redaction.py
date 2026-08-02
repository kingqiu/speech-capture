"""Stable public error text and content-free diagnostic helpers."""

from __future__ import annotations

import re

_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[^/\s]+/)+[^\s]*")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)+[^\s]*")
_BEARER_SECRET = re.compile(r"\b(?:scw|Bearer)[_\s][A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)

PUBLIC_ERROR_MESSAGES = {
    "INVALID_JOB_REQUEST": "The request could not be accepted.",
    "JOB_NOT_FOUND": "The requested job was not found.",
    "UPLOAD_NOT_FOUND": "The requested upload was not found.",
    "ARTIFACT_NOT_FOUND": "The requested artifact was not found.",
    "RESOURCE_PREFLIGHT_BLOCKED": "Worker resources must recover before processing can continue.",
    "MEDIA_PROBE_UNAVAILABLE": "The Worker media runtime is unavailable.",
    "SOURCE_UNDECODABLE": "The uploaded source could not be decoded as supported audio.",
    "SOURCE_UPLOAD_NOT_VERIFIED": "The source upload is not verified.",
    "UPLOAD_CHECKSUM_MISMATCH": "The uploaded source failed its integrity check.",
    "UPLOAD_PART_CHECKSUM_MISMATCH": "The upload part failed its integrity check.",
    "UPLOAD_INCOMPLETE": "The upload is incomplete.",
    "UPLOAD_STORAGE_ERROR": "The Worker could not safely store the upload.",
    "WORKER_PROCESSING_BUSY": "The Worker is already processing another job.",
    "ARTIFACT_VERIFICATION_FAILED": "The artifact package failed its integrity check.",
    "PAIRING_SESSION_NOT_FOUND": "The pairing session was not found.",
    "PAIRING_SESSION_EXPIRED": "The pairing session is no longer active.",
    "PAIRING_CODE_INVALID": "The pairing code was not accepted.",
    "DEVICE_ALREADY_PAIRED": "The device already has an active credential.",
    "DEVICE_NOT_FOUND": "The paired device was not found.",
    "CREDENTIAL_ROTATION_NOT_FOUND": "The credential rotation was not found.",
    "CREDENTIAL_ROTATION_EXPIRED": "The credential rotation has expired.",
    "CREDENTIAL_ROTATION_INVALID": "The replacement credential was not accepted.",
    "CREDENTIAL_ROTATION_CONFLICT": "The credential rotation could not be completed safely.",
}


def public_error_message(code: str | None) -> str | None:
    """Return code-derived text without copying exception or persisted user content."""

    if code is None:
        return None
    known = PUBLIC_ERROR_MESSAGES.get(code)
    if known is not None:
        return known
    if code.endswith("_NOT_FOUND"):
        return "The requested resource was not found."
    if code.endswith(("_CONFLICT", "_REVISION_CONFLICT")):
        return "The request conflicted with the current Worker state."
    if code.endswith(("_FAILED", "_INVALID")):
        return "The Worker could not complete this processing stage safely."
    return "The Worker could not complete the request."


def public_error_payload(code: str) -> dict[str, str]:
    return {"code": code, "message": public_error_message(code) or "Worker request failed."}


def public_cli_error_payload(code: str, message: str) -> dict[str, str]:
    """Preserve actionable local CLI text while removing common secret/path forms."""

    single_line = message.replace("\r", " ").replace("\n", " ")[:500]
    redacted = _BEARER_SECRET.sub("[redacted-credential]", single_line)
    redacted = _WINDOWS_PATH.sub("[redacted-path]", redacted)
    redacted = _UNIX_PATH.sub("[redacted-path]", redacted)
    return {
        "code": code,
        "message": redacted or public_error_message(code) or "Worker request failed.",
    }
