"""Pure port-allocation regressions: no sockets or native processes are started."""

from __future__ import annotations

from typing import Any

import pytest

from tests import test_real_boot as boot


class _RecyclingSockets:
    """Immediately reuse released ports, exposing sequential allocation bugs."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.sockets: list[_Socket] = []
        self.bound: set[int] = set()
        self.simultaneous: list[int] = []

    def __call__(self, family: int, kind: int) -> _Socket:
        assert (family, kind) == (boot.socket.AF_INET, boot.socket.SOCK_STREAM)
        sock = _Socket(self, len(self.sockets))
        self.sockets.append(sock)
        return sock


class _Socket:
    def __init__(self, owner: _RecyclingSockets, index: int) -> None:
        self.owner = owner
        self.index = index
        self.port: int | None = None
        self.closed = False

    def __enter__(self) -> _Socket:
        return self

    def __exit__(self, *_: Any) -> None:
        self.closed = True
        if self.port is not None:
            self.owner.bound.remove(self.port)

    def bind(self, address: tuple[str, int]) -> None:
        assert address == ("127.0.0.1", 0)
        if self.index == self.owner.fail_at:
            raise OSError("fixture bind failed")
        self.port = next(
            port for port in range(41000, 41010) if port not in self.owner.bound
        )
        self.owner.bound.add(self.port)
        self.owner.simultaneous.append(len(self.owner.bound))

    def getsockname(self) -> tuple[str, int]:
        assert self.port is not None
        return "127.0.0.1", self.port


def test_ports_are_distinct_and_reservations_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = _RecyclingSockets()
    monkeypatch.setattr(boot.socket, "socket", sockets)

    assert boot._free_ports(3) == (41000, 41001, 41002)
    assert sockets.simultaneous == [1, 2, 3]
    assert not sockets.bound
    assert all(sock.closed for sock in sockets.sockets)


def test_bind_failure_closes_every_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets = _RecyclingSockets(fail_at=2)
    monkeypatch.setattr(boot.socket, "socket", sockets)

    with pytest.raises(OSError, match="^fixture bind failed$"):
        boot._free_ports(3)
    assert len(sockets.sockets) == 3
    assert not sockets.bound
    assert all(sock.closed for sock in sockets.sockets)


def test_single_port_keeps_existing_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    sockets = _RecyclingSockets()
    monkeypatch.setattr(boot.socket, "socket", sockets)

    assert boot._free_port() == 41000
    assert len(sockets.sockets) == 1
    assert sockets.sockets[0].closed
