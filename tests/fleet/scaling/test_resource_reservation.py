"""Public-surface behaviour of the engine-native reservation transport."""

from __future__ import annotations

from typing import Any

import pytest

from graph_os.fleet.scaling.resource_reservation import (
    EngineNativeReservationTransport,
    NativeReservationUnavailable,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.755")
Request = dict[str, Any]


class WorkItems:
    """Generated-client namespace fake that echoes the method it served."""

    def __init__(self, reservations: int = 0, *, is_async: bool = False) -> None:
        self.calls: list[str] = []
        self.reservations = reservations
        self.is_async = is_async

    def _serve(self, name: str) -> Any:
        self.calls.append(name)
        result = {"decision": name, "reservations": [{}] * self.reservations}
        if self.is_async:

            async def pending() -> Request:
                return result

            return pending()
        return result

    def reserve(self, *, request: Request) -> Any:
        return self._serve("reserve")

    def release(self, *, request: Request) -> Any:
        return self._serve("release")

    def reclaim(self, *, request: Request) -> Any:
        return self._serve("reclaim")

    def query_reservation(self, *, request: Request) -> Any:
        return self._serve("query")

    def status(self, *, request: Request) -> Any:
        return self._serve("status")

    def update_host(self, *, request: Request) -> Any:
        return self._serve("update_host")


class Client:
    def __init__(self, work_items: WorkItems, *, advertised: bool = True) -> None:
        self.work_items = work_items
        self.advertised = advertised

    def supports(self, operation: str) -> bool:
        return self.advertised


def test_each_operation_maps_to_its_typed_native_method() -> None:
    items = WorkItems()
    transport = EngineNativeReservationTransport(Client(items))
    for operation in ("reserve", "release", "reclaim", "query", "update_host"):
        assert getattr(transport, operation)({"now_ms": 1})["decision"]
    transport.status({"limit": 1})
    assert items.calls == [
        "reserve",
        "release",
        "reclaim",
        "query",
        "update_host",
        "status",
    ]


@pytest.mark.parametrize(
    ("client", "error", "match"),
    [
        (object(), NativeReservationUnavailable, None),
        (Client(WorkItems(), advertised=False), NativeReservationUnavailable, None),
        (Client(WorkItems(is_async=True)), TypeError, "sync generated client"),
        (Client(WorkItems(reservations=2)), ValueError, "exceeded requested limit"),
    ],
)
def test_transport_fails_closed(
    client: object, error: type[Exception], match: str | None
) -> None:
    with pytest.raises(error, match=match):
        EngineNativeReservationTransport(client).status({"limit": 1})
    if isinstance(client, Client) and not client.advertised:
        assert client.work_items.calls == []
