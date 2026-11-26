"""Disposable provisioner double for the provisioning rows.

The double has the shape and semantics of the scion-sdk provisioner
(``scion_sdk.provisioning.provisioner``, ``SCION-CONNECTOR-CONTROL-R906``): ``allocate``
fenced by the idempotency key, ``ALLOCATION_UNKNOWN`` on a timeout, ``reconcile`` that
reads and never allocates. When the installed wheel has the real module the suites run
against it too (``sdk`` fixture parameters); when it lags, the guard test names the symbol.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.contracts.provisioning_capacity import ProvisionRequest
from arbora.control_plane.runs.audit import AuditChain
from arbora.control_plane.runs.capacity import CapacityLedger
from arbora.fleet.provisioning import ProvisionGuards

IMAGE = "a" * 64
MATRIX = frozenset({("linux", "docker"), ("linux", "podman"), ("windows", "hyperv")})


class DoubleProvisionError(RuntimeError):
    def __init__(self, code: str, allocation_id: str = "") -> None:
        self.code = code
        self.allocation_id = allocation_id
        super().__init__(code)


@dataclass(frozen=True)
class DoubleAllocationRequest:
    idempotency_key: str
    image: str
    platform: str
    count: int = 1

    def digest(self) -> str:
        body = json.dumps(
            [self.idempotency_key, self.image, self.platform, self.count],
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class DoubleReceipt:
    allocation_id: str
    instance_ids: tuple[str, ...]
    digest: str


class DisposableAdapter:
    """Allocates disposable instances; ``mode`` picks ok, timeout or short."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.live: dict[str, tuple[str, ...]] = {}
        self.calls = 0
        self.peak = 0
        self._now = 0
        self._lock = threading.Lock()

    def supports(self, image: str, platform: str) -> bool:
        os_, _, runtime = platform.partition("/")
        return (os_, runtime) in MATRIX

    def allocate(self, allocation_id: str, request: Any) -> tuple[str, ...]:
        with self._lock:
            self.calls += 1
            count = request.count - (1 if self.mode == "short" else 0)
            ids = tuple(f"{allocation_id}-i{n}" for n in range(count))
            self.live[allocation_id] = ids
            self._now += len(ids)
            self.peak = max(self.peak, self._now)
        if self.mode == "timeout":
            raise TimeoutError
        return ids

    def instances(self, allocation_id: str) -> tuple[str, ...]:
        return self.live.get(allocation_id, ())


class DoubleProvisioner:
    def __init__(self, adapter: DisposableAdapter) -> None:
        self._adapter = adapter
        self._requests: dict[str, DoubleAllocationRequest] = {}
        self._receipts: dict[str, DoubleReceipt] = {}
        self._lock = threading.Lock()

    def allocate(self, request: DoubleAllocationRequest) -> DoubleReceipt:
        key = request.idempotency_key
        alloc = "alloc-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        with self._lock:
            prior = self._requests.get(key)
            if prior is not None:
                if prior.digest() != request.digest():
                    raise DoubleProvisionError("IDEMPOTENCY_CONFLICT")
                if key in self._receipts:
                    return self._receipts[key]
                raise DoubleProvisionError("ALLOCATION_UNKNOWN", alloc)
            if not self._adapter.supports(request.image, request.platform):
                raise DoubleProvisionError("PLATFORM_UNSUPPORTED")
            self._requests[key] = request
        try:
            ids = self._adapter.allocate(alloc, request)
        except (TimeoutError, ConnectionError):
            raise DoubleProvisionError("ALLOCATION_UNKNOWN", alloc) from None
        body = json.dumps([alloc, sorted(ids)], separators=(",", ":"))
        receipt = DoubleReceipt(alloc, ids, hashlib.sha256(body.encode()).hexdigest())
        with self._lock:
            self._receipts[key] = receipt
        return receipt

    def reconcile(self, allocation_id: str) -> DoubleReceipt:
        ids = self._adapter.instances(allocation_id)
        if not ids:
            raise DoubleProvisionError("ALLOCATION_UNKNOWN", allocation_id)
        body = json.dumps([allocation_id, sorted(ids)], separators=(",", ":"))
        return DoubleReceipt(
            allocation_id, ids, hashlib.sha256(body.encode()).hexdigest()
        )


DOUBLE_SDK = SimpleNamespace(
    AllocationRequest=DoubleAllocationRequest,
    ProvisionError=DoubleProvisionError,
    Provisioner=DoubleProvisioner,
)


def real_sdk() -> Any | None:
    """The installed SDK provisioner module, or ``None`` while the wheel lags main."""
    try:
        return importlib.import_module("scion_sdk.provisioning.provisioner")
    except ImportError:
        return None


def make_provisioner(sdk: Any, adapter: DisposableAdapter) -> Any:
    if sdk is DOUBLE_SDK:
        return DoubleProvisioner(adapter)
    return sdk.Provisioner(adapter, sdk.MemoryAllocationStore())


def request(key: str = "key-1", count: int = 3, **update: Any) -> ProvisionRequest:
    body: dict[str, Any] = {
        "image_digest": IMAGE,
        "os": "linux",
        "runtime": "docker",
        "tenant": "tenant:one",
        "instance_count": count,
        "budget": 500,
        "idempotency_key": key,
    }
    return ProvisionRequest.model_validate({**body, **update})


def guards(
    *,
    grant: bool = True,
    fresh: bool = True,
    matrix: frozenset[tuple[str, str]] = MATRIX,
) -> ProvisionGuards:
    return ProvisionGuards(
        matrix=matrix, grant=lambda _r: grant, decision_fresh=lambda: fresh
    )


def new_run(total: int = 200) -> tuple[CapacityLedger, AuditChain]:
    return CapacityLedger(total), AuditChain(max_events=4096)


def _sdk_params() -> list[Any]:
    found = real_sdk()
    return [DOUBLE_SDK] if found is None else [DOUBLE_SDK, found]


@pytest.fixture(
    params=_sdk_params(), ids=lambda s: "double" if s is DOUBLE_SDK else "sdk"
)
def sdk(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The provisioner module under test; the double stands in while the wheel lags."""
    if request.param is DOUBLE_SDK:
        monkeypatch.setattr(
            "arbora.fleet.provisioning.require_provisioning", lambda: DOUBLE_SDK
        )
    return request.param
