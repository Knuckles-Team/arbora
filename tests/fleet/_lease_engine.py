"""A recorded stand-in for the engine's build-slot lease family, for the lease runner tests.

``FakeEngine`` keeps one one-slot cell the way the engine does: a granted or running lease
holds the slot, a full cell queues in order, and a release promotes the next queued lease.
``EngineDoor`` is the in-process door over it; ``serve`` puts the same engine behind the
served operation path over loopback HTTP.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from arbora.fleet.lease_runner import DoorRefused

HOLDING = ("granted", "running")


class FakeEngine:
    def __init__(self, slots: int = 1) -> None:
        self.slots = slots
        self.leases: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.renews: dict[str, int] = {}
        self.on_release: Callable[[str], None] | None = None
        self.revoke_at_renew: int | None = None
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def _held(self) -> int:
        return sum(1 for lease in self.leases.values() if lease["state"] in HOLDING)

    def _promote(self) -> None:
        for lease_id in self.order:
            lease = self.leases[lease_id]
            if lease["state"] == "queued" and self._held() < self.slots:
                lease["state"] = "granted"

    def _refuse(self) -> DoorRefused:
        return DoorRefused("TRANSITION_REFUSED", {"code": "TRANSITION_REFUSED"})

    def _move(
        self, params: Mapping[str, Any], allowed: tuple[str, ...], to: str
    ) -> Any:
        lease = self.leases.get(params["lease_id"])
        if lease is None or lease["state"] not in allowed:
            raise self._refuse()
        lease["state"] = to
        return lease

    def _renew(self, params: Mapping[str, Any]) -> Any:
        lease_id = params["lease_id"]
        self.renews[lease_id] = self.renews.get(lease_id, 0) + 1
        if self.revoke_at_renew == self.renews[lease_id]:
            self.leases[lease_id]["state"] = "revoked"
        return self._move(params, ("running",), "running")

    def _request(self, params: Mapping[str, Any]) -> Any:
        number = len(self.order) + 1
        lease_id = f"lease-{number}"
        state = "granted" if self._held() < self.slots else "queued"
        self.leases[lease_id] = {
            "lease_id": lease_id,
            "lease_epoch": 1,
            "fence_token": 100 + number,
            "state": state,
            "cell_id": params["cell_id"],
        }
        self.order.append(lease_id)
        return self.leases[lease_id]

    def _release(self, params: Mapping[str, Any]) -> Any:
        lease = self._move(params, ("granted", "running", "queued"), "released")
        if self.on_release is not None:
            self.on_release(params["lease_id"])
        self._promote()
        return lease

    def handle(self, op_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        verb = op_id.rsplit(".", 1)[-1]
        with self.changed:
            self.calls.append((verb, params.get("lease_id", "")))
            table = {
                "request": self._request,
                "start": lambda p: self._move(p, ("granted",), "running"),
                "renew": self._renew,
                "release": self._release,
                "get": lambda p: self.leases.get(p["lease_id"]),
            }
            lease = table[verb](params)
            if lease is None:
                raise DoorRefused("NOT_FOUND")
            self.changed.notify_all()
            queued = [i for i in self.order if self.leases[i]["state"] == "queued"]
            place = (
                queued.index(lease["lease_id"]) + 1
                if lease["state"] == "queued"
                else None
            )
            return {"lease": dict(lease), "queue_position": place}

    def wait_for(self, predicate: Callable[[FakeEngine], bool], timeout: float) -> bool:
        with self.changed:
            return self.changed.wait_for(lambda: predicate(self), timeout)


class EngineDoor:
    """The in-process door over a :class:`FakeEngine`."""

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    def call(
        self, op_id: str, params: Mapping[str, Any], *, key: str | None = None
    ) -> Mapping[str, Any]:
        return self.engine.handle(op_id, params)


def serve(engine: FakeEngine, credential: str) -> ThreadingHTTPServer:
    """The engine behind ``POST /api/v1/ops/<op>`` on loopback; the caller shuts it down."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 -- the stdlib handler's name
            size = int(self.headers.get("Content-Length") or 0)
            params = json.loads(self.rfile.read(size) or b"{}")
            op_id = self.path.rsplit("/", 1)[-1]
            if self.headers.get("Authorization") != f"Bearer {credential}":
                self._send(
                    401, {"ok": False, "error": {"code": "AUTHENTICATION_REQUIRED"}}
                )
                return
            try:
                value = engine.handle(op_id, params)
            except DoorRefused as refused:
                self._send(409, {"ok": False, "error": {"code": refused.code}})
                return
            self._send(200, {"ok": True, "result": {"value": value}, "meta": {}})

        def _send(self, status: int, body: Mapping[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
