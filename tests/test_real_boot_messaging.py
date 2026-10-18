"""Boot a real ``arbora`` process and deliver an alert to a faked Telegram API.

ARBORA-MESSAGING-R008: a monitoring source posts the alert-bridge's
``{"text": ...}`` body to ``POST /api/v1/messaging/alert`` with a bearer. The
served process admits it through the normal invoke pipeline and sends it with
the Telegram channel adapter. The adapter talks to a local Bot API double
(``TELEGRAM_API_BASE_URL``), so no message leaves the host.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from tests.test_real_boot import _booted_stack, _free_port, pytestmark

__all__ = ["pytestmark"]

BOT_TOKEN = "123456:boot-test-token"  # nosec B105 - a fake token for the double
CHAT_ID = "4242"


class _FakeBotApi(http.server.ThreadingHTTPServer):
    """Answers the Bot API methods the adapter calls and records sends."""

    def __init__(self, port: int) -> None:
        self.sent: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", port), _BotApiHandler)


class _BotApiHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeBotApi

    def log_message(self, *_: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - the stdlib handler name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        method = self.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result: Any = {
                "id": 123456,
                "is_bot": True,
                "first_name": "boot",
                "username": "boot_bot",
            }
        elif method == "sendMessage":
            fields = _form_or_json(raw, self.headers.get("Content-Type", ""))
            self.server.sent.append(fields)
            result = {
                "message_id": len(self.server.sent),
                "date": 0,
                "chat": {"id": int(fields["chat_id"]), "type": "private"},
                "text": fields.get("text", ""),
            }
        else:
            result = True
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _form_or_json(raw: str, content_type: str) -> dict[str, Any]:
    if "json" in content_type:
        return dict(json.loads(raw or "{}"))
    from urllib.parse import parse_qsl

    return dict(parse_qsl(raw))


@pytest.fixture(scope="module")
def bot_api() -> Iterator[_FakeBotApi]:
    server = _FakeBotApi(_free_port())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def stack(bot_api: _FakeBotApi) -> Iterator[dict[str, Any]]:
    host, port = str(bot_api.server_address[0]), int(bot_api.server_address[1])
    with _booted_stack(
        extra_env={"TELEGRAM_BOT_TOKEN": BOT_TOKEN},
        extra_config={
            "TELEGRAM_API_BASE_URL": f"http://{host}:{port}/bot",
            "MESSAGING_DEFAULT_PLATFORM": "telegram",
            "MESSAGING_DEFAULT_CHANNEL": CHAT_ID,
        },
    ) as booted:
        yield booted


@pytest.mark.spec("ARBORA-MESSAGING-R008")
def test_an_admitted_alert_reaches_the_telegram_double(
    stack: dict[str, Any], bot_api: _FakeBotApi
) -> None:
    import httpx

    url = f"{stack['base_url']}/api/v1/messaging/alert"
    anonymous = httpx.post(url, json={"text": "x"}, timeout=30)
    assert anonymous.status_code == 401, anonymous.text
    assert bot_api.sent == []

    response = httpx.post(
        url,
        json={"text": "[firing] boot alert"},
        headers={
            "Authorization": f"Bearer {stack['token']}",
            # A write: the caller names its retry identity.
            "Idempotency-Key": "boot-alert-1",
        },
        timeout=60,
    )
    assert response.status_code == 200, f"{response.text}\n{stack['logs']()}"
    result = response.json()["result"]
    assert result["delivered"] is True
    assert result["channel_id"] == CHAT_ID
    assert [(m["chat_id"], m["text"]) for m in bot_api.sent] == [
        (CHAT_ID, "[firing] boot alert")
    ]
