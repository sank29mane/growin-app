"""Test-only OpenAI-compatible stub server (stdlib, loopback, ephemeral port).

Answers ``POST /{prefix}/v1/chat/completions`` in plain JSON or SSE and records
the path, headers and JSON body of every request. One server stands in for xAI,
LM Studio and Ollama: the test chooses the prefix per provider.

Structured outputs, three shapes:

* the registry's ``run_structured``: no ``response_format``. The last user message
  ends with the fixed instruction and the JSON text of the wire schema; the stub
  finds that schema (``embedded_schema``), names it by its property set
  (``_SCHEMA_NAMES``) and answers with plain content taken from
  ``structured[schema_name]``. A dict is sent as JSON; a string is sent verbatim,
  so a test can send JSON text with fences, prose, or anything else a model might
  emit. ``finish_reason``, ``reasoning_tokens`` and ``reasoning_content`` set what
  the reply reports about how the model got there.
* ``response_format`` json_schema: same lookup by ``json_schema.name``. No
  production caller sends it now; the shape stays for the wire-schema tests.
* a forced tool (magentic): the stub answers with a tool call whose arguments
  come from ``tool_arguments[function_name]``.

The stub enforces OpenAI's strict ``json_schema`` subset: a request that sends
``strict: true`` with a schema outside it gets a 400, the way OpenAI and Azure
answer. ``strict_subset_violations`` is the checker, written independently of the
production builder.

``served_model`` makes every reply name that model instead of the requested one,
the way LM Studio answers an unknown id with its loaded model. ``omit_model`` leaves
the ``model`` field out of every reply and stream chunk.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

_PATH = re.compile(r"^/(?P<prefix>[^/]+)/v1/chat/completions$")


SCHEMA_MARKER = "JSON schema:\n"

# Property set of each wire schema the tests use -> its name in ``structured``.
_SCHEMA_NAMES = {
    frozenset({"status", "confidence_score", "risk_assessment", "compliance_notes",
               "recommendation_adjustment", "debate_refutation", "requires_hitl"}): "RiskAssessment",
    frozenset({"tool_calls"}): "ToolCallList",
    frozenset({"q", "qInTitle", "country", "category", "domain"}): "NewsDataQueryParams",
    frozenset({"text"}): "Answer",
}


def last_user_text(body: Any) -> str:
    """The text of the last user message of a request body ('' when there is none)."""
    messages = body.get("messages") if isinstance(body, dict) else None
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            return content if isinstance(content, str) else ""
    return ""


def embedded_schema(body: Any) -> Optional[Dict[str, Any]]:
    """The wire schema ``run_structured`` appended to the last user message, or None."""
    text = last_user_text(body)
    head, marker, tail = text.rpartition(SCHEMA_MARKER)
    if not marker:
        return None
    try:
        schema = json.loads(tail)
    except ValueError:
        return None
    return schema if isinstance(schema, dict) else None


def embedded_schema_name(body: Any) -> Optional[str]:
    schema = embedded_schema(body)
    props = schema.get("properties") if isinstance(schema, dict) else None
    return _SCHEMA_NAMES.get(frozenset(props)) if isinstance(props, dict) else None


@dataclass
class RecordedRequest:
    method: str
    path: str
    prefix: str
    headers: Dict[str, str]
    json: Any

    @property
    def model(self) -> Optional[str]:
        return self.json.get("model") if isinstance(self.json, dict) else None


_STRICT_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "anyOf", "description", "title", "$ref", "$defs",
}


def strict_subset_violations(schema: Any, path: str = "$", root: Any = None) -> List[str]:
    """Why ``schema`` is outside OpenAI's strict structured-output subset (empty if inside)."""

    root = schema if root is None else root
    found: List[str] = []
    if path == "$" and not (isinstance(schema, dict) and schema.get("type") == "object"):
        found.append(f"{path}: the root must be an object")
    if not isinstance(schema, dict):
        return found + [f"{path}: schema node is not an object"]
    for keyword in schema:
        if keyword not in _STRICT_KEYWORDS:
            found.append(f"{path}: keyword {keyword!r} is not supported in strict mode")
    if "$ref" in schema:
        name = str(schema["$ref"]).rpartition("/")[2]
        target = (root.get("$defs") or {}).get(name) if isinstance(root, dict) else None
        if target is None:
            found.append(f"{path}: unresolvable $ref")
        return found
    if "anyOf" in schema:
        for i, branch in enumerate(schema["anyOf"]):
            found += strict_subset_violations(branch, f"{path}.anyOf[{i}]", root)
        return found
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            found.append(f"{path}: an open object (no properties) is not supported")
            props = {}
        if schema.get("additionalProperties") is not False:
            found.append(f"{path}: additionalProperties must be false")
        if set(schema.get("required", [])) != set(props):
            missing = sorted(set(props) - set(schema.get("required", [])))
            found.append(f"{path}: every property must be required (not required: {missing})")
        for name, sub in props.items():
            found += strict_subset_violations(sub, f"{path}.{name}", root)
    elif kind == "array":
        if "items" not in schema:
            found.append(f"{path}: an array needs items")
        else:
            found += strict_subset_violations(schema["items"], f"{path}[]", root)
    elif kind is None and "enum" not in schema:
        found.append(f"{path}: no type")
    return found


_UNSET: Any = object()


@dataclass
class ModelStubServer:
    reply_text: str = "stub reply"
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
    structured: Dict[str, Any] = field(default_factory=dict)
    served_model: Optional[str] = None
    omit_model: bool = False
    finish_reason: str = "stop"
    # What the reply reports about reasoning. None leaves the field out, the way
    # a runtime that does not count reasoning tokens does.
    reasoning_tokens: Optional[int] = 7
    # Verbatim wire value for reasoning_tokens (a string, a bool, null...). Overrides
    # ``reasoning_tokens`` when set; ``_UNSET`` leaves ``reasoning_tokens`` in charge.
    reasoning_tokens_raw: Any = _UNSET
    reasoning_content: Optional[str] = None
    reasoning_field: str = "reasoning_content"  # or "reasoning", the other name runtimes use
    requests: List[RecordedRequest] = field(default_factory=list)
    _server: Optional[ThreadingHTTPServer] = None
    _thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ModelStubServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # silence stderr
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                match = _PATH.match(self.path)
                record = RecordedRequest(
                    method="POST",
                    path=self.path,
                    prefix=match.group("prefix") if match else "",
                    headers={k.lower(): v for k, v in self.headers.items()},
                    json=body,
                )
                with stub._lock:
                    stub.requests.append(record)
                if match is None or not isinstance(body, dict):
                    self._send_json(404, {"error": {"message": "not found"}})
                    return
                stub._respond(self, body)

            def _send_json(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- addressing --------------------------------------------------------

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def base_url(self, prefix: str) -> str:
        return f"{self.url}/{prefix}/v1"

    # -- recording ---------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def snapshot(self) -> List[RecordedRequest]:
        with self._lock:
            return list(self.requests)

    # -- responses ---------------------------------------------------------

    def _forced_tool(self, body: Dict[str, Any]) -> Optional[str]:
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            function = choice.get("function")
            if isinstance(function, dict):
                return function.get("name")
        return None

    def _json_schema_spec(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            spec = fmt.get("json_schema")
            if isinstance(spec, dict):
                return spec
        return None

    def _schema_name(self, body: Dict[str, Any]) -> Optional[str]:
        fmt = body.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            spec = fmt.get("json_schema")
            if isinstance(spec, dict):
                return spec.get("name")
        return embedded_schema_name(body)

    def _respond(self, handler: BaseHTTPRequestHandler, body: Dict[str, Any]) -> None:
        model: Optional[str] = self.served_model if self.served_model is not None else body.get("model", "")
        if self.omit_model:
            model = None
        tool_name = self._forced_tool(body)
        arguments = json.dumps(self.tool_arguments.get(tool_name, {})) if tool_name else None
        if body.get("stream"):
            self._respond_sse(handler, body, model, tool_name, arguments)
            return
        content = self.reply_text
        spec = self._json_schema_spec(body)
        if spec is not None and spec.get("strict") is True:
            violations = strict_subset_violations(spec.get("schema"))
            if violations:
                handler._send_json(
                    400,
                    {"error": {"message": "Invalid schema for response_format: " + "; ".join(violations),
                               "type": "invalid_request_error", "param": "response_format"}},
                )
                return
        schema_name = self._schema_name(body)
        if schema_name is not None:
            reply = self.structured.get(schema_name, {})
            content = reply if isinstance(reply, str) else json.dumps(reply)
        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if self.reasoning_content is not None:
            message[self.reasoning_field] = self.reasoning_content
        finish = self.finish_reason
        if tool_name:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_stub",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    }
                ],
            }
            finish = "tool_calls"
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            **({} if model is None else {"model": model}),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                **(
                    {"completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens_raw}}
                    if self.reasoning_tokens_raw is not _UNSET
                    else {}
                    if self.reasoning_tokens is None
                    else {"completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens}}
                ),
            },
        }
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _respond_sse(
        self,
        handler: BaseHTTPRequestHandler,
        body: Dict[str, Any],
        model: Optional[str],
        tool_name: Optional[str],
        arguments: Optional[str],
    ) -> None:
        def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> Dict[str, Any]:
            return {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 0,
                **({} if model is None else {"model": model}),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        events: List[Any] = []
        if tool_name:
            events.append(chunk({"role": "assistant", "content": None}))
            events.append(
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_stub",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": ""},
                            }
                        ]
                    }
                )
            )
            events.append(
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]})
            )
            events.append(chunk({}, "tool_calls"))
        else:
            events.append(chunk({"role": "assistant", "content": ""}))
            events.append(chunk({"content": self.reply_text}))
            events.append(chunk({}, "stop"))
        options = body.get("stream_options")
        if isinstance(options, dict) and options.get("include_usage"):
            events.append(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    **({} if model is None else {"model": model}),
                    "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )
        payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        data = payload.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
