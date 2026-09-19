"""Strict, side-effect-free loading for versioned content profile bundles.

Phase B1 deliberately does not connect these bundles to the structuring runtime.  The
loader establishes the trust boundary first: a bundle is either completely validated
or it is not available to a future resolver.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from speech_capture_worker import __version__ as WORKER_VERSION
from speech_capture_worker.domain import SUPPORTED_CONTENT_TYPES
from speech_capture_worker.meeting_field_repairs import (
    MAX_FIELD_CALL_SECONDS,
    MAX_HEARTBEAT_SECONDS,
    MAX_PACKET_CHARACTERS,
    MAX_PACKET_ESTIMATED_TOKENS,
    MAX_PACKET_SEGMENTS,
    MAX_PARSER_RETRIES_PER_REPAIR,
    MAX_REPAIR_CALLS,
    MAX_REPAIR_FIELD_CHARACTERS,
    MAX_REPAIR_OUTPUT_TOKENS,
    MAX_TOTAL_REPAIR_SECONDS,
    MEETING_FIELD_REPAIR_KEYS,
)
from speech_capture_worker.meeting_semantic_gate import MEETING_SEMANTIC_VALIDATORS

BUNDLE_SCHEMA_VERSION = "1.0.0"
DOCUMENT_SCHEMA_ID = "speech-capture/structured-note"
DOCUMENT_SCHEMA_VERSION = "1.0.0"

SUPPORTED_PROMPT_SLOTS = frozenset(
    {"extraction", "synthesis", "coverage_repair", "quality_edit", "named_repairs"}
)
SUPPORTED_FIELD_REPAIRS = MEETING_FIELD_REPAIR_KEYS
SUPPORTED_NAMED_REPAIRS = frozenset({"meeting_outcomes", *SUPPORTED_FIELD_REPAIRS})
SUPPORTED_MODEL_ROLES = frozenset({"primary", "editor"})
SUPPORTED_EXECUTION_ROLES = frozenset(
    {"classification", "extraction", "synthesis", "quality_edit"}
)
SUPPORTED_VALIDATORS = frozenset(
    {
        "meeting.context.sufficient",
        "meeting.decision.confirmed",
        "meeting.action.evidence_complete",
        "meeting.categories.nonduplicated",
        *MEETING_SEMANTIC_VALIDATORS,
    }
)
SUPPORTED_DOCUMENT_FIELDS = frozenset(
    {
        "title",
        "objective",
        "summary",
        "context",
        "highlights",
        "topics",
        "scene_sections",
        "discussion_threads",
        "timeline_sections",
        "speaker_summaries",
        "decisions",
        "actions",
        "risks",
        "open_questions",
    }
)

_MANIFEST_FIELDS = frozenset(
    {
        "bundle_schema_version",
        "profile_id",
        "profile_version",
        "content_type",
        "document_schema",
        "engine_compatibility",
        "prompts",
        "document_policy",
        "execution_policy",
        "validation_policy",
        "renderer",
        "fixtures_manifest",
        "fallback_profile",
        "files",
        "bundle_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")


class ProfileBundleError(ValueError):
    """Raised when a content profile bundle fails static validation."""


@dataclass(frozen=True)
class ProfileReference:
    profile_id: str
    profile_version: str
    bundle_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "bundle_sha256": self.bundle_sha256,
        }


@dataclass(frozen=True)
class ProfileBundle:
    root: Path
    profile_id: str
    profile_version: str
    content_type: str
    bundle_sha256: str
    document_schema_version: str
    renderer_version: str
    manifest: Mapping[str, Any]
    prompts: Mapping[str, str | None | Mapping[str, str]]
    document_policy: Mapping[str, Any]
    execution_policy: Mapping[str, Any]
    validation_policy: Mapping[str, Any]
    renderer: Mapping[str, Any]

    @property
    def reference(self) -> ProfileReference:
        return ProfileReference(
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            bundle_sha256=self.bundle_sha256,
        )

    def read_prompt(self, slot: str, *, repair_name: str | None = None) -> str | None:
        """Read a validated prompt as inert UTF-8 text.

        No template interpolation occurs here.  A future runtime adapter may only
        substitute engine-registered placeholders after its own validation.
        """

        if slot not in SUPPORTED_PROMPT_SLOTS:
            raise KeyError(slot)
        value = self.prompts[slot]
        if slot == "named_repairs":
            if repair_name is None or not isinstance(value, Mapping):
                raise KeyError(repair_name)
            path = value.get(repair_name)
        else:
            path = value
        if path is None:
            return None
        if not isinstance(path, str):
            raise ProfileBundleError(f"Prompt slot {slot!r} does not contain a file path.")
        return (self.root / path).read_text(encoding="utf-8")


class ProfileBundleRegistry:
    """In-memory last-known-good index for already validated bundles.

    The registry has no runtime integration in B1.  It makes collision and fallback
    behavior executable and testable before any task can resolve a profile from it.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ProfileBundle] = {}
        self._last_known_good: dict[str, ProfileReference] = {}

    def register_validated(self, bundle: ProfileBundle) -> None:
        key = (bundle.profile_id, bundle.profile_version)
        existing = self._by_key.get(key)
        if existing is not None and existing.bundle_sha256 != bundle.bundle_sha256:
            raise ProfileBundleError(
                "A profile_id and profile_version pair cannot refer to different hashes."
            )
        self._by_key[key] = bundle

    def mark_last_known_good(self, reference: ProfileReference) -> None:
        bundle = self.resolve(reference)
        self._last_known_good[bundle.content_type] = bundle.reference

    def resolve(self, reference: ProfileReference) -> ProfileBundle:
        bundle = self._by_key.get((reference.profile_id, reference.profile_version))
        if bundle is None or bundle.bundle_sha256 != reference.bundle_sha256:
            raise ProfileBundleError("The exact pinned profile bundle is not registered.")
        return bundle

    def last_known_good(self, content_type: str) -> ProfileBundle | None:
        reference = self._last_known_good.get(content_type)
        return self.resolve(reference) if reference is not None else None


def load_profile_bundle(
    root: str | Path,
    *,
    engine_version: str = WORKER_VERSION,
) -> ProfileBundle:
    """Load and completely validate a bundle rooted at ``root``.

    Validation is intentionally strict: unknown keys, unlisted files, symlinks,
    path escape, incompatible versions, unknown registered names, and any checksum
    mismatch reject the entire bundle.
    """

    bundle_root = Path(root)
    if not bundle_root.is_dir() or bundle_root.is_symlink():
        raise ProfileBundleError("Profile bundle root must be a real directory.")
    manifest_path = bundle_root / "profile.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ProfileBundleError("Profile bundle must contain a regular profile.json file.")
    manifest = _read_json_object(manifest_path, label="profile.json")
    _require_exact_fields(manifest, _MANIFEST_FIELDS, label="profile.json")

    if manifest["bundle_schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise ProfileBundleError("Unsupported bundle_schema_version.")
    profile_id = _require_string(manifest, "profile_id", pattern=_PROFILE_ID_PATTERN)
    profile_version = _require_printable_string(manifest, "profile_version")
    content_type = _require_printable_string(manifest, "content_type")
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise ProfileBundleError("Unsupported content_type.")

    document_schema = _require_object(manifest, "document_schema")
    _require_exact_fields(document_schema, {"id", "version"}, label="document_schema")
    if document_schema["id"] != DOCUMENT_SCHEMA_ID:
        raise ProfileBundleError("Unsupported document schema id.")
    if document_schema["version"] != DOCUMENT_SCHEMA_VERSION:
        raise ProfileBundleError("Unsupported document schema version.")

    compatibility = _require_object(manifest, "engine_compatibility")
    _require_exact_fields(
        compatibility,
        {"minimum", "maximum_exclusive"},
        label="engine_compatibility",
    )
    _validate_engine_compatibility(compatibility, engine_version=engine_version)

    files = _validate_declared_files(bundle_root, _require_object(manifest, "files"))
    prompts = _validate_prompts(_require_object(manifest, "prompts"), files=files)
    referenced_paths = _referenced_paths(manifest, prompts=prompts)
    if referenced_paths != set(files):
        missing = sorted(set(files) - referenced_paths)
        undeclared = sorted(referenced_paths - set(files))
        raise ProfileBundleError(
            f"Bundle file references must exactly match files; unreferenced={missing}, "
            f"missing={undeclared}."
        )

    document_policy = _validate_document_policy(
        _read_declared_json(bundle_root, manifest["document_policy"], files=files)
    )
    execution_policy = _validate_execution_policy(
        _read_declared_json(bundle_root, manifest["execution_policy"], files=files)
    )
    _validate_profile_cross_references(
        content_type=content_type,
        prompts=prompts,
        execution_policy=execution_policy,
    )
    validation_policy = _validate_validation_policy(
        _read_declared_json(bundle_root, manifest["validation_policy"], files=files)
    )
    renderer = _validate_renderer(
        _read_declared_json(bundle_root, manifest["renderer"], files=files)
    )
    _validate_fixtures_manifest(
        _read_declared_json(bundle_root, manifest["fixtures_manifest"], files=files)
    )
    fallback = _require_object(manifest, "fallback_profile")
    _require_exact_fields(
        fallback,
        {"profile_id", "profile_version"},
        label="fallback_profile",
    )
    _require_string(fallback, "profile_id", pattern=_PROFILE_ID_PATTERN)
    _require_printable_string(fallback, "profile_version")

    expected_bundle_hash = _require_hash(manifest["bundle_sha256"], "bundle_sha256")
    actual_bundle_hash = compute_bundle_sha256(manifest)
    if actual_bundle_hash != expected_bundle_hash:
        raise ProfileBundleError("bundle_sha256 does not match the canonical manifest.")

    return ProfileBundle(
        root=bundle_root.resolve(strict=True),
        profile_id=profile_id,
        profile_version=profile_version,
        content_type=content_type,
        bundle_sha256=actual_bundle_hash,
        document_schema_version=document_schema["version"],
        renderer_version=renderer["renderer_version"],
        manifest=MappingProxyType(manifest),
        prompts=MappingProxyType(prompts),
        document_policy=MappingProxyType(document_policy),
        execution_policy=MappingProxyType(execution_policy),
        validation_policy=MappingProxyType(validation_policy),
        renderer=MappingProxyType(renderer),
    )


def compute_bundle_sha256(manifest: Mapping[str, Any]) -> str:
    """Return the canonical bundle hash for a parsed manifest.

    File content hashes are already part of ``files``.  They are duplicated in a
    sorted list to make the total-hash input explicit and independently inspectable.
    """

    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("bundle_sha256", None)
    files = manifest_without_hash.get("files")
    if not isinstance(files, dict):
        raise ProfileBundleError("files must be a JSON object before hashing.")
    payload = {
        "manifest": manifest_without_hash,
        "file_hashes": [[path, files[path]] for path in sorted(files)],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_declared_files(root: Path, files: dict[str, Any]) -> dict[str, str]:
    if not files:
        raise ProfileBundleError("files must not be empty.")
    normalized: dict[str, str] = {}
    for raw_path, raw_hash in files.items():
        path = _validate_relative_path(raw_path)
        if path in normalized:
            raise ProfileBundleError("files contains a duplicate normalized path.")
        expected_hash = _require_hash(raw_hash, f"files[{path!r}]")
        resolved = _resolve_regular_file(root, path)
        actual_hash = f"sha256:{hashlib.sha256(resolved.read_bytes()).hexdigest()}"
        if actual_hash != expected_hash:
            raise ProfileBundleError(f"Checksum mismatch for {path!r}.")
        normalized[path] = expected_hash

    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item.name != "profile.json"
    }
    if actual_files != set(normalized):
        raise ProfileBundleError("Bundle contains missing or unlisted files.")
    return normalized


def _validate_prompts(
    prompts: dict[str, Any], *, files: Mapping[str, str]
) -> dict[str, str | None | Mapping[str, str]]:
    _require_exact_fields(prompts, SUPPORTED_PROMPT_SLOTS, label="prompts")
    result: dict[str, str | None | Mapping[str, str]] = {}
    for slot in SUPPORTED_PROMPT_SLOTS - {"named_repairs"}:
        value = prompts[slot]
        if value is None:
            result[slot] = None
            continue
        path = _validate_relative_path(value)
        if path not in files or not path.endswith(".md"):
            raise ProfileBundleError(f"Prompt slot {slot!r} must reference a declared .md file.")
        result[slot] = path

    repairs = prompts["named_repairs"]
    if not isinstance(repairs, dict):
        raise ProfileBundleError("prompts.named_repairs must be a JSON object.")
    unknown_repairs = set(repairs) - SUPPORTED_NAMED_REPAIRS
    if unknown_repairs:
        raise ProfileBundleError(f"Unknown named repair: {sorted(unknown_repairs)!r}.")
    normalized_repairs: dict[str, str] = {}
    for name, value in repairs.items():
        path = _validate_relative_path(value)
        if path not in files or not path.endswith(".md"):
            raise ProfileBundleError("Named repairs must reference declared .md files.")
        normalized_repairs[name] = path
    result["named_repairs"] = MappingProxyType(normalized_repairs)
    return result


def _referenced_paths(
    manifest: Mapping[str, Any], *, prompts: Mapping[str, Any]
) -> set[str]:
    paths = {
        _validate_relative_path(manifest[key])
        for key in (
            "document_policy",
            "execution_policy",
            "validation_policy",
            "renderer",
            "fixtures_manifest",
        )
    }
    for name, value in prompts.items():
        if name == "named_repairs":
            paths.update(value.values())
        elif value is not None:
            paths.add(value)
    return paths


def _validate_document_policy(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        value,
        {"required_nonempty", "allowed_empty", "body_source", "field_limits"},
        label="document-policy.json",
    )
    required = _string_list(value["required_nonempty"], "required_nonempty")
    allowed = _string_list(value["allowed_empty"], "allowed_empty")
    if set(required) & set(allowed):
        raise ProfileBundleError("A document field cannot be both required and allowed empty.")
    if (set(required) | set(allowed)) - SUPPORTED_DOCUMENT_FIELDS:
        raise ProfileBundleError("document policy references an unknown document field.")
    body_source = value["body_source"]
    if body_source not in {"topics", "scene_sections"}:
        raise ProfileBundleError("body_source is not registered.")
    limits = value["field_limits"]
    if not isinstance(limits, dict) or not limits:
        raise ProfileBundleError("field_limits must be a non-empty JSON object.")
    for field, limit in limits.items():
        if field not in SUPPORTED_DOCUMENT_FIELDS or not isinstance(limit, int) or limit < 0:
            raise ProfileBundleError("field_limits contains an invalid field or limit.")
    return value


def _validate_execution_policy(value: dict[str, Any]) -> dict[str, Any]:
    legacy_fields = {
        "roles",
        "batch_target_tokens",
        "maximum_quality_passes",
        "enabled_registered_repairs",
    }
    extended_fields = legacy_fields | {"field_repairs"}
    if set(value) not in {frozenset(legacy_fields), frozenset(extended_fields)}:
        _require_exact_fields(value, extended_fields, label="execution-policy.json")
    roles = value["roles"]
    if not isinstance(roles, dict) or set(roles) != SUPPORTED_EXECUTION_ROLES:
        raise ProfileBundleError("execution roles must contain the registered role slots exactly.")
    if any(role not in SUPPORTED_MODEL_ROLES for role in roles.values()):
        raise ProfileBundleError("execution policy references an unknown model role.")
    if not isinstance(value["batch_target_tokens"], int) or not (
        256 <= value["batch_target_tokens"] <= 8192
    ):
        raise ProfileBundleError("batch_target_tokens is outside the registered safe range.")
    if not isinstance(value["maximum_quality_passes"], int) or not (
        0 <= value["maximum_quality_passes"] <= 1
    ):
        raise ProfileBundleError("maximum_quality_passes is outside the registered safe range.")
    repairs = _string_list(value["enabled_registered_repairs"], "enabled_registered_repairs")
    if set(repairs) - {"meeting_outcomes"}:
        raise ProfileBundleError("execution policy enables an unknown repair.")
    if "field_repairs" in value:
        _validate_field_repairs(value["field_repairs"])
    return value


def _validate_field_repairs(value: Any) -> None:
    if not isinstance(value, dict):
        raise ProfileBundleError("field_repairs must be a JSON object.")
    _require_exact_fields(
        value,
        {"maximum_calls", "total_timeout_seconds", "heartbeat_seconds", "repairs"},
        label="field_repairs",
    )
    _bounded_integer(
        value["maximum_calls"],
        label="field_repairs.maximum_calls",
        minimum=0,
        maximum=MAX_REPAIR_CALLS,
    )
    _bounded_number(
        value["total_timeout_seconds"],
        label="field_repairs.total_timeout_seconds",
        maximum=MAX_TOTAL_REPAIR_SECONDS,
    )
    _bounded_number(
        value["heartbeat_seconds"],
        label="field_repairs.heartbeat_seconds",
        maximum=MAX_HEARTBEAT_SECONDS,
    )
    repairs = value["repairs"]
    if not isinstance(repairs, dict):
        raise ProfileBundleError("field_repairs.repairs must be a JSON object.")
    unknown = set(repairs) - SUPPORTED_FIELD_REPAIRS
    if unknown:
        raise ProfileBundleError(f"field_repairs enables an unknown repair: {sorted(unknown)!r}.")
    if len(repairs) > MAX_REPAIR_CALLS:
        raise ProfileBundleError("field_repairs enables too many registered repair types.")
    if value["maximum_calls"] > 0 and not repairs:
        raise ProfileBundleError("field_repairs with a call budget must enable a repair.")
    if value["maximum_calls"] == 0 and repairs:
        raise ProfileBundleError("field_repairs cannot enable repairs with a zero call budget.")
    for repair_key, policy in repairs.items():
        _validate_field_repair_policy(repair_key, policy)


def _validate_field_repair_policy(repair_key: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ProfileBundleError(f"field_repairs.repairs[{repair_key!r}] must be an object.")
    fields = {
        "model_role",
        "maximum_output_tokens",
        "maximum_field_characters",
        "maximum_evidence_segments",
        "maximum_evidence_characters",
        "maximum_evidence_tokens",
        "call_timeout_seconds",
        "maximum_parser_retries",
    }
    _require_exact_fields(value, fields, label=f"field_repairs.repairs[{repair_key!r}]")
    if value["model_role"] != "editor":
        raise ProfileBundleError("field repair model_role must be the registered editor role.")
    _bounded_integer(
        value["maximum_output_tokens"],
        label="field repair maximum_output_tokens",
        minimum=1,
        maximum=MAX_REPAIR_OUTPUT_TOKENS,
    )
    _bounded_integer(
        value["maximum_field_characters"],
        label="field repair maximum_field_characters",
        minimum=1,
        maximum=MAX_REPAIR_FIELD_CHARACTERS,
    )
    _bounded_integer(
        value["maximum_evidence_segments"],
        label="field repair maximum_evidence_segments",
        minimum=1,
        maximum=MAX_PACKET_SEGMENTS,
    )
    _bounded_integer(
        value["maximum_evidence_characters"],
        label="field repair maximum_evidence_characters",
        minimum=1,
        maximum=MAX_PACKET_CHARACTERS,
    )
    _bounded_integer(
        value["maximum_evidence_tokens"],
        label="field repair maximum_evidence_tokens",
        minimum=1,
        maximum=MAX_PACKET_ESTIMATED_TOKENS,
    )
    _bounded_number(
        value["call_timeout_seconds"],
        label="field repair call_timeout_seconds",
        maximum=MAX_FIELD_CALL_SECONDS,
    )
    _bounded_integer(
        value["maximum_parser_retries"],
        label="field repair maximum_parser_retries",
        minimum=0,
        maximum=MAX_PARSER_RETRIES_PER_REPAIR,
    )


def _validate_profile_cross_references(
    *,
    content_type: str,
    prompts: Mapping[str, Any],
    execution_policy: Mapping[str, Any],
) -> None:
    field_repairs = execution_policy.get("field_repairs")
    named_repairs = prompts["named_repairs"]
    configured_prompts = set(named_repairs) & SUPPORTED_FIELD_REPAIRS
    if field_repairs is None:
        if configured_prompts:
            raise ProfileBundleError("Field repair prompts require a field_repairs policy.")
        return
    if content_type != "meeting":
        raise ProfileBundleError("field_repairs is currently registered only for meetings.")
    configured_repairs = set(field_repairs["repairs"])
    if configured_prompts != configured_repairs:
        raise ProfileBundleError(
            "field_repairs and registered field repair prompts must match exactly."
        )


def _bounded_integer(value: Any, *, label: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProfileBundleError(f"{label} is outside the registered safe range.")


def _bounded_number(value: Any, *, label: str, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
        raise ProfileBundleError(f"{label} is outside the registered safe range.")


def _validate_validation_policy(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        value,
        {"registered_validators", "thresholds"},
        label="validation-policy.json",
    )
    validators = _string_list(value["registered_validators"], "registered_validators")
    if set(validators) - SUPPORTED_VALIDATORS:
        raise ProfileBundleError("validation policy references an unknown validator.")
    thresholds = value["thresholds"]
    if not isinstance(thresholds, dict):
        raise ProfileBundleError("thresholds must be a JSON object.")
    allowed_thresholds = {"minimum_context_facets", "single_context_minimum_characters"}
    _require_exact_fields(thresholds, allowed_thresholds, label="thresholds")
    if any(not isinstance(item, int) or item < 0 for item in thresholds.values()):
        raise ProfileBundleError("validation thresholds must be non-negative integers.")
    return value


def _validate_renderer(value: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        value,
        {
            "renderer_version",
            "document_schema_version",
            "sections",
            "timeline_output",
            "evidence_output",
        },
        label="renderer.json",
    )
    if value["document_schema_version"] != DOCUMENT_SCHEMA_VERSION:
        raise ProfileBundleError("renderer document schema is incompatible.")
    _parse_version(_require_printable_string(value, "renderer_version"))
    sections = value["sections"]
    if not isinstance(sections, list) or not sections:
        raise ProfileBundleError("renderer sections must be a non-empty array.")
    seen: set[str] = set()
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ProfileBundleError("renderer section must be a JSON object.")
        _require_exact_fields(section, {"field", "heading", "when"}, label=f"sections[{index}]")
        field = _require_printable_string(section, "field")
        if field not in SUPPORTED_DOCUMENT_FIELDS or field in seen:
            raise ProfileBundleError("renderer contains an unknown or duplicate field.")
        seen.add(field)
        _require_printable_string(section, "heading")
        if section["when"] not in {"always", "nonempty"}:
            raise ProfileBundleError("renderer when must be always or nonempty.")
    if value["timeline_output"] != "separate_markdown":
        raise ProfileBundleError("timeline_output is not registered.")
    if value["evidence_output"] != "separate_markdown":
        raise ProfileBundleError("evidence_output is not registered.")
    return value


def _validate_fixtures_manifest(value: dict[str, Any]) -> None:
    _require_exact_fields(
        value,
        {"fixture_schema_version", "fixtures"},
        label="fixtures/manifest.json",
    )
    if value["fixture_schema_version"] != "1.0.0":
        raise ProfileBundleError("Unsupported fixture_schema_version.")
    fixtures = value["fixtures"]
    if not isinstance(fixtures, list):
        raise ProfileBundleError("fixtures must be an array.")
    seen: set[str] = set()
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, dict):
            raise ProfileBundleError("fixture entry must be a JSON object.")
        _require_exact_fields(fixture, {"id", "description"}, label=f"fixtures[{index}]")
        fixture_id = _require_printable_string(fixture, "id")
        if fixture_id in seen:
            raise ProfileBundleError("fixture ids must be unique.")
        seen.add(fixture_id)
        _require_printable_string(fixture, "description")


def _read_declared_json(root: Path, raw_path: Any, *, files: Mapping[str, str]) -> dict[str, Any]:
    path = _validate_relative_path(raw_path)
    if path not in files or not path.endswith(".json"):
        raise ProfileBundleError(f"{path!r} must be a declared JSON file.")
    return _read_json_object(_resolve_regular_file(root, path), label=path)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileBundleError(f"{label} is not valid UTF-8 JSON.") from error
    if not isinstance(value, dict):
        raise ProfileBundleError(f"{label} must contain a JSON object.")
    return value


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProfileBundleError("Bundle paths must be non-empty POSIX relative paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProfileBundleError("Bundle path escapes or is not canonical.")
    return path.as_posix()


def _resolve_regular_file(root: Path, relative_path: str) -> Path:
    candidate = root / relative_path
    if candidate.is_symlink() or not candidate.is_file():
        raise ProfileBundleError(f"{relative_path!r} must be a regular non-symlink file.")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ProfileBundleError("Bundle file escapes its root directory.") from error
    return candidate


def _validate_engine_compatibility(value: Mapping[str, Any], *, engine_version: str) -> None:
    minimum = _parse_version(value["minimum"])
    maximum = _parse_version(value["maximum_exclusive"])
    current = _parse_version(engine_version)
    if not minimum <= current < maximum:
        raise ProfileBundleError("Profile bundle is incompatible with this Worker version.")


def _parse_version(value: Any) -> tuple[int, int, int, int, int]:
    if not isinstance(value, str):
        raise ProfileBundleError("Version must be a string.")
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ProfileBundleError(f"Unsupported version syntax: {value!r}.")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    stage_name = match.group(4)
    stage_number = int(match.group(5) or 0)
    stage = {"a": -3, "b": -2, "rc": -1, None: 0}[stage_name]
    return major, minor, patch, stage, stage_number


def _require_exact_fields(
    value: Mapping[str, Any],
    fields: set[str] | frozenset[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    expected = set(fields)
    if actual != expected:
        raise ProfileBundleError(
            f"{label} fields do not match the contract; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}."
        )


def _require_object(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ProfileBundleError(f"{key} must be a JSON object.")
    return result


def _require_printable_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result or len(result) > 256 or not result.isprintable():
        raise ProfileBundleError(f"{key} must be a non-empty printable string.")
    return result


def _require_string(value: Mapping[str, Any], key: str, *, pattern: re.Pattern[str]) -> str:
    result = _require_printable_string(value, key)
    if pattern.fullmatch(result) is None:
        raise ProfileBundleError(f"{key} has invalid syntax.")
    return result


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ProfileBundleError(f"{label} must be a lowercase sha256: digest.")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ProfileBundleError(f"{label} must be an array of non-empty strings.")
    if len(value) != len(set(value)):
        raise ProfileBundleError(f"{label} must not contain duplicates.")
    return value
