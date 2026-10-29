"""A loopback OpenAI-compatible chat model for the real-boot test.

``python _boot_model.py <port>`` answers ``POST /v1/chat/completions`` with one
fixed assistant reply, plain or streamed (server-sent events), so an agent run
through the booted stack completes without a real model endpoint.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import sys
import time
from pathlib import Path

REPLY = "Arbora boot reply: see https://example.org/arbora-boot for details."
MODEL = "boot-model"
MAX_ASTRAL_TRIGGER = "__arbora_test_max_astral_reply__"
OVERSIZED_EVENT_TRIGGER = "__arbora_test_oversized_terminal_event__"
MAX_ASTRAL_REPLY = "😀" * 65_536
OVERSIZED_EVENT_REPLY = "😀" * 100_000
HISTORY_FIRST = "__arbora_history_first__"
HISTORY_SECOND = "__arbora_history_second__"


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _history_answer(request: dict) -> tuple[str, dict | None]:
    """Control only model output; native planning, claims and effects stay real."""
    messages = request.get("messages", [])
    users = [_message_text(row) for row in messages if row.get("role") == "user"]
    latest = users[-1] if users else ""
    if any(
        "Return only a JSON object with tasks and output_task_id" in _message_text(row)
        for row in messages
        if row.get("role") in {"system", "developer"}
    ):
        return json.dumps(
            {
                "tasks": [{"task_id": "answer", "prompt": latest, "depends_on": []}],
                "output_task_id": "answer",
            }
        ), None
    if HISTORY_FIRST in latest:
        if any(
            row.get("role") == "tool"
            and row.get("tool_call_id") == "history-effect-call"
            for row in messages
        ):
            return "history-first-reply " + latest, None
        tools = {
            row.get("function", {}).get("name") for row in request.get("tools", [])
        }
        if "act" not in tools:
            raise ValueError("history acceptance requires the real admitted act tool")
        nonce = re.findall(r"proof-[0-9a-f]{32}", latest)[-1]
        return "", {
            "id": "history-effect-call",
            "type": "function",
            "function": {
                "name": "act",
                "arguments": json.dumps(
                    {
                        "op": "fleet.call",
                        "params": {
                            "server": "boot-echo",
                            "tool": "echo",
                            "arguments": {"text": nonce},
                        },
                        "execute": True,
                        "idempotency_key": "history-effect-" + nonce,
                    }
                ),
            },
        }
    if HISTORY_SECOND in latest:
        return "history-second-reply " + latest, None
    raise ValueError("unexpected prompt in isolated history acceptance model")


def _completion(reply: str = REPLY) -> dict:
    return {
        "id": "chatcmpl-boot",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _chunks(reply: str = REPLY) -> list[dict]:
    base = {
        "id": "chatcmpl-boot",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
    }
    return [
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": reply},
                    "finish_reason": None,
                }
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = json.dumps(
            {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
        ).encode()
        self._send(200, "application/json", body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = json.dumps(request.get("messages", []))
        nonces = re.findall(r"proof-[0-9a-f]{32}", messages)
        tool_call = None
        # Only explicit synthetic test prompts select a large response.
        if os.getenv("BOOT_HISTORY_ACCEPTANCE") == "1":
            reply, tool_call = _history_answer(request)
        elif MAX_ASTRAL_TRIGGER in messages:
            reply = MAX_ASTRAL_REPLY
        elif OVERSIZED_EVENT_TRIGGER in messages:
            reply = OVERSIZED_EVENT_REPLY
        else:
            reply = REPLY + (" " + nonces[-1] if nonces else "")
        if receipt_path := os.getenv("BOOT_MODEL_RECEIPTS"):
            with Path(receipt_path).open("a") as receipts:
                receipts.write(
                    json.dumps(
                        {
                            "nonce": nonces[-1] if nonces else None,
                            "reply": reply,
                            **(
                                {
                                    "messages": request.get("messages", []),
                                    "tool_call": tool_call,
                                }
                                if os.getenv("BOOT_HISTORY_ACCEPTANCE") == "1"
                                else {}
                            ),
                        }
                    )
                    + "\n"
                )
        if not request.get("stream"):
            completed = _completion(reply)
            if tool_call is not None:
                completed["choices"][0].update(
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call],
                        },
                        "finish_reason": "tool_calls",
                    }
                )
            self._send(200, "application/json", json.dumps(completed).encode())
            return
        chunks = _chunks(reply)
        if tool_call is not None:
            chunks[0]["choices"][0]["delta"] = {
                "role": "assistant",
                "tool_calls": [{"index": 0, **tool_call}],
            }
            chunks[-1]["choices"][0]["finish_reason"] = "tool_calls"
        frames = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        self._send(200, "text/event-stream", (frames + "data: [DONE]\n\n").encode())

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(
        ("127.0.0.1", int(sys.argv[1])), _Handler
    ).serve_forever()
