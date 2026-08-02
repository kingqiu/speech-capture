"""Strict public schemas for the version and capability API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from speech_capture_worker.protocol_contract import (
    SEMANTIC_VERSION_PATTERN,
    CompatibilityIssue,
    ProtocolCapability,
    SemanticVersion,
)

VersionString = Annotated[str, Field(pattern=SEMANTIC_VERSION_PATTERN.pattern)]
CapabilityName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class PublicSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionRangeSchema(PublicSchema):
    minimum: VersionString
    maximum: VersionString

    @model_validator(mode="after")
    def validate_order(self) -> VersionRangeSchema:
        if SemanticVersion.parse(self.minimum) > SemanticVersion.parse(self.maximum):
            raise ValueError("Version range minimum must not exceed maximum.")
        return self


class ProtocolLimitsSchema(PublicSchema):
    default_upload_chunk_size_bytes: int = Field(gt=0)
    max_upload_chunk_size_bytes: int = Field(gt=0)
    max_upload_parts: int = Field(gt=0)
    max_snapshot_segments: int = Field(gt=0)
    max_update_events: int = Field(gt=0)
    max_recording_context_characters: int = Field(gt=0)


class HealthResponse(PublicSchema):
    status: Literal["ok"]
    worker_version: str
    protocol_version: VersionString


class CapabilitiesResponse(PublicSchema):
    worker_version: str
    protocol: VersionRangeSchema
    artifact_schema: VersionRangeSchema
    features: tuple[ProtocolCapability, ...]
    content_types: tuple[str, ...]
    model_profiles: tuple[str, ...]
    limits: ProtocolLimitsSchema


class CompatibilityRequestSchema(PublicSchema):
    protocol: VersionRangeSchema
    artifact_schema: VersionRangeSchema
    required_features: tuple[CapabilityName, ...] = Field(
        default=(),
        max_length=64,
    )

    @field_validator("required_features")
    @classmethod
    def reject_duplicate_features(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required_features must not contain duplicates.")
        return value


class CompatibilityResponse(PublicSchema):
    compatible: bool
    protocol_version: VersionString | None
    artifact_schema_version: VersionString | None
    missing_features: tuple[CapabilityName, ...]
    issues: tuple[CompatibilityIssue, ...]
