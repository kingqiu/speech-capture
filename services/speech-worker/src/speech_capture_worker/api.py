"""Minimal versioned FastAPI surface for protocol discovery and negotiation."""

from __future__ import annotations

from fastapi import FastAPI

from speech_capture_worker import __version__
from speech_capture_worker.api_schemas import (
    CapabilitiesResponse,
    CompatibilityRequestSchema,
    CompatibilityResponse,
    HealthResponse,
)
from speech_capture_worker.protocol_contract import (
    PROTOCOL_VERSION,
    CompatibilityRequest,
    VersionRange,
    get_capabilities,
    negotiate_compatibility,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Speech Capture Worker API",
        summary="Versioned local and private-network speech processing API.",
        version=PROTOCOL_VERSION,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )

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

    return app


app = create_app()
