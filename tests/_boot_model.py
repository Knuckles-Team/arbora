"""A loopback OpenAI-compatible chat model for the real-boot test.

``python _boot_model.py <port>`` answers ``POST /v1/chat/completions`` with one
fixed assistant reply, plain or streamed (server-sent events), so an agent run
through the booted stack completes without a real model endpoint.
"""

from __future__ import annotations

import http.server
import json
import sys
import time

REPLY = "Arbora boot reply: see https://example.org/arbora-boot for details."
MODEL = "boot-model"


def _completion() -> dict:
    return {
        "id": "chatcmpl-boot",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _chunks() -> list[dict]:
    base = {
        "id": "chatcmpl-boot",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
    }
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}).encode()
        self._send(200, "application/json", body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if not request.get("stream"):
            self._send(200, "application/json", json.dumps(_completion()).encode())
            return
        frames = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in _chunks())
        self._send(200, "text/event-stream", (frames + "data: [DONE]\n\n").encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), _Handler).serve_forever()
