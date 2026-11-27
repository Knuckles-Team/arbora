"""Recorded ports for the meeting-summary tests."""

from __future__ import annotations

from typing import Any

from tests.api._engine_double import context as engine_context

PARAMS = {"meeting_id": "m-1", "provider": "prov", "model": "mod"}
#: The summary request of ARBORA-OPS-R065.1 against the receipt ``Final`` holds.
REQUEST = {
    "schema_version": 1,
    "meeting_id": "m-1",
    "transcript_revision": 9,
    "provider_id": "prov",
    "context_revision": "ctx-3",
}


class Admission:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls = 0

    async def admit(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return self.answer


class Transcript:
    async def read(self, **_: Any) -> dict[str, Any]:
        return {
            "transcript_digest": "t" * 64,
            "segments": [{"segment_id": "s1", "text": "hello"}],
        }


class Summarizer:
    def __init__(self, timeouts: int = 0) -> None:
        self.timeouts = timeouts
        self.calls = 0

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.timeouts:
            raise TimeoutError
        return {"claims": [], "receipt": {"receipt_id": "r1"}}


class Final:
    def __init__(self, digest: str = "f1", **receipt: Any) -> None:
        self.digest = digest
        self.receipt = {
            "transcript_revision": 9,
            "context_revision": "ctx-3",
            **receipt,
        }

    async def get_final(self, **_: Any) -> dict[str, Any]:
        return {"finalization_digest": self.digest, **self.receipt}


class Catalog:
    """The fleet catalog port: items that declare a capability."""

    def __init__(self, *entries: tuple[str, dict[str, Any]]) -> None:
        self.entries = entries

    async def with_capability(self, caller: Any, capability: str) -> tuple[Any, ...]:
        from types import SimpleNamespace

        return tuple(
            SimpleNamespace(id=item_id, annotations=note)
            for item_id, note in sorted(self.entries)
            if note.get("capability") == capability
        )


def gateway(*entries: tuple[str, dict[str, Any]]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(catalog=Catalog(*entries))


def provider(execution_class: str = "hosted", model_id: str = "mod") -> dict[str, Any]:
    return {
        "capability": "meeting_summary",
        "execution_class": execution_class,
        "model_id": model_id,
    }


def meeting_context(admission: Admission, **services: Any) -> Any:
    context = engine_context()
    context.caller.principal = "principal:one"
    context.services = {
        "meeting_provider_admission": admission,
        "meeting_transcript": Transcript(),
        "meeting_summarizer": services.pop("summarizer", Summarizer()),
        "meeting_final": services.pop("meeting_final", Final()),
        "fleet_gateway": services.pop("fleet_gateway", gateway(("prov", provider()))),
        **services,
    }
    return context


def install_engine(monkeypatch: Any, *modules: Any) -> list[str]:
    calls: list[str] = []

    async def send(context: Any, method: str, params: dict[str, Any], **_: Any) -> Any:
        calls.append(method)
        return (
            {"citations": [], "context_digest": "c" * 64}
            if "Context" in method
            else {"claim_count": 0}
        )

    for module in modules:
        monkeypatch.setattr(module, "engine_send", send)
    return calls
