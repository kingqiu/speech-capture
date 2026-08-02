"""Versioned FastAPI surface over the durable Worker core."""

from dataclasses import asdict
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from speech_capture_worker import __version__
from speech_capture_worker.api_auth import ApiPrincipal, CredentialAuthenticator
from speech_capture_worker.api_schemas import (
    ApiErrorResponse,
    ApiErrorSchema,
    ArtifactListResponse,
    ArtifactName,
    ArtifactSchema,
    CapabilitiesResponse,
    CompatibilityRequestSchema,
    CompatibilityResponse,
    HealthResponse,
    IssuedDeviceCredentialSchema,
    JobActionEnvelope,
    JobActionRequestSchema,
    JobCreateSchema,
    JobEnvelope,
    JobListResponse,
    JobProgressSchema,
    JobSchema,
    JobSnapshotResponse,
    JobUpdateSchema,
    JobUpdatesResponse,
    PairingConfirmRequestSchema,
    ProvisionalTranscriptSchema,
    SafeIdentifier,
    Sha256String,
    TranscriptSegmentSchema,
    UploadCreateSchema,
    UploadEnvelope,
    UploadPartEnvelope,
    UploadPartSchema,
    UploadSchema,
)
from speech_capture_worker.artifact_access import load_artifact_package
from speech_capture_worker.device_security import DeviceSecurityStore
from speech_capture_worker.domain import (
    JobRecord,
    JobState,
    UploadCreateRequest,
    UploadRecord,
)
from speech_capture_worker.errors import (
    ArtifactNotFound,
    ArtifactVerificationFailed,
    IdempotencyConflict,
    InvalidJobRequest,
    InvalidTransition,
    JobNotFound,
    RevisionConflict,
    SchedulerBusy,
    UploadIncomplete,
    UploadNotFound,
    UploadPartChecksumMismatch,
    UploadPartConflict,
    UploadStateConflict,
    WorkerCoreError,
)
from speech_capture_worker.job_store import MAX_UPLOAD_CHUNK_SIZE_BYTES, JobStore
from speech_capture_worker.protocol_contract import (
    PROTOCOL_VERSION,
    CompatibilityRequest,
    VersionRange,
    get_capabilities,
    negotiate_compatibility,
)
from speech_capture_worker.recording_context import (
    RECORDING_CONTEXT_OPTION,
    recording_context_from_options,
)
from speech_capture_worker.transcript import JobSnapshot, TranscriptSegment

PRIVATE_ERROR_RESPONSES = {
    400: {"model": ApiErrorResponse},
    401: {"model": ApiErrorResponse},
    403: {"model": ApiErrorResponse},
    404: {"model": ApiErrorResponse},
    409: {"model": ApiErrorResponse},
    413: {"model": ApiErrorResponse},
    415: {"model": ApiErrorResponse},
    422: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


class ApiProblem(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def create_app(
    *,
    store: JobStore | None = None,
    credential_verifier: CredentialAuthenticator | None = None,
    device_security_store: DeviceSecurityStore | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Speech Capture Worker API",
        summary="Versioned local and private-network speech processing API.",
        version=PROTOCOL_VERSION,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="The request did not match the versioned API contract.",
        )

    @app.exception_handler(WorkerCoreError)
    async def handle_worker_error(request: Request, exc: WorkerCoreError) -> JSONResponse:
        return _error_response(
            request,
            status_code=_worker_error_status(exc),
            code=exc.code,
            message=exc.message,
        )

    def require_principal(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> ApiPrincipal:
        if credential_verifier is None:
            raise ApiProblem(
                503,
                "AUTHENTICATION_NOT_CONFIGURED",
                "Private Worker API access is not configured.",
            )
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiProblem(
                401,
                "AUTHENTICATION_REQUIRED",
                "A valid bearer credential is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = credential_verifier.authenticate(credentials.credentials)
        if principal is None:
            raise ApiProblem(
                401,
                "AUTHENTICATION_FAILED",
                "The bearer credential was not accepted.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    def require_store() -> JobStore:
        if store is None:
            raise ApiProblem(
                503,
                "WORKER_STORE_NOT_CONFIGURED",
                "The durable Worker store is not configured.",
            )
        return store

    Principal = Annotated[ApiPrincipal, Depends(require_principal)]
    Store = Annotated[JobStore, Depends(require_store)]

    @app.get(
        "/v1/health",
        response_model=HealthResponse,
        operation_id="getHealth",
        tags=["discovery"],
    )
    def get_health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            worker_version=__version__,
            protocol_version=PROTOCOL_VERSION,
        )

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        operation_id="getCapabilities",
        tags=["discovery"],
    )
    def read_capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse.model_validate(get_capabilities().to_dict())

    @app.post(
        "/v1/capabilities/negotiate",
        response_model=CompatibilityResponse,
        operation_id="negotiateCapabilities",
        tags=["discovery"],
        responses={422: {"model": ApiErrorResponse}},
    )
    def negotiate(request: CompatibilityRequestSchema) -> CompatibilityResponse:
        result = negotiate_compatibility(
            CompatibilityRequest(
                protocol=VersionRange(
                    request.protocol.minimum,
                    request.protocol.maximum,
                ),
                artifact_schema=VersionRange(
                    request.artifact_schema.minimum,
                    request.artifact_schema.maximum,
                ),
                required_features=request.required_features,
            )
        )
        return CompatibilityResponse.model_validate(result.to_dict())

    @app.post(
        "/v1/pairing/confirm",
        response_model=IssuedDeviceCredentialSchema,
        operation_id="confirmPairing",
        tags=["pairing"],
        responses={
            401: {"model": ApiErrorResponse},
            404: {"model": ApiErrorResponse},
            409: {"model": ApiErrorResponse},
            410: {"model": ApiErrorResponse},
            422: {"model": ApiErrorResponse},
            503: {"model": ApiErrorResponse},
        },
    )
    def confirm_pairing(
        request: PairingConfirmRequestSchema,
    ) -> IssuedDeviceCredentialSchema:
        if device_security_store is None:
            raise ApiProblem(
                503,
                "PAIRING_NOT_CONFIGURED",
                "Device pairing is not configured.",
            )
        issued = device_security_store.confirm_pairing(
            session_id=request.session_id,
            pairing_code=request.pairing_code,
        )
        return IssuedDeviceCredentialSchema.model_validate(asdict(issued))

    @app.post(
        "/v1/uploads",
        response_model=UploadEnvelope,
        operation_id="createUpload",
        tags=["uploads"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def create_upload(
        request: UploadCreateSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> UploadEnvelope:
        _authorize_supplied_vault(principal, request.vault_id)
        upload, created = worker_store.create_upload(
            UploadCreateRequest(**request.model_dump()),
            idempotency_key=idempotency_key,
        )
        return _upload_envelope(worker_store, upload, created=created)

    @app.get(
        "/v1/uploads/{upload_id}",
        response_model=UploadEnvelope,
        operation_id="getUpload",
        tags=["uploads"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def get_upload(upload_id: str, principal: Principal, worker_store: Store) -> UploadEnvelope:
        upload = worker_store.get_upload(upload_id)
        _authorize_existing_vault(principal, upload.vault_id)
        return _upload_envelope(worker_store, upload)

    @app.put(
        "/v1/uploads/{upload_id}/parts/{part_number}",
        response_model=UploadPartEnvelope,
        operation_id="putUploadPart",
        tags=["uploads"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    async def put_upload_part(
        upload_id: str,
        part_number: int,
        request: Request,
        principal: Principal,
        worker_store: Store,
        part_sha256: Annotated[Sha256String, Header(alias="X-Part-SHA256")],
    ) -> UploadPartEnvelope:
        upload = worker_store.get_upload(upload_id)
        _authorize_existing_vault(principal, upload.vault_id)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/octet-stream":
            raise ApiProblem(
                415,
                "UNSUPPORTED_UPLOAD_PART_MEDIA_TYPE",
                "Upload parts require application/octet-stream.",
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise ApiProblem(
                    400,
                    "INVALID_CONTENT_LENGTH",
                    "Content-Length must be a non-negative integer.",
                ) from exc
            if declared_length < 0 or declared_length > MAX_UPLOAD_CHUNK_SIZE_BYTES:
                raise ApiProblem(
                    413,
                    "UPLOAD_PART_TOO_LARGE",
                    "The upload part exceeds the Worker limit.",
                )
        content_buffer = bytearray()
        async for chunk in request.stream():
            if len(content_buffer) + len(chunk) > MAX_UPLOAD_CHUNK_SIZE_BYTES:
                raise ApiProblem(
                    413,
                    "UPLOAD_PART_TOO_LARGE",
                    "The upload part exceeds the Worker limit.",
                )
            content_buffer.extend(chunk)
        content = bytes(content_buffer)
        part, created = worker_store.put_upload_part(
            upload_id,
            part_number=part_number,
            content=content,
            part_sha256=part_sha256,
        )
        return UploadPartEnvelope(
            part=UploadPartSchema.model_validate(part.to_dict()),
            created=created,
        )

    @app.post(
        "/v1/uploads/{upload_id}/complete",
        response_model=UploadEnvelope,
        operation_id="completeUpload",
        tags=["uploads"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def complete_upload(
        upload_id: str,
        principal: Principal,
        worker_store: Store,
    ) -> UploadEnvelope:
        upload = worker_store.get_upload(upload_id)
        _authorize_existing_vault(principal, upload.vault_id)
        completed, changed = worker_store.complete_upload(upload_id)
        return _upload_envelope(worker_store, completed, created=changed)

    @app.post(
        "/v1/jobs",
        response_model=JobEnvelope,
        operation_id="createJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def create_job(
        request: JobCreateSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JobEnvelope:
        upload = worker_store.get_upload(request.upload_id)
        _authorize_existing_vault(principal, upload.vault_id)
        options = (
            {RECORDING_CONTEXT_OPTION: request.recording_context}
            if request.recording_context is not None
            else {}
        )
        job, created = worker_store.create_job_from_upload(
            request.upload_id,
            idempotency_key=idempotency_key,
            model_profile=request.model_profile,
            language_hint=request.language_hint,
            content_type_override=request.content_type_override,
            options=options,
        )
        return JobEnvelope(job=_job_schema(job), created=created)

    @app.get(
        "/v1/jobs",
        response_model=JobListResponse,
        operation_id="listJobs",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def list_jobs(
        principal: Principal,
        worker_store: Store,
        vault_id: Annotated[SafeIdentifier, Query()],
        states: Annotated[list[JobState] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> JobListResponse:
        _authorize_supplied_vault(principal, vault_id)
        jobs = worker_store.list_jobs(vault_id=vault_id, states=states, limit=limit)
        return JobListResponse(jobs=tuple(_job_schema(job) for job in jobs))

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobEnvelope,
        operation_id="getJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def get_job(job_id: str, principal: Principal, worker_store: Store) -> JobEnvelope:
        job = _authorized_job(worker_store, principal, job_id)
        return JobEnvelope(job=_job_schema(job))

    def perform_job_action(
        *,
        job_id: str,
        action: str,
        request: JobActionRequestSchema,
        principal: ApiPrincipal,
        worker_store: JobStore,
        idempotency_key: str,
    ) -> JobActionEnvelope:
        _authorized_job(worker_store, principal, job_id)
        job, applied = worker_store.apply_job_action(
            job_id,
            action=action,
            expected_revision=request.expected_revision,
            idempotency_key=idempotency_key,
        )
        return JobActionEnvelope(job=_job_schema(job), applied=applied)

    @app.post(
        "/v1/jobs/{job_id}/pause",
        response_model=JobActionEnvelope,
        operation_id="pauseJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def pause_job(
        job_id: str,
        request: JobActionRequestSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JobActionEnvelope:
        return perform_job_action(
            job_id=job_id,
            action="pause",
            request=request,
            principal=principal,
            worker_store=worker_store,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/jobs/{job_id}/resume",
        response_model=JobActionEnvelope,
        operation_id="resumeJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def resume_job(
        job_id: str,
        request: JobActionRequestSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JobActionEnvelope:
        return perform_job_action(
            job_id=job_id,
            action="resume",
            request=request,
            principal=principal,
            worker_store=worker_store,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/jobs/{job_id}/cancel",
        response_model=JobActionEnvelope,
        operation_id="cancelJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def cancel_job(
        job_id: str,
        request: JobActionRequestSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JobActionEnvelope:
        return perform_job_action(
            job_id=job_id,
            action="cancel",
            request=request,
            principal=principal,
            worker_store=worker_store,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/jobs/{job_id}/retry",
        response_model=JobActionEnvelope,
        operation_id="retryJob",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def retry_job(
        job_id: str,
        request: JobActionRequestSchema,
        principal: Principal,
        worker_store: Store,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JobActionEnvelope:
        return perform_job_action(
            job_id=job_id,
            action="retry",
            request=request,
            principal=principal,
            worker_store=worker_store,
            idempotency_key=idempotency_key,
        )

    @app.get(
        "/v1/jobs/{job_id}/snapshot",
        response_model=JobSnapshotResponse,
        operation_id="getJobSnapshot",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def get_job_snapshot(
        job_id: str,
        principal: Principal,
        worker_store: Store,
        after_segment_sequence: Annotated[int, Query(ge=0)] = 0,
        segment_limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> JobSnapshotResponse:
        _authorized_job(worker_store, principal, job_id)
        snapshot = worker_store.get_job_snapshot(
            job_id,
            after_segment_sequence=after_segment_sequence,
            segment_limit=segment_limit,
        )
        return _snapshot_schema(snapshot)

    @app.get(
        "/v1/jobs/{job_id}/events",
        response_model=JobUpdatesResponse,
        operation_id="getJobUpdates",
        tags=["jobs"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def get_job_updates(
        job_id: str,
        principal: Principal,
        worker_store: Store,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> JobUpdatesResponse:
        _authorized_job(worker_store, principal, job_id)
        updates, has_more = worker_store.list_job_updates(
            job_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return JobUpdatesResponse(
            updates=tuple(
                JobUpdateSchema.model_validate(update.to_dict()) for update in updates
            ),
            has_more=has_more,
            next_after_sequence=(updates[-1].sequence if updates else after_sequence),
        )

    @app.get(
        "/v1/jobs/{job_id}/artifacts",
        response_model=ArtifactListResponse,
        operation_id="listJobArtifacts",
        tags=["artifacts"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def list_job_artifacts(
        job_id: str,
        principal: Principal,
        worker_store: Store,
    ) -> ArtifactListResponse:
        _authorized_job(worker_store, principal, job_id)
        package = load_artifact_package(worker_store, job_id)
        return ArtifactListResponse(
            job_id=job_id,
            speech_id=package.speech_id,
            manifest_sha256=package.manifest_sha256,
            artifacts=tuple(
                ArtifactSchema.model_validate(asdict(artifact))
                for artifact in package.artifacts
            ),
        )

    @app.get(
        "/v1/jobs/{job_id}/artifacts/{artifact_name}",
        operation_id="downloadJobArtifact",
        tags=["artifacts"],
        responses=PRIVATE_ERROR_RESPONSES,
    )
    def download_job_artifact(
        job_id: str,
        artifact_name: ArtifactName,
        principal: Principal,
        worker_store: Store,
    ) -> FileResponse:
        _authorized_job(worker_store, principal, job_id)
        package = load_artifact_package(worker_store, job_id)
        descriptor = next(
            artifact for artifact in package.artifacts if artifact.name == artifact_name
        )
        return FileResponse(
            package.path_for(artifact_name),
            media_type=descriptor.media_type,
            filename=artifact_name,
            content_disposition_type="inline",
        )

    return app


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
    payload = ApiErrorResponse(
        error=ApiErrorSchema(
            code=code,
            message=message,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def _worker_error_status(exc: WorkerCoreError) -> int:
    if isinstance(exc, (JobNotFound, UploadNotFound, ArtifactNotFound)):
        return 404
    if isinstance(
        exc,
        (
            ArtifactVerificationFailed,
            IdempotencyConflict,
            InvalidTransition,
            RevisionConflict,
            SchedulerBusy,
            UploadIncomplete,
            UploadPartChecksumMismatch,
            UploadPartConflict,
            UploadStateConflict,
        ),
    ):
        return 409
    if isinstance(exc, InvalidJobRequest):
        return 400
    if exc.code in {
        "SOURCE_UNDECODABLE",
    }:
        return 422
    if exc.code in {
        "RESOURCE_PREFLIGHT_BLOCKED",
        "MEDIA_PROBE_UNAVAILABLE",
        "WORKER_PROCESSING_BUSY",
    }:
        return 503
    if exc.code in {
        "SOURCE_UPLOAD_NOT_VERIFIED",
        "UPLOAD_CHECKSUM_MISMATCH",
        "VAULT_PUBLICATION_CONFLICT",
        "PUBLICATION_LEASE_CONFLICT",
        "PUBLICATION_VERIFICATION_FAILED",
    }:
        return 409
    if exc.code == "PAIRING_CODE_INVALID":
        return 401
    if exc.code == "PAIRING_SESSION_EXPIRED":
        return 410
    if exc.code in {"PAIRING_SESSION_NOT_FOUND", "DEVICE_NOT_FOUND"}:
        return 404
    if exc.code == "DEVICE_ALREADY_PAIRED":
        return 409
    return 500


def _authorize_supplied_vault(principal: ApiPrincipal, vault_id: str) -> None:
    if not principal.allows(vault_id):
        raise ApiProblem(
            403,
            "VAULT_ACCESS_DENIED",
            "The credential is not authorized for the requested Vault.",
        )


def _authorize_existing_vault(principal: ApiPrincipal, vault_id: str) -> None:
    if not principal.allows(vault_id):
        raise ApiProblem(404, "RESOURCE_NOT_FOUND", "The requested resource was not found.")


def _authorized_job(store: JobStore, principal: ApiPrincipal, job_id: str) -> JobRecord:
    job = store.get_job(job_id)
    _authorize_existing_vault(principal, job.vault_id)
    return job


def _upload_schema(upload: UploadRecord) -> UploadSchema:
    return UploadSchema.model_validate(upload.to_dict())


def _upload_envelope(
    store: JobStore,
    upload: UploadRecord,
    *,
    created: bool | None = None,
) -> UploadEnvelope:
    return UploadEnvelope(
        upload=_upload_schema(upload),
        created=created,
        missing_part_numbers=tuple(store.list_missing_upload_parts(upload.upload_id)),
    )


def _job_schema(job: JobRecord) -> JobSchema:
    return JobSchema(
        job_id=job.job_id,
        vault_id=job.vault_id,
        source_upload_id=job.source_upload_id,
        source_display_name=job.source_display_name,
        source_sha256=job.source_sha256,
        source_size_bytes=job.source_size_bytes,
        state=job.state,
        model_profile=job.model_profile,
        language_hint=job.language_hint,
        content_type_override=job.content_type_override,
        recording_context=recording_context_from_options(job.options),
        revision=job.revision,
        last_error_code=job.last_error_code,
        last_error_message=job.last_error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _segment_schema(segment: TranscriptSegment) -> TranscriptSegmentSchema:
    payload = segment.to_dict()
    payload.pop("commit_key")
    return TranscriptSegmentSchema.model_validate(payload)


def _snapshot_schema(snapshot: JobSnapshot) -> JobSnapshotResponse:
    return JobSnapshotResponse(
        job=_job_schema(snapshot.job),
        progress=(
            JobProgressSchema.model_validate(snapshot.progress.to_dict())
            if snapshot.progress is not None
            else None
        ),
        stable_segments=tuple(_segment_schema(segment) for segment in snapshot.stable_segments),
        provisional=(
            ProvisionalTranscriptSchema.model_validate(snapshot.provisional.to_dict())
            if snapshot.provisional is not None
            else None
        ),
        resource_report=snapshot.resource_report,
        latest_event_sequence=snapshot.latest_event_sequence,
        next_after_segment_sequence=snapshot.next_after_segment_sequence,
        has_more_segments=snapshot.has_more_segments,
    )


app = create_app()
