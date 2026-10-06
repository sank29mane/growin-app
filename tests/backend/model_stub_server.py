"""Test-only OpenAI-compatible stub server (stdlib, loopback, ephemeral port).

Answers ``POST /{prefix}/v1/chat/completions`` in plain JSON or SSE and records
the path, headers and JSON body of every request. One server stands in for xAI,
LM Studio and Ollama: the test chooses the prefix per provider.

Structured outputs: when a request forces a tool (magentic does this for every
pydantic return type) the stub answers with a tool call whose arguments come
from ``tool_arguments[function_name]``.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

_PATH = re.compile(r"^/(?P<prefix>[^/]+)/v1/chat/completions$")


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


@dataclass
class ModelStubServer:
    reply_text: str = "stub reply"
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
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

    def _respond(self, handler: BaseHTTPRequestHandler, body: Dict[str, Any]) -> None:
        model = body.get("model", "")
        tool_name = self._forced_tool(body)
        arguments = json.dumps(self.tool_arguments.get(tool_name, {})) if tool_name else None
        if body.get("stream"):
            self._respond_sse(handler, body, model, tool_name, arguments)
            return
        message: Dict[str, Any] = {"role": "assistant", "content": self.reply_text}
        finish = "stop"
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
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
        model: str,
        tool_name: Optional[str],
        arguments: Optional[str],
    ) -> None:
        def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> Dict[str, Any]:
            return {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
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
                    "model": model,
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
