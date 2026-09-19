"""Unconnected, bounded local-Ollama transport for meeting field-repair shadows.

The adapter is intentionally not imported by the production structuring runtime.
It maps one already-profiled, minimal field-repair envelope to a fixed loopback
Ollama endpoint.  Connection construction is injectable so public synthetic tests
can exercise every I/O and cleanup path without opening a real network connection.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from speech_capture_worker.meeting_field_repair_shadow import (
    CancellationCheck,
    MeetingFieldRepairCallRequest,
    MeetingFieldRepairShadowError,
)
from speech_capture_worker.meeting_field_repair_transport_shadow import (
    build_meeting_field_repair_transport_envelope,
)

LOCAL_OLLAMA_HOST = "127.0.0.1"
LOCAL_OLLAMA_PORT = 11434
LOCAL_OLLAMA_GENERATE_PATH = "/api/generate"
LOCAL_OLLAMA_RESPONSE_LIMIT_BYTES = 256 * 1024
LOCAL_OLLAMA_CONTEXT_TOKENS = 8_192
LOCAL_OLLAMA_MONITOR_INTERVAL_SECONDS = 0.01
LOCAL_OLLAMA_MONITOR_JOIN_SECONDS = 1.0
LOCAL_OLLAMA_MONITOR_THREAD_NAME = "speech-capture-meeting-field-repair-local-cancel"

_SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_OLLAMA_RESPONSE_KEYS = frozenset(
    {
        "model",
        "created_at",
        "response",
        "done",
        "done_reason",
        "context",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    }
)
_OLLAMA_STRING_FIELDS = frozenset({"model", "created_at", "done_reason"})
_OLLAMA_COUNT_FIELDS = frozenset(
    {
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    }
)


class MeetingFieldRepairLocalTransportError(ValueError):
    """Raised when the isolated local transport fails closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LocalOllamaResponse(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class LocalOllamaConnection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> LocalOllamaResponse: ...

    def close(self) -> None: ...


LocalOllamaConnectionFactory = Callable[[str, int, float], LocalOllamaConnection]


def _default_connection_factory(
    host: str,
    port: int,
    timeout_seconds: float,
) -> LocalOllamaConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout_seconds)


class LocalOllamaMeetingFieldRepairTransport:
    """Map one bounded field request to a fixed, cancellable local Ollama call."""

    def __init__(
        self,
        *,
        editor_model: str,
        connection_factory: LocalOllamaConnectionFactory = _default_connection_factory,
        cancelled: CancellationCheck | None = None,
    ) -> None:
        if not isinstance(editor_model, str) or _SAFE_MODEL_NAME.fullmatch(editor_model) is None:
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_model_invalid",
                "The local field-repair editor model name is invalid.",
            )
        self._editor_model = editor_model
        self._connection_factory = connection_factory
        self._cancelled = cancelled

    def uses_cancellation_check(self, cancelled: CancellationCheck) -> bool:
        """Return whether transport abort and orchestration share one cancellation source."""

        return self._cancelled is cancelled

    def __call__(self, request: MeetingFieldRepairCallRequest) -> str:
        self._raise_if_cancelled()
        envelope = build_meeting_field_repair_transport_envelope(request)
        if envelope.model_role != "editor":
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_role_invalid",
                "The local field-repair transport only accepts the editor role.",
            )
        payload = _canonical_request_json(
            {
                "model": self._editor_model,
                "prompt": "/no_think\n" + envelope.rendered_prompt(),
                "stream": False,
                "think": False,
                "format": envelope.result_schema(),
                "options": {
                    "temperature": 0.2,
                    "num_ctx": LOCAL_OLLAMA_CONTEXT_TOKENS,
                    "num_predict": envelope.maximum_output_tokens,
                },
            }
        )
        self._raise_if_cancelled()

        connection: LocalOllamaConnection | None = None
        response: LocalOllamaResponse | None = None
        stop_monitor = threading.Event()
        monitor: threading.Thread | None = None
        monitor_failure: list[BaseException] = []
        try:
            connection = self._connection_factory(
                LOCAL_OLLAMA_HOST,
                LOCAL_OLLAMA_PORT,
                envelope.timeout_seconds,
            )
            monitor = self._start_cancellation_monitor(
                connection=connection,
                stop=stop_monitor,
                failure=monitor_failure,
            )
            self._raise_if_cancelled()
            connection.request(
                "POST",
                LOCAL_OLLAMA_GENERATE_PATH,
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            response = connection.getresponse()
            self._raise_if_cancelled()
            result = _read_ollama_response(response)
            self._raise_if_cancelled()
            if monitor_failure:
                raise MeetingFieldRepairLocalTransportError(
                    "field_repair_local_cancellation_check_failed",
                    "The local field-repair cancellation monitor failed closed.",
                ) from monitor_failure[0]
            return result
        except MeetingFieldRepairShadowError:
            raise
        except TimeoutError as error:
            raise TimeoutError("The local field-repair call reached its I/O timeout.") from error
        except MeetingFieldRepairLocalTransportError:
            raise
        except OSError as error:
            if self._is_cancelled():
                raise MeetingFieldRepairShadowError(
                    "field_repair_cancelled",
                    "The local meeting field-repair call was cancelled.",
                ) from error
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_io_failed",
                "The isolated local field-repair transport failed.",
            ) from error
        finally:
            stop_monitor.set()
            _close_quietly(response)
            _close_quietly(connection)
            if monitor is not None:
                monitor.join(timeout=LOCAL_OLLAMA_MONITOR_JOIN_SECONDS)
                if monitor.is_alive():
                    raise MeetingFieldRepairLocalTransportError(
                        "field_repair_local_cleanup_failed",
                        "The local field-repair cancellation monitor did not stop.",
                    )

    def _start_cancellation_monitor(
        self,
        *,
        connection: LocalOllamaConnection,
        stop: threading.Event,
        failure: list[BaseException],
    ) -> threading.Thread | None:
        if self._cancelled is None:
            return None

        def monitor_cancellation() -> None:
            while not stop.wait(LOCAL_OLLAMA_MONITOR_INTERVAL_SECONDS):
                try:
                    cancelled = self._cancelled()
                except BaseException as error:
                    failure.append(error)
                    _close_quietly(connection)
                    return
                if cancelled:
                    _close_quietly(connection)
                    return

        thread = threading.Thread(
            target=monitor_cancellation,
            name=LOCAL_OLLAMA_MONITOR_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _is_cancelled(self) -> bool:
        if self._cancelled is None:
            return False
        try:
            return bool(self._cancelled())
        except Exception as error:
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_cancellation_check_failed",
                "The local field-repair cancellation check failed closed.",
            ) from error

    def _raise_if_cancelled(self) -> None:
        if self._is_cancelled():
            raise MeetingFieldRepairShadowError(
                "field_repair_cancelled",
                "The local meeting field-repair call was cancelled.",
            )


def _canonical_request_json(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_request_invalid",
            "The local field-repair request is not canonical JSON.",
        ) from error


def _read_ollama_response(response: LocalOllamaResponse) -> str:
    if isinstance(response.status, bool) or response.status != 200:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_http_status",
            "The local field-repair engine returned a non-success status.",
        )
    content_type = response.getheader("Content-Type")
    if not isinstance(content_type, str) or not content_type.lower().startswith(
        "application/json"
    ):
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_content_type",
            "The local field-repair engine returned an unexpected content type.",
        )
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as error:
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_response_length_invalid",
                "The local field-repair engine returned an invalid response length.",
            ) from error
        if declared_length < 0 or declared_length > LOCAL_OLLAMA_RESPONSE_LIMIT_BYTES:
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_response_too_large",
                "The local field-repair response exceeds its byte budget.",
            )
    raw = response.read(LOCAL_OLLAMA_RESPONSE_LIMIT_BYTES + 1)
    if not isinstance(raw, bytes):
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_invalid",
            "The local field-repair engine returned a non-byte response.",
        )
    if len(raw) > LOCAL_OLLAMA_RESPONSE_LIMIT_BYTES:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_too_large",
            "The local field-repair response exceeds its byte budget.",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_invalid",
            "The local field-repair engine returned invalid JSON.",
        ) from error
    return _validate_ollama_response_payload(payload)


def _validate_ollama_response_payload(payload: Any) -> str:
    if not isinstance(payload, dict) or set(payload) - _OLLAMA_RESPONSE_KEYS:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_invalid",
            "The local field-repair engine returned an invalid response object.",
        )
    if payload.get("done") is not True:
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_incomplete",
            "The local field-repair engine did not complete the response.",
        )
    value = payload.get("response")
    if not isinstance(value, str) or not value.strip():
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_empty",
            "The local field-repair engine returned an empty response.",
        )
    for name in _OLLAMA_STRING_FIELDS:
        if name in payload and not isinstance(payload[name], str):
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_response_invalid",
                "The local field-repair engine returned invalid metadata.",
            )
    for name in _OLLAMA_COUNT_FIELDS:
        count = payload.get(name)
        if name in payload and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            raise MeetingFieldRepairLocalTransportError(
                "field_repair_local_response_invalid",
                "The local field-repair engine returned invalid metrics.",
            )
    if "context" in payload and (
        not isinstance(payload["context"], list)
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in payload["context"]
        )
    ):
        raise MeetingFieldRepairLocalTransportError(
            "field_repair_local_response_invalid",
            "The local field-repair engine returned invalid context metadata.",
        )
    return value.strip()


def _close_quietly(resource: Any) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        return
