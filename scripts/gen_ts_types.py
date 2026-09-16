"""Generate `ui/shared/generated/ipc.ts` from the pydantic models in `src/nox/ipc/protocol.py`
and the event payload models in `src/nox/core/events.py` (`PAYLOAD_MODELS`).

OP-9 (Decision Plan 2026-09-11): the Python payload models are the single source of truth for the
wire contract; the TypeScript side must never hand-guess a shape (that is exactly the `chunk`/
`delta`/`text` drift this generator exists to prevent). Conversion is JSON-Schema -> TypeScript via
`BaseModel.model_json_schema()`, using a small dependency-free converter (no
`json-schema-to-typescript`/`datamodel-code-generator`) covering the shapes these models actually
use: string, number, boolean, array, object (interface or `Record<string, T>`), enum, optional
(JSON Schema `required`) and nullable (`anyOf [T, null]`). Anything else degrades to `unknown`
rather than guessing.

Usage:
    python scripts/gen_ts_types.py            # (re)write ui/shared/generated/ipc.ts
    python scripts/gen_ts_types.py --check     # exit 1 if the committed file is stale (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "ui" / "shared" / "generated" / "ipc.ts"

HEADER = """/**
 * GENERATED — do not edit by hand.
 *
 * Source of truth: `src/nox/ipc/protocol.py` and `src/nox/core/events.py` (PAYLOAD_MODELS).
 * Regenerate with:
 *   .venv/Scripts/python.exe scripts/gen_ts_types.py
 * `scripts/gen_ts_types.py --check` fails CI when this file is stale (OP-9, Decision Plan
 * 2026-09-11: the pydantic models are authoritative, the TypeScript types are derived).
 */
"""

JsonSchema = dict[str, Any]


def _ordered_models() -> list[type[BaseModel]]:
    """Models to emit, in a fixed, deterministic order."""
    from nox.core.events import PAYLOAD_MODELS
    from nox.ipc.protocol import (
        AuthRequest,
        AuthResponse,
        ChatSendResult,
        ChatStreamFrame,
        ClipExportResult,
        ClipListResult,
        ClipRecord,
        ClipTagResult,
        ClipTrimResult,
        ConfigEffective,
        ConfigFieldSchema,
        ConfigSetResult,
        ConfigSnapshot,
        Envelope,
        ErrorPayload,
        FunkenTop,
        FunkenTopEntry,
        HealthHistoryEntry,
        HealthHistoryResult,
        PersonalityText,
        RemoteDevice,
        RemoteDevices,
        RemotePairCode,
        RemoteUnpairResult,
        SecretsStatus,
        SecretStatus,
        SettingsOk,
        StreamPluginStatus,
        StreamSessionStatus,
        TwitchAuthStatus,
        TwitchDeviceCode,
    )

    ordered: list[type[BaseModel]] = [
        Envelope,
        AuthRequest,
        AuthResponse,
        ErrorPayload,
        ChatStreamFrame,
        ChatSendResult,
        StreamPluginStatus,
        StreamSessionStatus,
        FunkenTopEntry,
        FunkenTop,
        RemoteDevice,
        RemoteDevices,
        RemotePairCode,
        RemoteUnpairResult,
        HealthHistoryEntry,
        HealthHistoryResult,
        ConfigEffective,
        ClipRecord,
        ClipListResult,
        ClipTagResult,
        ClipExportResult,
        ClipTrimResult,
        ConfigFieldSchema,
        ConfigSnapshot,
        ConfigSetResult,
        SecretStatus,
        SecretsStatus,
        SettingsOk,
        TwitchDeviceCode,
        TwitchAuthStatus,
        PersonalityText,
    ]
    seen = {m.__name__ for m in ordered}
    # PAYLOAD_MODELS is a plain dict literal in events.py: iteration order is the declared,
    # deterministic source order.
    for model in PAYLOAD_MODELS.values():
        if model.__name__ not in seen:
            seen.add(model.__name__)
            ordered.append(model)
    return ordered


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return json.dumps(value)


def _ts_type(schema: JsonSchema) -> str:
    """Convert one JSON Schema fragment to a TypeScript type expression."""
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return _ts_type(schema["allOf"][0])
    if "anyOf" in schema or "oneOf" in schema:
        parts = schema.get("anyOf") or schema.get("oneOf") or []
        rendered: list[str] = []
        for part in parts:
            t = _ts_type(part)
            if t not in rendered:
                rendered.append(t)
        return " | ".join(rendered) if rendered else "unknown"
    if "const" in schema:
        return _literal(schema["const"])
    if "enum" in schema:
        return " | ".join(_literal(v) for v in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        # JSON Schema `"type": ["string", "null"]` (not emitted by pydantic v2 today, handled
        # defensively so a future model shape degrades gracefully instead of crashing).
        return " | ".join(_ts_type({**schema, "type": t}) for t in kind)
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        items = schema.get("items")
        item_t = _ts_type(items) if isinstance(items, dict) else "unknown"
        return f"({item_t})[]" if "|" in item_t else f"{item_t}[]"
    if kind == "object" or "properties" in schema or "additionalProperties" in schema:
        props = schema.get("properties")
        if props:
            return _render_object_literal(schema)
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"Record<string, {_ts_type(additional)}>"
        return "Record<string, unknown>"
    return "unknown"


def _render_object_literal(schema: JsonSchema) -> str:
    """Inline `{ field: type; ... }` for an anonymous nested object (no top-level name)."""
    fields = _render_fields(schema)
    return "{ " + " ".join(fields) + " }" if fields else "Record<string, unknown>"


def _render_fields(schema: JsonSchema) -> list[str]:
    required = set(schema.get("required", []))
    lines: list[str] = []
    for field_name, field_schema in schema.get("properties", {}).items():
        optional = "" if field_name in required else "?"
        lines.append(f"{field_name}{optional}: {_ts_type(field_schema)};")
    return lines


def _render_named_type(name: str, schema: JsonSchema) -> str:
    """A top-level `export interface` (object) or `export type` (enum/alias) declaration."""
    if "enum" in schema and schema.get("type") != "object":
        return f"export type {name} = {_ts_type(schema)};"
    if schema.get("type") == "object" or "properties" in schema:
        fields = _render_fields(schema)
        if not fields:
            return f"export type {name} = Record<string, unknown>;"
        body = "\n".join(f"  {line}" for line in fields)
        return f"export interface {name} {{\n{body}\n}}"
    return f"export type {name} = {_ts_type(schema)};"


def generate() -> str:
    models = _ordered_models()
    defs: dict[str, JsonSchema] = {}
    top_level: list[tuple[str, JsonSchema]] = []
    top_level_names: set[str] = set()

    for model in models:
        schema = model.model_json_schema()
        for def_name, def_schema in schema.get("$defs", {}).items():
            defs.setdefault(def_name, def_schema)
        own = {k: v for k, v in schema.items() if k != "$defs"}
        name = schema.get("title") or model.__name__
        top_level.append((name, own))
        top_level_names.add(name)

    blocks: list[str] = [HEADER.rstrip("\n")]
    for def_name in sorted(defs):
        if def_name in top_level_names:
            continue
        blocks.append(_render_named_type(def_name, defs[def_name]))
    for name, schema in top_level:
        blocks.append(_render_named_type(name, schema))

    return "\n\n".join(blocks) + "\n"


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if ui/shared/generated/ipc.ts is missing or does not match the models",
    )
    args = parser.parse_args(argv)

    content = generate()
    label = _display(OUTPUT_PATH)

    if args.check:
        if not OUTPUT_PATH.exists() or OUTPUT_PATH.read_text(encoding="utf-8") != content:
            print(f"{label} is stale; run `python scripts/gen_ts_types.py`", file=sys.stderr)
            return 1
        print(f"{label} is up to date")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
