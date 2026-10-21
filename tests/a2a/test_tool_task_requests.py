"""Native request transport bindings; these fixtures grant no execution."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a import tool_task_requests
from arbora.a2a.tool_task import ToolTaskRefused, tool_task_work_item_id
from arbora.a2a.tool_task_requests import EgToolTaskRequestStore
from tests.a2a.test_tool_task import REQUEST

pytestmark = pytest.mark.spec("ARBORA-A2A-R002.2.2.3.1")


class Blob:
    def __init__(self) -> None:
        self.raw = b""
        self.holders = 0
        self.closed = 0
        self.count = 1

    async def store(self, raw: bytes, *, chunk_size: int) -> str:
        assert chunk_size == 1 << 20
        self.raw = raw
        return "b" * 64

    async def incref(self, digest: str) -> int:
        self.holders += 1
        return self.holders

    async def fetch_begin(self, digest: str) -> tuple[int, int]:
        assert digest == "b" * 64
        return 1, self.count

    async def chunk_get(self, cursor: int, index: int) -> bytes:
        return self.raw

    async def fetch_end(self, cursor: int) -> bool:
        self.closed += 1
        return True


class Fixture:
    def __init__(self) -> None:
        self.blob = Blob()
        self.item: dict[str, Any] | None = None
        self.principal = REQUEST.principal
        self.tenant = REQUEST.tenant
        self.engine = SimpleNamespace(
            graph_compute=SimpleNamespace(
                for_graph=lambda _graph: SimpleNamespace(
                    async_client=SimpleNamespace(blob=self.blob)
                )
            )
        )
        self.store = EgToolTaskRequestStore(self.engine)

    def session(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            tenant=self.tenant, actor=SimpleNamespace(actor_id=self.principal)
        )

    def submit(self, _engine: Any, **kwargs: Any) -> tuple[str, bool]:
        self.item = {
            **kwargs,
            "input_ref": kwargs["payload_ref"],
        }
        assert self.blob.holders == 1
        assert "parameters_json" not in str(kwargs["metadata"])
        return kwargs["work_item_id"], True

    def read(self, **changes: Any) -> Any:
        kwargs = {
            "job_id": "job",
            "tenant": REQUEST.tenant,
            "session_id": REQUEST.session_id,
            **changes,
        }
        return self.store.resolve(REQUEST.request_ref, **kwargs)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Fixture:
    fixture = Fixture()
    monkeypatch.setattr(tool_task_requests, "resolve_session", fixture.session)
    monkeypatch.setattr(
        tool_task_requests.work_items, "submit_work_item_atomic", fixture.submit
    )
    monkeypatch.setattr(
        tool_task_requests.work_items, "get_work_item", lambda *_: fixture.item
    )
    return fixture


def test_store_then_new_adapter_resolves_only_native_bytes(transport: Fixture) -> None:
    carrier = transport.store.store("job", REQUEST)
    assert carrier.payload_ref == REQUEST.request_ref
    transport.store = EgToolTaskRequestStore(transport.engine)
    assert transport.read() == REQUEST
    assert transport.blob.closed == 2


@pytest.mark.parametrize("field", ["principal", "tenant"])
def test_foreign_caller_cannot_publish(transport: Fixture, field: str) -> None:
    setattr(transport, field, "foreign")
    with pytest.raises(ToolTaskRefused):
        transport.store.store("job", REQUEST)
    assert transport.blob.raw == b""
    assert transport.item is None


@pytest.mark.parametrize(
    "changes",
    [
        {"work_item_id": tool_task_work_item_id("foreign")},
        {"kind": "other"},
        {"input_ref": "tool-task:sha256:" + "c" * 64},
        {"metadata": {}},
    ],
)
def test_locator_cannot_substitute_another_request(
    transport: Fixture, changes: dict[str, Any]
) -> None:
    transport.store.store("job", REQUEST)
    assert transport.item is not None
    transport.item.update(changes)
    with pytest.raises(ToolTaskRefused):
        transport.read()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"x" * 80_001,
        REQUEST.model_copy(update={"authority_ref": "changed"})
        .model_dump_json()
        .encode(),
    ],
)
def test_changed_or_excessive_bytes_refuse_and_close_cursor(
    transport: Fixture, raw: bytes
) -> None:
    transport.store.store("job", REQUEST)
    transport.blob.raw = raw
    with pytest.raises(ToolTaskRefused):
        transport.read()
    assert transport.blob.closed == 2


def test_excessive_chunk_count_never_fetches_chunks(transport: Fixture) -> None:
    transport.store.store("job", REQUEST)
    transport.blob.count = 999_999
    with pytest.raises(ToolTaskRefused, match="too_large"):
        transport.read()
    assert transport.blob.closed == 2


def test_changed_caller_or_session_cannot_read(transport: Fixture) -> None:
    transport.store.store("job", REQUEST)
    with pytest.raises(ToolTaskRefused):
        transport.read(session_id="foreign")
    transport.principal = "foreign"
    with pytest.raises(ToolTaskRefused):
        transport.read()
