"""Arbora's own OAuth client contract, reconciled in the identity provider.

ARBORA-IDENTITY-R030.4. Arbora declares the OAuth clients and client scopes that
its served sign-in needs (:class:`ClientContract`), and reconciles them through
the identity provider's realm admin interface at start and on an interval. The
environment supplies configuration only (``ARBORA_IDENTITY_CLIENT_CONTRACT``):
the provider URL, the realm, the admin credential reference, the native client
id and its loopback redirect URIs. The definition itself lives here, so an
out-of-band cleanup that deletes the native client is repaired without an
operator, and there is no second copy of the definition anywhere.

The reconciler is an explicit state machine (organization constitution Article
XXXI): one ``state`` field, one :func:`transition` over :data:`CONTRACT_EDGES`,
and the typed refusal :class:`IllegalContractTransition`. It only creates and
updates the items it declares and never deletes anything; dynamic client
registration stays available as the sign-in fallback (R030.4.4).

The admin adapter speaks the Keycloak-compatible realm admin REST interface.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

#: The configuration key: a JSON object (see :class:`ContractConfig`).
CONFIG_KEY = "ARBORA_IDENTITY_CLIENT_CONTRACT"

#: The default loopback callbacks of the native coding clients: a fixed
#: ``--callback-port`` of one client, and the fixed remote-MCP OAuth callback of
#: the other. Configuration replaces them (``native_redirect_uris``).
DEFAULT_NATIVE_REDIRECT_URIS: tuple[str, ...] = (
    "http://localhost:33418/callback",
    "http://127.0.0.1:19876/mcp/oauth/callback",
)
DEFAULT_NATIVE_CLIENT_ID = "arbora-native-mcp"
#: Default client scopes of the native client. ``offline_access`` makes the
#: refresh token an offline token, so a long coding session never re-logs in.
NATIVE_DEFAULT_SCOPES: tuple[str, ...] = (
    "mcp-fleet",
    "roles",
    "agent-services",
    "offline_access",
    "profile",
    "email",
)
#: Client scopes arbora requires to exist. ``mcp-fleet`` is shared with other
#: clients, so arbora only ensures it exists and never adds a mapper to it.
REQUIRED_SCOPES: tuple[str, ...] = ("mcp-fleet", "agent-services")
#: The audience scope: it carries an audience mapper for the configured audience
#: (the audience arbora verifies), so a native client's token carries it.
AUDIENCE_SCOPE = "agent-services"

RECONCILE_INTERVAL_SECONDS = 300.0
RETRY_CAP_SECONDS = 300.0
JITTER_LOW = 0.5
JITTER_HIGH = 1.0

#: Closed vocabulary of reconciliation failures (safe for unauthenticated readiness).
CONTRACT_FAILURE_CODES = frozenset(
    {
        "admin_credential_missing",
        "admin_refused",
        "admin_too_broad",
        "admin_unreachable",
        "client_drift",
        "client_missing",
        "scope_missing",
    }
)
#: Realm roles that make an admin credential too broad for the reconciler.
_BROAD_ADMIN_ROLES = frozenset({"admin", "create-realm"})


# --------------------------------------------------------------------------- #
# The contract (R030.4.1)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """One client scope; ``audience`` adds an audience mapper when set."""

    name: str
    audience: str | None = None


@dataclass(frozen=True, slots=True)
class ClientSpec:
    """One public authorization-code client with PKCE ``S256``."""

    client_id: str
    name: str
    redirect_uris: tuple[str, ...]
    default_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClientContract:
    """Every client and client scope that arbora's served sign-in requires."""

    clients: tuple[ClientSpec, ...]
    scopes: tuple[ScopeSpec, ...]


@dataclass(frozen=True, slots=True)
class ContractConfig:
    """The environment's part: where the provider is and which values to use."""

    admin_url: str
    realm: str
    admin_client_id: str
    admin_client_secret_ref: str
    admin_realm: str = "master"
    native_client_id: str = DEFAULT_NATIVE_CLIENT_ID
    native_redirect_uris: tuple[str, ...] = DEFAULT_NATIVE_REDIRECT_URIS
    audience: str = ""
    ca_bundle: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ContractConfig:
        required = ("admin_url", "realm", "admin_client_id", "admin_client_secret_ref")
        missing = [name for name in required if not str(raw.get(name) or "").strip()]
        if missing:
            raise ValueError(f"{CONFIG_KEY} lacks {', '.join(missing)}")
        redirects = raw.get("native_redirect_uris") or DEFAULT_NATIVE_REDIRECT_URIS
        if isinstance(redirects, str) or not all(
            isinstance(uri, str) and _is_loopback(uri) for uri in redirects
        ):
            raise ValueError("native_redirect_uris must be loopback http URIs")
        return cls(
            admin_url=str(raw["admin_url"]).rstrip("/"),
            realm=str(raw["realm"]),
            admin_client_id=str(raw["admin_client_id"]),
            admin_client_secret_ref=str(raw["admin_client_secret_ref"]),
            admin_realm=str(raw.get("admin_realm") or "master"),
            native_client_id=str(
                raw.get("native_client_id") or DEFAULT_NATIVE_CLIENT_ID
            ),
            native_redirect_uris=tuple(redirects),
            audience=str(raw.get("audience") or ""),
            ca_bundle=str(raw.get("ca_bundle") or ""),
        )


def _is_loopback(uri: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    return parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}


def configured_contract_config() -> ContractConfig | None:
    """The configured contract, or ``None`` when the feature is not configured."""
    from arbora.security.settings import setting

    raw = setting(CONFIG_KEY, "")
    if not raw:
        return None
    mapping = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{CONFIG_KEY} must be a JSON object")
    return ContractConfig.from_mapping(mapping)


def build_contract(config: ContractConfig) -> ClientContract:
    """The one definition of arbora's clients and scopes."""
    audience = config.audience or None
    return ClientContract(
        clients=(
            ClientSpec(
                client_id=config.native_client_id,
                name="Native MCP clients",
                redirect_uris=config.native_redirect_uris,
                default_scopes=NATIVE_DEFAULT_SCOPES,
            ),
        ),
        scopes=tuple(
            ScopeSpec(name, audience if name == AUDIENCE_SCOPE else None)
            for name in REQUIRED_SCOPES
        ),
    )


def client_representation(spec: ClientSpec) -> dict[str, Any]:
    """The admin representation of a declared client (only the fields we own)."""
    return {
        "clientId": spec.client_id,
        "name": spec.name,
        "protocol": "openid-connect",
        "publicClient": True,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": list(spec.redirect_uris),
        "webOrigins": [],
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": "+",
        },
    }


def scope_representation(spec: ScopeSpec) -> dict[str, Any]:
    """The admin representation of a declared client scope."""
    return {
        "name": spec.name,
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true",
            "display.on.consent.screen": "false",
        },
    }


def audience_mapper(spec: ScopeSpec) -> dict[str, Any]:
    """The audience mapper of an audience scope."""
    return {
        "name": f"{spec.name}-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": str(spec.audience),
            "access.token.claim": "true",
            "id.token.claim": "false",
        },
    }


def _client_differs(found: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    for key, value in wanted.items():
        if key == "attributes":
            attributes = found.get("attributes") or {}
            if any(attributes.get(k) != v for k, v in value.items()):
                return True
        elif key == "redirectUris":
            if sorted(found.get(key) or []) != sorted(value):
                return True
        elif found.get(key) != value:
            return True
    return False


# --------------------------------------------------------------------------- #
# The machine (R030.4.2)
# --------------------------------------------------------------------------- #
class ContractState(StrEnum):
    UNCHECKED = "unchecked"
    RECONCILING = "reconciling"
    CONVERGED = "converged"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class ContractEvent(StrEnum):
    RECONCILE_START = "reconcile_start"
    RECONCILE_OK = "reconcile_ok"
    RECONCILE_ERR = "reconcile_err"
    INTERVAL_ELAPSED = "interval_elapsed"
    RETRY_ELAPSED = "retry_elapsed"
    STOP = "stop"


class ContractGuard(StrEnum):
    CONTRACT_MATCHES = "contract_matches"


CONTRACT_MACHINE = "client_contract"
CLIENT_CONTRACT_TRANSITION_ILLEGAL = "CLIENT_CONTRACT_TRANSITION_ILLEGAL"


@dataclass(frozen=True, slots=True)
class ContractEdge:
    edge_id: str
    source: ContractState
    event: ContractEvent
    target: ContractState
    guard: ContractGuard | None = None


_C = ContractState
_V = ContractEvent
CONTRACT_EDGES: tuple[ContractEdge, ...] = (
    ContractEdge("C01", _C.UNCHECKED, _V.RECONCILE_START, _C.RECONCILING),
    ContractEdge(
        "C02",
        _C.RECONCILING,
        _V.RECONCILE_OK,
        _C.CONVERGED,
        ContractGuard.CONTRACT_MATCHES,
    ),
    ContractEdge("C03", _C.RECONCILING, _V.RECONCILE_ERR, _C.DEGRADED),
    ContractEdge("C04", _C.CONVERGED, _V.INTERVAL_ELAPSED, _C.RECONCILING),
    ContractEdge("C05", _C.DEGRADED, _V.RETRY_ELAPSED, _C.RECONCILING),
    ContractEdge("C06", _C.UNCHECKED, _V.STOP, _C.STOPPED),
    ContractEdge("C07", _C.RECONCILING, _V.STOP, _C.STOPPED),
    ContractEdge("C08", _C.CONVERGED, _V.STOP, _C.STOPPED),
    ContractEdge("C09", _C.DEGRADED, _V.STOP, _C.STOPPED),
)
_BY_PAIR = {(edge.source, edge.event): edge for edge in CONTRACT_EDGES}
TERMINAL_CONTRACT_STATES = frozenset({ContractState.STOPPED})


class IllegalContractTransition(RuntimeError):
    """The reconciler refused an event (``CLIENT_CONTRACT_TRANSITION_ILLEGAL``)."""

    code = CLIENT_CONTRACT_TRANSITION_ILLEGAL
    machine = CONTRACT_MACHINE

    def __init__(self, from_state: ContractState, event: ContractEvent) -> None:
        super().__init__(
            f"client contract refuses {event.value!r} in state {from_state.value!r}"
        )
        self.from_state = from_state
        self.event = event


@dataclass(frozen=True, slots=True)
class ContractFacts:
    """Guard input: whether the verified provider state matches the contract."""

    contract_matches: bool = True


def transition(
    state: ContractState, event: ContractEvent, *, facts: ContractFacts
) -> ContractState:
    """The one transition function of the client contract machine."""
    edge = _BY_PAIR.get((state, event))
    if edge is None or (
        edge.guard is ContractGuard.CONTRACT_MATCHES and not facts.contract_matches
    ):
        raise IllegalContractTransition(state, event)
    return edge.target


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """The outcome of one reconciliation pass."""

    ok: bool
    code: str | None = None
    items: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()


class ContractFailure(Exception):
    """A pass failure with a closed-vocabulary ``code`` and the affected items."""

    def __init__(self, code: str, items: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code = code if code in CONTRACT_FAILURE_CODES else "client_drift"
        self.items = tuple(items)


class ClientContractReconciler:
    """Holds the machine state; :meth:`apply` is the only writer of ``state``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state: ContractState = ContractState.UNCHECKED
        self.attempts = 0
        self.failure_code: str | None = None
        self.items: tuple[str, ...] = ()
        self.converged_once = False

    def apply(
        self,
        event: ContractEvent,
        *,
        result: ReconcileResult | None = None,
    ) -> ContractState:
        with self._lock:
            matches = bool(result and result.ok)
            target = transition(
                self.state, event, facts=ContractFacts(contract_matches=matches)
            )
            if event is ContractEvent.RECONCILE_OK:
                self.attempts = 0
                self.failure_code = None
                self.items = ()
                self.converged_once = True
            elif event is ContractEvent.RECONCILE_ERR:
                self.attempts += 1
                self.failure_code = (result.code if result else None) or "client_drift"
                self.items = result.items if result else ()
            self.state = target
            return target

    def readiness(self) -> dict[str, Any]:
        """``identity`` readiness: ``ready``, ``checking`` or ``degraded`` (R030.4.3)."""
        with self._lock:
            if self.state is ContractState.CONVERGED:
                return {"status": "ready"}
            if self.state is ContractState.DEGRADED:
                code = self.failure_code
                if code not in CONTRACT_FAILURE_CODES:
                    code = "client_drift"
                return {"status": "degraded", "reason": f"identity_{code}"}
            if self.state is ContractState.STOPPED:
                return {"status": "stopped"}
            return {"status": "checking"}


def next_retry_delay(attempt: int, rng: Any) -> float:
    """``min(300, 2^(n-1))`` seconds times a jitter factor from 0.5 to 1.0."""
    base = min(RETRY_CAP_SECONDS, 2.0 ** (max(1, int(attempt)) - 1))
    return base * rng.uniform(JITTER_LOW, JITTER_HIGH)


# --------------------------------------------------------------------------- #
# The realm admin adapter
# --------------------------------------------------------------------------- #
#: ``(method, path, body) -> (status, json)``; replaced by a fake in tests.
AdminTransport = Callable[[str, str, Any], tuple[int, Any]]


class RealmAdmin:
    """Keycloak-compatible realm admin REST calls under one admin token."""

    def __init__(self, config: ContractConfig, transport: AdminTransport) -> None:
        self.config = config
        self.transport = transport
        base = f"/admin/realms/{config.realm}"
        self.clients = f"{base}/clients"
        self.scopes = f"{base}/client-scopes"

    def call(self, method: str, path: str, body: Any = None) -> Any:
        status, payload = self.transport(method, path, body)
        if status in (401, 403):
            raise ContractFailure("admin_refused", (path,))
        if status >= 400:
            raise ContractFailure("client_drift", (path,))
        return payload


def http_transport(config: ContractConfig, secret: str) -> AdminTransport:
    """An httpx transport that mints a scoped admin token and refuses a broad one."""
    import httpx

    verify: Any = config.ca_bundle or True
    client = httpx.Client(base_url=config.admin_url, verify=verify, timeout=20.0)
    token_url = f"/realms/{config.admin_realm}/protocol/openid-connect/token"
    try:
        response = client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": config.admin_client_id,
                "client_secret": secret,
            },
        )
    except httpx.HTTPError as exc:
        client.close()
        raise ContractFailure("admin_unreachable") from exc
    if response.status_code != 200:
        client.close()
        raise ContractFailure("admin_refused")
    token = str(response.json().get("access_token") or "")
    if _token_is_too_broad(token):
        client.close()
        raise ContractFailure("admin_too_broad")
    headers = {"Authorization": f"Bearer {token}"}

    def send(method: str, path: str, body: Any) -> tuple[int, Any]:
        try:
            reply = client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ContractFailure("admin_unreachable") from exc
        payload = reply.json() if reply.content else None
        return reply.status_code, payload

    return send


def _token_is_too_broad(token: str) -> bool:
    """True when the admin token holds a provider-wide administrator role.

    Only the token's own claims are read; the provider already validated it.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (IndexError, ValueError):
        return False
    roles = set((claims.get("realm_access") or {}).get("roles") or ())
    return bool(roles & _BROAD_ADMIN_ROLES)


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #
def reconcile_once(contract: ClientContract, admin: RealmAdmin) -> ReconcileResult:
    """Create or update every declared item, then verify; never delete anything."""
    changed: list[str] = []
    try:
        scope_ids = _ensure_scopes(contract, admin, changed)
        for spec in contract.clients:
            _ensure_client(spec, admin, scope_ids, changed)
        missing = _verify(contract, admin)
    except ContractFailure as failure:
        return ReconcileResult(False, failure.code, failure.items, tuple(changed))
    if missing:
        code = (
            "scope_missing"
            if any(m.startswith("scope:") for m in missing)
            else (
                "client_missing"
                if any(m.startswith("client:") for m in missing)
                else "client_drift"
            )
        )
        return ReconcileResult(False, code, tuple(missing), tuple(changed))
    return ReconcileResult(True, None, (), tuple(changed))


def _scope_ids(admin: RealmAdmin) -> dict[str, str]:
    return {str(s["name"]): str(s["id"]) for s in admin.call("GET", admin.scopes) or ()}


def _ensure_scopes(
    contract: ClientContract, admin: RealmAdmin, changed: list[str]
) -> dict[str, str]:
    ids = _scope_ids(admin)
    for spec in contract.scopes:
        if spec.name not in ids:
            admin.call("POST", admin.scopes, scope_representation(spec))
            changed.append(f"scope:{spec.name}")
            ids = _scope_ids(admin)
        if spec.audience and spec.name in ids:
            path = f"{admin.scopes}/{ids[spec.name]}/protocol-mappers/models"
            mappers = admin.call("GET", path) or ()
            if not any(
                (m.get("config") or {}).get("included.custom.audience") == spec.audience
                for m in mappers
            ):
                admin.call("POST", path, audience_mapper(spec))
                changed.append(f"mapper:{spec.name}")
    return ids


def _find_client(admin: RealmAdmin, client_id: str) -> dict[str, Any] | None:
    found = admin.call("GET", f"{admin.clients}?clientId={client_id}") or []
    return dict(found[0]) if found else None


def _ensure_client(
    spec: ClientSpec,
    admin: RealmAdmin,
    scope_ids: Mapping[str, str],
    changed: list[str],
) -> None:
    wanted = client_representation(spec)
    found = _find_client(admin, spec.client_id)
    if found is None:
        admin.call("POST", admin.clients, wanted)
        changed.append(f"client:{spec.client_id}")
        found = _find_client(admin, spec.client_id)
        if found is None:
            raise ContractFailure("client_missing", (f"client:{spec.client_id}",))
    elif _client_differs(found, wanted):
        merged = {**found, **wanted}
        merged["attributes"] = {
            **(found.get("attributes") or {}),
            **wanted["attributes"],
        }
        admin.call("PUT", f"{admin.clients}/{found['id']}", merged)
        changed.append(f"client:{spec.client_id}")
    path = f"{admin.clients}/{found['id']}/default-client-scopes"
    present = {str(s.get("name")) for s in admin.call("GET", path) or ()}
    for name in spec.default_scopes:
        if name in present:
            continue
        if name not in scope_ids:
            raise ContractFailure("scope_missing", (f"scope:{name}",))
        admin.call("PUT", f"{path}/{scope_ids[name]}", None)
        changed.append(f"default-scope:{spec.client_id}:{name}")


def _verify(contract: ClientContract, admin: RealmAdmin) -> list[str]:
    missing: list[str] = []
    ids = _scope_ids(admin)
    missing += [f"scope:{s.name}" for s in contract.scopes if s.name not in ids]
    for spec in contract.clients:
        found = _find_client(admin, spec.client_id)
        if found is None:
            missing.append(f"client:{spec.client_id}")
            continue
        if _client_differs(found, client_representation(spec)):
            missing.append(f"drift:{spec.client_id}")
        path = f"{admin.clients}/{found['id']}/default-client-scopes"
        present = {str(s.get("name")) for s in admin.call("GET", path) or ()}
        missing += [
            f"default-scope:{spec.client_id}:{name}"
            for name in spec.default_scopes
            if name not in present
        ]
    return missing


# --------------------------------------------------------------------------- #
# Driving the machine: one pass, and the supervisor
# --------------------------------------------------------------------------- #
AdminFactory = Callable[[ContractConfig], RealmAdmin]


def default_admin_factory(config: ContractConfig) -> RealmAdmin:
    """Resolve the admin credential reference and open the admin transport."""
    from scion_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    try:
        secret = resolve_runtime_secret_reference(config.admin_client_secret_ref)
    except Exception as exc:
        raise ContractFailure("admin_credential_missing") from exc
    if not secret:
        raise ContractFailure("admin_credential_missing")
    return RealmAdmin(config, http_transport(config, secret))


def run_pass(
    reconciler: ClientContractReconciler,
    config: ContractConfig,
    admin_factory: AdminFactory = default_admin_factory,
) -> ReconcileResult:
    """Run one reconciliation pass from ``Reconciling`` and record its outcome."""
    try:
        admin = admin_factory(config)
        result = reconcile_once(build_contract(config), admin)
    except ContractFailure as failure:
        result = ReconcileResult(False, failure.code, failure.items)
    if result.ok:
        reconciler.apply(ContractEvent.RECONCILE_OK, result=result)
        if result.changed:
            logger.warning(
                "identity client contract repaired: %s", ", ".join(result.changed)
            )
    else:
        reconciler.apply(ContractEvent.RECONCILE_ERR, result=result)
        logger.error(
            "identity client contract degraded (code=%s, items=%s)",
            result.code,
            ", ".join(result.items) or "-",
        )
    return result


def reconcile_now(
    config: ContractConfig, admin_factory: AdminFactory = default_admin_factory
) -> tuple[ClientContractReconciler, ReconcileResult]:
    """One pass from a fresh machine; the ``arbora identity reconcile`` command."""
    reconciler = ClientContractReconciler()
    reconciler.apply(ContractEvent.RECONCILE_START)
    return reconciler, run_pass(reconciler, config, admin_factory)


@dataclass
class ClientContractSupervisor:
    """Reconciles at start, then every interval; backs off while degraded."""

    config: ContractConfig
    admin_factory: AdminFactory = default_admin_factory
    reconciler: ClientContractReconciler = field(
        default_factory=ClientContractReconciler
    )
    stop_event: threading.Event = field(default_factory=threading.Event)
    rng: Any = field(default_factory=random.Random)  # nosec B311 - jitter, not crypto
    _thread: threading.Thread | None = None

    def loop(self) -> None:
        self.reconciler.apply(ContractEvent.RECONCILE_START)
        while not self.stop_event.is_set():
            if self.reconciler.state is ContractState.RECONCILING:
                run_pass(self.reconciler, self.config, self.admin_factory)
                continue
            if self.reconciler.state is ContractState.CONVERGED:
                delay = RECONCILE_INTERVAL_SECONDS * self.rng.uniform(
                    JITTER_LOW, JITTER_HIGH
                )
                event = ContractEvent.INTERVAL_ELAPSED
            elif self.reconciler.state is ContractState.DEGRADED:
                delay = next_retry_delay(self.reconciler.attempts, self.rng)
                event = ContractEvent.RETRY_ELAPSED
            else:
                return
            if self.stop_event.wait(delay):
                break
            self.reconciler.apply(event)

    def start(self) -> None:
        from arbora.observability.runtime_health import register_identity_contract

        register_identity_contract(self.reconciler)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="IdentityClientContract"
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self.loop()
        except Exception as exc:  # pragma: no cover - last-resort guard
            logger.error("identity client contract supervisor crashed: %s", exc)

    def stop(self) -> None:
        from arbora.observability.runtime_health import register_identity_contract

        self.stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        if self.reconciler.state not in TERMINAL_CONTRACT_STATES:
            self.reconciler.apply(ContractEvent.STOP)
        register_identity_contract(None)


_SUPERVISOR: list[ClientContractSupervisor | None] = [None]


def start_client_contract_supervisor() -> ClientContractSupervisor | None:
    """Start the supervisor when the contract is configured; never raise into boot."""
    try:
        config = configured_contract_config()
    except ValueError as exc:
        logger.error("identity client contract not started: %s", exc)
        return None
    if config is None:
        return None
    supervisor = ClientContractSupervisor(config)
    supervisor.start()
    _SUPERVISOR[0] = supervisor
    return supervisor


def stop_client_contract_supervisor() -> None:
    supervisor, _SUPERVISOR[0] = _SUPERVISOR[0], None
    if supervisor is not None:
        supervisor.stop()


def reconcile_command(argv: Sequence[str]) -> int:
    """``arbora identity reconcile``: one pass; prints the state; 0 when converged."""
    del argv
    config = configured_contract_config()
    if config is None:
        print(json.dumps({"status": "not_configured", "config": CONFIG_KEY}))
        return 2
    reconciler, result = reconcile_now(config)
    print(
        json.dumps(
            {
                "state": reconciler.state.value,
                "code": result.code,
                "items": list(result.items),
                "changed": list(result.changed),
            }
        )
    )
    return 0 if reconciler.state is ContractState.CONVERGED else 1
