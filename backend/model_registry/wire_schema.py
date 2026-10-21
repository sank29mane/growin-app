"""The strict wire schema of a structured answer, and its strict decoder.

``run_structured`` sends ``response_format.json_schema.strict = true``. OpenAI's
strict mode accepts only a subset of JSON Schema: every object has
``additionalProperties: false``, every property is listed in ``required``, no
object is open, and bounds such as ``minimum`` are not enforced. A raw Pydantic
schema breaks all of that, and a server that enforces strict mode answers 400.

``wire_spec(model)`` builds the schema that goes on the wire from a Pydantic
model, with one generic walk and no provider branching:

* every object gets ``additionalProperties: false`` and lists all its properties
  in ``required``;
* a field that has a default becomes nullable (``anyOf [T, null]``). ``null``
  means "use the default"; ``WireSpec.to_native_json`` drops the key so the real
  model's default applies, and nothing else is defaulted;
* ``$ref`` is inlined, so the schema has no ``$defs``;
* annotation and bound keywords (``title``, ``default``, ``minimum``,
  ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``) are dropped. The bounds
  are not lost: the reply is validated strictly against the real Pydantic model,
  which still enforces them. Any other keyword raises ``WireSchemaError``, so a
  model that needs something outside the subset fails loudly at build time;
* an open object (``Dict[str, Any]``) cannot be expressed in strict mode. A field
  opts in with ``json_schema_extra={"x-wire-encoding": "json_string"}`` and goes
  on the wire as ``<name>_json``, a string holding one JSON object. The decoder
  runs ``json.loads`` on it and requires a JSON object; anything else is a
  ``WireReplyError``. An open object with no such opt-in raises
  ``WireSchemaError``.

The decoder is as strict as the schema it sent. The reply must be strict JSON
(no ``NaN``, no number that overflows to infinity, no duplicate keys, no nesting
deep enough to exhaust the parser; each is a ``WireReplyError``), every object must carry exactly the wire
properties (a missing or extra key is an error), and the result is handed back as
JSON text for ``model_validate_json(..., strict=True)`` against the real model.
There is no lenient parse and no repair.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, Tuple

from pydantic import BaseModel

ENCODING_KEY = "x-wire-encoding"
ENCODING_JSON_STRING = "json_string"

_DROPPED = frozenset(
    {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
)
_KEPT = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf", "description"}
)
_SCALARS = frozenset({"string", "integer", "number", "boolean"})

_OMIT = object()  # a null for a defaulted field: leave the key out


class WireSchemaError(Exception):
    """The model has a shape the strict wire schema cannot express. A build-time bug."""


class WireReplyError(ValueError):
    """The reply does not match the wire schema that was sent."""


_Decoder = Callable[[Any], Any]


def _fail(message: str) -> "WireReplyError":
    return WireReplyError(message)


def _identity(value: Any) -> Any:
    return value


def _strict_loads(text: str) -> Any:
    def reject_constant(name: str) -> Any:
        raise WireReplyError(f"non-JSON constant {name}")

    def finite_float(literal: str) -> float:
        value = float(literal)
        if not math.isfinite(value):
            raise WireReplyError("number out of range")
        return value

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise WireReplyError("duplicate key")
        return dict(pairs)

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            parse_float=finite_float,
            object_pairs_hook=reject_duplicates,
        )
    except WireReplyError:
        raise
    except RecursionError:
        raise WireReplyError("nested too deeply") from None
    except ValueError:  # JSONDecodeError, or an integer literal past the interpreter's digit limit
        raise WireReplyError("not JSON") from None


@dataclass(frozen=True)
class WireSpec:
    """The schema to send and the decoder for what comes back."""

    schema: Dict[str, Any]
    _decode: _Decoder

    def to_native_json(self, text: str) -> str:
        """Strict-parse a reply, undo the wire encodings, and return JSON for the real model."""

        native = self._decode(_strict_loads(text))
        try:
            return json.dumps(native, allow_nan=False)
        except (ValueError, RecursionError):
            raise WireReplyError("reply cannot be re-encoded as JSON") from None


class _Builder:
    def __init__(self, root: Dict[str, Any]) -> None:
        self.defs: Dict[str, Any] = root.get("$defs", {})
        self._active: list[str] = []

    def convert(self, node: Dict[str, Any], where: str) -> Tuple[Dict[str, Any], _Decoder]:
        ref = node.get("$ref")
        if ref is None:
            return self._convert(node, where)
        # Inline the target. Sibling keys (a description) override the target's.
        prefix = "#/$defs/"
        if not isinstance(ref, str) or not ref.startswith(prefix) or ref[len(prefix):] not in self.defs:
            raise WireSchemaError(f"{where}: unresolvable $ref {ref!r}")
        name = ref[len(prefix):]
        if name in self._active:
            raise WireSchemaError(f"{where}: recursive schema through {name!r}")
        merged = {**self.defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
        self._active.append(name)
        try:
            return self._convert(merged, where)
        finally:
            self._active.pop()

    def _convert(self, node: Dict[str, Any], where: str) -> Tuple[Dict[str, Any], _Decoder]:
        unknown = set(node) - _DROPPED - _KEPT
        if unknown:
            raise WireSchemaError(f"{where}: keyword {sorted(unknown)} is outside the strict subset")
        if "anyOf" in node:
            return self._any_of(node, where)
        kind = node.get("type")
        if kind == "object":
            return self._object(node, where)
        if kind == "array":
            return self._array(node, where)
        if kind in _SCALARS:
            out = {k: v for k, v in node.items() if k in {"type", "enum", "description"}}
            return out, _identity
        raise WireSchemaError(f"{where}: no usable type ({kind!r})")

    def _any_of(self, node: Dict[str, Any], where: str) -> Tuple[Dict[str, Any], _Decoder]:
        branches = node["anyOf"]
        non_null = [b for b in branches if b.get("type") != "null"]
        has_null = len(non_null) != len(branches)
        if len(non_null) != 1:
            raise WireSchemaError(f"{where}: anyOf must be one type, optionally with null")
        inner, decode = self.convert(non_null[0], where)
        out: Dict[str, Any] = {"anyOf": [inner, {"type": "null"}] if has_null else [inner]}
        if "description" in node:
            out["description"] = node["description"]
        if not has_null:
            return out, decode
        return out, lambda value: None if value is None else decode(value)

    def _array(self, node: Dict[str, Any], where: str) -> Tuple[Dict[str, Any], _Decoder]:
        if "items" not in node:
            raise WireSchemaError(f"{where}: array without items")
        items, decode_item = self.convert(node["items"], where + "[]")
        out: Dict[str, Any] = {"type": "array", "items": items}
        if "description" in node:
            out["description"] = node["description"]

        def decode(value: Any) -> Any:
            if not isinstance(value, list):
                raise _fail(f"{where}: expected an array")
            return [decode_item(v) for v in value]

        return out, decode

    def _object(self, node: Dict[str, Any], where: str) -> Tuple[Dict[str, Any], _Decoder]:
        properties = node.get("properties")
        if not properties:
            raise WireSchemaError(f"{where}: open object has no strict-mode form")
        required = set(node.get("required", []))
        wire_props: Dict[str, Any] = {}
        plan: Dict[str, Tuple[str, bool, _Decoder]] = {}  # wire name -> (native name, optional, decoder)
        for name, prop in properties.items():
            prop = dict(prop)
            optional = name not in required
            encoding = prop.pop(ENCODING_KEY, None)
            wire_name = name
            if encoding is None:
                wire, decode = self.convert(prop, f"{where}.{name}")
            elif encoding == ENCODING_JSON_STRING:
                if prop.get("type") != "object" or prop.get("properties"):
                    raise WireSchemaError(f"{where}.{name}: {ENCODING_KEY} is for open objects only")
                wire_name = f"{name}_json"
                note = "A JSON object, written as a string."
                described = prop.get("description")
                wire = {"type": "string", "description": f"{described}. {note}" if described else note}
                decode = _json_object_from_string(f"{where}.{name}")
            else:
                raise WireSchemaError(f"{where}.{name}: unknown {ENCODING_KEY} {encoding!r}")
            if wire_name in wire_props:
                raise WireSchemaError(f"{where}: wire name {wire_name!r} collides")
            if optional and not _is_nullable(wire):
                wire = _nullable(wire)
            wire_props[wire_name] = wire
            plan[wire_name] = (name, optional, decode)
        out: Dict[str, Any] = {
            "type": "object",
            "properties": wire_props,
            "required": list(wire_props),
            "additionalProperties": False,
        }
        if "description" in node:
            out["description"] = node["description"]

        def decode_object(value: Any) -> Any:
            if not isinstance(value, dict):
                raise _fail(f"{where}: expected an object")
            if set(value) != set(plan):
                raise _fail(f"{where}: keys differ from the schema")
            native: Dict[str, Any] = {}
            for wire_name, (name, optional, decode) in plan.items():
                item = value[wire_name]
                if item is None and optional:
                    continue  # null means "use the default"
                native[name] = decode(item)
            return native

        return out, decode_object


def _json_object_from_string(where: str) -> _Decoder:
    def decode(value: Any) -> Any:
        if not isinstance(value, str):
            raise _fail(f"{where}: expected a JSON string")
        parsed = _strict_loads(value)
        if not isinstance(parsed, dict):
            raise _fail(f"{where}: the string must hold a JSON object")
        return parsed

    return decode


def _is_nullable(wire: Dict[str, Any]) -> bool:
    return any(b.get("type") == "null" for b in wire.get("anyOf", []))


def _nullable(wire: Dict[str, Any]) -> Dict[str, Any]:
    description = wire.get("description")
    inner = {k: v for k, v in wire.items() if k != "description"}
    out: Dict[str, Any] = {"anyOf": [inner, {"type": "null"}]}
    if description is not None:
        out["description"] = description
    return out


@lru_cache(maxsize=None)
def wire_spec(model: type[BaseModel]) -> WireSpec:
    """The strict wire schema for ``model`` and the decoder for its replies."""

    raw = model.model_json_schema()
    builder = _Builder(raw)
    root = {k: v for k, v in raw.items() if k != "$defs"}
    if root.get("type") != "object":
        raise WireSchemaError("the root of a strict schema must be an object")
    schema, decode = builder.convert(root, model.__name__)
    return WireSpec(schema=schema, _decode=decode)
