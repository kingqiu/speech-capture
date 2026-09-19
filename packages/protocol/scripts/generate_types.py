"""Generate dependency-free Python and TypeScript types from Worker OpenAPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import keyword
import re
import sys
from pathlib import Path
from typing import Any

GENERATED_PYTHON_PATH = Path("generated/python/speech_capture_protocol.py")
GENERATED_TYPESCRIPT_PATH = Path("generated/typescript/speech-capture-protocol.ts")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _json_literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _python_literal(value: object) -> str:
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    return repr(value)


def _schema_reference(schema: dict[str, Any]) -> str | None:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return None
    prefix = "#/components/schemas/"
    if not reference.startswith(prefix):
        raise ValueError(f"Unsupported external OpenAPI reference: {reference}")
    return reference.removeprefix(prefix)


def _python_type(schema: dict[str, Any]) -> str:
    reference = _schema_reference(schema)
    if reference is not None:
        return reference
    if "const" in schema:
        return f"Literal[{_python_literal(schema['const'])}]"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        values = ", ".join(_python_literal(value) for value in enum)
        return f"Literal[{values}]"
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        return " | ".join(dict.fromkeys(_python_type(item) for item in any_of))
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        item_type = _python_type(items) if isinstance(items, dict) else "object"
        return f"list[{item_type}]"
    if schema_type == "object":
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"dict[str, {_python_type(additional)}]"
        return "dict[str, object]"
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }.get(schema_type, "object")


def _typescript_type(schema: dict[str, Any]) -> str:
    reference = _schema_reference(schema)
    if reference is not None:
        return reference
    if "const" in schema:
        return _json_literal(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_json_literal(value) for value in enum)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        return " | ".join(dict.fromkeys(_typescript_type(item) for item in any_of))
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        item_type = _typescript_type(items) if isinstance(items, dict) else "unknown"
        return f"ReadonlyArray<{item_type}>"
    if schema_type == "object":
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"Readonly<Record<string, {_typescript_type(additional)}>>"
        return "Readonly<Record<string, unknown>>"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(schema_type, "unknown")


def _python_field_name(name: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(name) or keyword.iskeyword(name):
        raise ValueError(f"OpenAPI property is not a safe Python identifier: {name}")
    return name


def _typescript_field_name(name: str) -> str:
    return name if IDENTIFIER_PATTERN.fullmatch(name) else _json_literal(name)


def _validate_document(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    openapi_version = document.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.startswith("3.1."):
        raise ValueError("Type generation requires an OpenAPI 3.1 document.")
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict) or not schemas:
        raise ValueError("OpenAPI document has no component schemas.")
    for name, schema in schemas.items():
        if not isinstance(name, str) or not IDENTIFIER_PATTERN.fullmatch(name):
            raise ValueError(f"Schema name is not a safe identifier: {name}")
        if not isinstance(schema, dict):
            raise TypeError(f"Schema {name} must be an object.")
    return schemas


def _schema_dependencies(schema: object) -> set[str]:
    dependencies: set[str] = set()
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str):
            dependencies.add(_schema_reference(schema) or "")
        for value in schema.values():
            dependencies.update(_schema_dependencies(value))
    elif isinstance(schema, list):
        for value in schema:
            dependencies.update(_schema_dependencies(value))
    dependencies.discard("")
    return dependencies


def _ordered_objects(
    objects: list[tuple[str, dict[str, Any]]],
    known_names: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    remaining = dict(objects)
    ordered: list[tuple[str, dict[str, Any]]] = []
    while remaining:
        ready = sorted(
            name
            for name, schema in remaining.items()
            if _schema_dependencies(schema) <= known_names
        )
        if not ready:
            unresolved = ", ".join(sorted(remaining))
            raise ValueError(f"Cyclic or unresolved OpenAPI schemas: {unresolved}")
        for name in ready:
            schema = remaining.pop(name)
            ordered.append((name, schema))
            known_names.add(name)
    return ordered


def _python_alias_lines(name: str, schema: dict[str, Any]) -> list[str]:
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return [
            f"{name}: TypeAlias = Literal[",
            *[f"    {_python_literal(value)}," for value in enum],
            "]",
        ]
    return [f"{name}: TypeAlias = {_python_type(schema)}"]


def generate_python(document: dict[str, Any], source_sha256: str) -> str:
    schemas = _validate_document(document)
    names = sorted(schemas)
    lines = [
        '"""Generated Worker protocol wire types. Do not edit manually."""',
        "",
        "from typing import Final, Literal, NotRequired, TypeAlias, TypedDict",
        "",
        f'OPENAPI_SHA256: Final = "{source_sha256}"',
        f'OPENAPI_VERSION: Final = "{document["openapi"]}"',
        f'PROTOCOL_VERSION: Final = "{document["info"]["version"]}"',
        "",
    ]
    aliases: list[tuple[str, dict[str, Any]]] = []
    objects: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        schema = schemas[name]
        if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
            objects.append((name, schema))
        else:
            aliases.append((name, schema))
    for name, schema in aliases:
        lines.extend((*_python_alias_lines(name, schema), ""))
    for name, schema in _ordered_objects(objects, {name for name, _ in aliases}):
        lines.append(f"class {name}(TypedDict):")
        properties = schema["properties"]
        required = set(schema.get("required", []))
        if not properties:
            lines.append("    pass")
        for field_name in sorted(properties):
            field_schema = properties[field_name]
            field_type = _python_type(field_schema)
            if field_name not in required:
                field_type = f"NotRequired[{field_type}]"
            lines.append(f"    {_python_field_name(field_name)}: {field_type}")
        lines.append("")
    exports = ["OPENAPI_SHA256", "OPENAPI_VERSION", "PROTOCOL_VERSION", *names]
    lines.append("__all__ = [")
    lines.extend(f'    "{name}",' for name in exports)
    lines.extend(("]", ""))
    return "\n".join(lines)


def generate_typescript(document: dict[str, Any], source_sha256: str) -> str:
    schemas = _validate_document(document)
    lines = [
        "// Generated Worker protocol wire types. Do not edit manually.",
        f'export const OPENAPI_SHA256 = "{source_sha256}" as const;',
        f'export const OPENAPI_VERSION = "{document["openapi"]}" as const;',
        f'export const PROTOCOL_VERSION = "{document["info"]["version"]}" as const;',
        "",
    ]
    for name in sorted(schemas):
        schema = schemas[name]
        properties = schema.get("properties")
        if schema.get("type") == "object" and isinstance(properties, dict):
            lines.append(f"export interface {name} {{")
            required = set(schema.get("required", []))
            for field_name in sorted(properties):
                optional = "" if field_name in required else "?"
                lines.append(
                    f"  readonly {_typescript_field_name(field_name)}{optional}: "
                    f"{_typescript_type(properties[field_name])};"
                )
            lines.extend(("}", ""))
        else:
            enum = schema.get("enum")
            if isinstance(enum, list) and enum:
                lines.append(f"export type {name} =")
                for index, value in enumerate(enum):
                    suffix = ";" if index == len(enum) - 1 else ""
                    lines.append(f"  | {_json_literal(value)}{suffix}")
                lines.append("")
            else:
                lines.extend((f"export type {name} = {_typescript_type(schema)};", ""))
    return "\n".join(lines)


def generated_files(openapi_path: Path) -> dict[Path, str]:
    source = openapi_path.read_bytes()
    document = json.loads(source)
    if not isinstance(document, dict):
        raise TypeError("OpenAPI root must be an object.")
    source_sha256 = hashlib.sha256(source).hexdigest()
    return {
        GENERATED_PYTHON_PATH: generate_python(document, source_sha256),
        GENERATED_TYPESCRIPT_PATH: generate_typescript(document, source_sha256),
    }


def write_or_check(openapi_path: Path, output_root: Path, *, check: bool) -> bool:
    current = True
    for relative_path, expected in generated_files(openapi_path).items():
        target = output_root / relative_path
        if check:
            actual = target.read_text(encoding="utf-8") if target.is_file() else None
            if actual != expected:
                print(f"Generated protocol type is stale: {target}", file=sys.stderr)
                current = False
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(expected, encoding="utf-8")
        temporary.replace(target)
    return current


def main() -> None:
    protocol_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openapi", type=Path, default=protocol_root / "openapi.json")
    parser.add_argument("--output-root", type=Path, default=protocol_root)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not write_or_check(
        arguments.openapi.resolve(),
        arguments.output_root.resolve(),
        check=arguments.check,
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
