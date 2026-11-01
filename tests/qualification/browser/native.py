"""Native helpers for the reviewed disposable installed-browser CI fixture.

The caller owns the disposable process lifecycle and private credential files.
No fixture result or exception may be copied verbatim to public artifacts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import secrets
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


def verify_server_record(binary: Path, distribution: Any) -> str:
    """Bind the executable bytes to the installed, hash-locked server wheel."""
    matches = [
        entry
        for entry in distribution.files or ()
        if Path(distribution.locate_file(entry)).resolve() == binary
    ]
    if len(matches) != 1:
        raise RuntimeError("Native server wheel ownership verification failed")
    entry = matches[0]
    if entry.hash is None or entry.hash.mode != "sha256" or entry.size is None:
        raise RuntimeError("Native server wheel digest is unavailable")
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if encoded != entry.hash.value or binary.stat().st_size != entry.size:
        raise RuntimeError("Native server wheel digest verification failed")
    return digest.hex()


def require_installed() -> dict[str, str]:
    """Fail rather than skip if the exact installed native stack is missing."""
    prefix = Path(sys.prefix).resolve()
    versions = {}
    for package, module, expected in (
        ("arbora", "arbora", None),
        ("medulla", "epistemic_graph", "2.27.10"),
        ("medulla-stem", "epistemic_graph_server", "2.27.10"),
        ("meristem", "meristem", "5.0.0"),
        ("scion-sdk", "scion_sdk", "0.6.5"),
        ("overstory", "overstory", None),
    ):
        loaded = importlib.import_module(module)
        location = loaded.__file__
        if location is None or prefix not in Path(location).resolve().parents:
            raise RuntimeError("Qualification requires installed package imports")
        version = importlib.metadata.version(package)
        if expected is not None and version != expected:
            raise RuntimeError("Qualification dependency version mismatch")
        versions[package] = version
    import overstory

    assets = Path(overstory.__file__).resolve().parent / "dist"
    if not (assets / "index.html").is_file() or not (assets / "assets").is_dir():
        raise RuntimeError("Installed browser assets are incomplete")
    from epistemic_graph.server_binary import resolve_server_binary

    binary = Path(resolve_server_binary()).resolve()
    if prefix not in binary.parents:
        raise RuntimeError("Native server must come from the installed environment")
    # The engine does not implement --version. Verify the actual executable
    # against the exact installed wheel's RECORD, not an unsupported CLI flag.
    versions["native-server-sha256"] = verify_server_record(
        binary, importlib.metadata.distribution("medulla-stem")
    )
    return versions


async def require_native_version(client: Any) -> str:
    """Attest the running executable before creating fixture identities."""
    async with asyncio.timeout(15):
        health = await client.health()
    if not isinstance(health, dict) or health.get("version") != "2.27.10":
        raise RuntimeError("Native runtime version verification failed")
    return health["version"]


async def identity_operation(
    client: Any, graph: str, operation: dict[str, Any], expected_kind: str
) -> Any:
    """Send through the generated native contract; never forge a reply."""
    from epistemic_graph.generated.security import decode_identity, send_identity

    async with asyncio.timeout(15):
        response = await send_identity(client, {"op": operation}, graph)
        decode_identity(response)
    payload = response.payload
    if not isinstance(payload, dict) or set(payload) != {"kind", "value"}:
        raise RuntimeError("Native identity reply shape mismatch")
    if payload["kind"] != expected_kind:
        raise RuntimeError("Native identity reply kind mismatch")
    return payload["value"]


async def initialize_local_identity(
    broker_client: Any, graph: str, *, admin_username: str, admin_password: str
) -> None:
    """Initialize the disposable native store, never a production identity DB."""
    if graph != "__control__":
        raise ValueError("Exact fixture identity graph required")
    value = await identity_operation(
        broker_client,
        graph,
        {
            "family": "config",
            "op": "initialize",
            "request": {
                "mode": "local",
                "admin_username": admin_username,
                "admin_password": admin_password,
            },
        },
        "principal",
    )
    if not isinstance(value, dict) or not value.get("principal_id"):
        raise RuntimeError("Native identity mode initialization failed")


async def provision_managed_broker(
    admin_client: Any,
    graph: str,
    *,
    principal_id: str,
    username: str,
    system_principal_id: str,
) -> None:
    """Register a distinct managed broker, never the bootstrap System principal.

    Native session resolution requires a managed service principal. Its role
    gets only identity:authenticate; the distinct external provisioning client
    retains administration. No password, session or graph grant is created for
    this service. The actual serving composition MUST use this broker too;
    substituting it only in the fixture helper is not qualification. The
    serving composition uses WEBUI_BROKER_TOKEN_REF for this same distinct
    managed principal. This fixture still requires reviewed isolated CI execution.
    """
    if principal_id == system_principal_id:
        raise ValueError("Managed broker must not collide with native System identity")
    await identity_operation(
        admin_client,
        graph,
        {
            "family": "access",
            "op": "upsert_role",
            "request": {
                "role_id": "fixture:browser-broker",
                "name": "Disposable browser broker",
                "scopes": ["identity:authenticate"],
                "graph_grants": [],
            },
        },
        "done",
    )
    created = await identity_operation(
        admin_client,
        graph,
        {
            "family": "user",
            "op": "create",
            "request": {
                "principal_id": principal_id,
                "username": username,
                "kind": "service",
                "roles": ["fixture:browser-broker"],
                "groups": [],
            },
        },
        "principal",
    )
    if not isinstance(created, dict) or created.get("principal_id") != principal_id:
        raise RuntimeError("Native broker principal mismatch")
    view = await identity_operation(
        admin_client,
        graph,
        {"family": "user", "op": "get", "request": {"id": principal_id}},
        "user",
    )
    if (
        not isinstance(view, dict)
        or view.get("kind") != "service"
        or view.get("has_password") is not False
        or view.get("is_bootstrap") is not False
    ):
        raise RuntimeError("Native broker registration mismatch")


async def provision_human(
    admin_client: Any,
    graph: str,
    *,
    username: str,
    password: str,
    tenant_graph: str,
    seed_permissions: bool = False,
    denied: bool = False,
) -> str:
    """Create one disposable native human with enumerated fixture permissions.

    admin_client must already carry the operator's verified provisioning
    authority. This function neither creates that authority nor changes scopes.
    It must run only against a new isolated fixture store.
    """
    if graph != "__control__" or tenant_graph == graph:
        raise ValueError("Distinct exact fixture graphs required")
    if not tenant_graph.startswith("tenant__") or "*" in tenant_graph:
        raise ValueError("Named tenant graph required")
    if denied and seed_permissions:
        raise ValueError("Denied fixture cannot have seed permissions")
    grants = [
        {"resource": {"Graph": name}, "action": "Read", "effect": "Allow"}
        for name in (graph, tenant_graph)
    ]
    scopes = ["kg:read", "work:read"]
    if seed_permissions:
        # Exact registered native capabilities; work:write implies neither submit nor claim.
        scopes.extend(["work:submit", "work:claim", "work:write", "service:control"])
        # The installed SDK's submit negotiation calls Health. service:control
        # is User-class but also covers other control reads/cancellation; this
        # disposable seed principal is deliberately not read-only.
        grants.append(
            {"resource": {"Graph": graph}, "action": "Write", "effect": "Allow"}
        )
    role_id = "fixture:protected-read-denied" if denied else "webui:user"
    await identity_operation(
        admin_client,
        graph,
        {
            "family": "access",
            "op": "upsert_role",
            "request": {
                "role_id": role_id,
                "name": "Disposable browser qualification",
                "scopes": ["identity:self"] if denied else scopes,
                "graph_grants": [] if denied else grants,
            },
        },
        "done",
    )
    admitted = await identity_operation(
        admin_client,
        graph,
        {
            "family": "user",
            "op": "create",
            "request": {
                "username": username,
                "kind": "human",
                "password": password,
                "must_change": False,
                "roles": [role_id],
                "groups": [],
            },
        },
        "principal",
    )
    if not isinstance(admitted, dict) or not isinstance(
        admitted.get("principal_id"), str
    ):
        raise TypeError("Native principal creation reply mismatch")
    if denied:
        # Native human creation unconditionally adds built-in `user` (kg:read).
        # Remove only this disposable user's binding, never edit the shared role.
        await identity_operation(
            admin_client,
            graph,
            {
                "family": "access",
                "op": "change_user_role",
                "request": {
                    "principal_id": admitted["principal_id"],
                    "role_id": "user",
                    "change": "remove",
                },
            },
            "done",
        )
    created = await identity_operation(
        admin_client,
        graph,
        {"family": "user", "op": "get", "request": {"id": admitted["principal_id"]}},
        "user",
    )
    if (
        not isinstance(created, dict)
        or not isinstance(created.get("principal_id"), str)
        or not created["principal_id"]
        or created.get("kind") != "human"
        or created.get("is_bootstrap") is not False
        or created.get("has_password") is not True
    ):
        raise RuntimeError("Native human provisioning did not match fixture identity")
    return created["principal_id"]


async def authenticate_seed_session(
    broker_client: Any,
    graph: str,
    *,
    username: str,
    password: str,
    principal_id: str,
    issuer: Any,
    caller: Any,
    denied: bool = False,
    stage: Callable[[str], None] | None = None,
) -> tuple[Any, str]:
    """Use actual native password authority and production issuer/resolver.

    The returned native credential stays private and is revoked by fixture
    teardown; it is not the browser cookie or an operator/process identity.
    """
    credential = secrets.token_urlsafe(32)
    if stage is not None:
        stage("native_credential_authenticate")
    result = await identity_operation(
        broker_client,
        graph,
        {
            "family": "credential",
            "op": "authenticate",
            "request": {
                "username": username,
                "password": password,
                "session_token": credential,
                "ip_prefix": "127.0.0.0/24",
            },
        },
        "authenticate",
    )
    if result.get("outcome") != "ok" or result.get("principal_id") != principal_id:
        raise RuntimeError("Native fixture authentication incomplete")
    if stage is not None:
        stage("native_local_issuer")
    token = await issuer.issue(credential)
    if stage is not None:
        stage("native_browser_caller")
    session = await caller(token, credential)
    from arbora.security.actor_context import ActorType

    if (
        session.actor.actor_id != principal_id
        or session.actor.actor_type != ActorType.HUMAN
    ):
        raise RuntimeError("Fixture human session mismatch")
    required = {
        "kg:read",
        "work:read",
        "work:submit",
        "work:claim",
        "work:write",
        "service:control",
    }
    if denied:
        if set(session.scopes) != {"identity:self"}:
            raise RuntimeError("Denied human has unexpected native authority")
    elif not required.issubset(session.scopes):
        raise RuntimeError("Fixture seed permissions unavailable")
    return session, credential


async def seed_owned_run(
    engine: Any,
    session: Any,
    invocation: Any,
    stage: Callable[[str], None],
) -> dict[str, str]:
    """Persist a native human-owned WorkItem and current plan, never fake reads.

    Caller supplies the exact session returned by authenticate_seed_session.
    Keep its enumerated seed permissions during the browser journey: this is
    not evidence for a read-only principal. No TaskPlan topology is invented.
    """
    from types import SimpleNamespace

    from scion_sdk.current_plan import PlanItem

    from arbora import current_plan_publication, work_items
    from arbora.a2a.authority import WorkItemA2AAuthority
    from arbora.api.contracts.session import use_session
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.security.actor_context import ActorType

    if session.actor.actor_type != ActorType.HUMAN:
        raise RuntimeError("Native seed must belong to the authenticated human")
    session = session.with_graph(CONTROL_GRAPH)
    run_id = f"browser-qualification-{uuid.uuid4().hex}"
    context_id = "a2a-context-" + secrets.token_hex(32)
    with use_session(session):
        stage("native_work_submit")
        item_id, created = work_items.submit_work_item_atomic(
            engine,
            kind="orchestrator_task",
            queue="orchestrator_task",
            resource_class="agent_dispatch",
            tenant=session.tenant,
            run_id=run_id,
            idempotency_key=run_id,
            payload_ref=run_id,
            description="Disposable installed browser qualification",
            created_by=session.actor.actor_id,
            metadata={
                "a2a_schema": "arbora-a2a-unary-v1",
                "a2a_owner_ref": WorkItemA2AAuthority._owner_ref(session),
                "a2a_run_id": run_id,
                "a2a_context_id": context_id,
            },
        )
        if created is not True:
            raise RuntimeError("Native fixture WorkItem was not freshly admitted")
        stage("native_work_claim")
        claim = work_items.claim_specific(
            engine,
            item_id,
            token=f"browser-worker:{run_id}",
            lease_ttl_s=600,
        )
        if claim is None:
            raise RuntimeError("Native fixture claim refused")
        stage("native_plan_binding")
        request = SimpleNamespace(run_id=run_id, metadata={"session": session})
        with current_plan_publication.bind_admitted_claim(
            engine,
            session=session,
            run_id=run_id,
            work_item_id=item_id,
            claim=claim,
            hosted_invocation=invocation,
            owner_graph=CONTROL_GRAPH,
        ):
            stage("native_plan_initialization")
            async with current_plan_publication.publication_scope(request) as publisher:
                if publisher is None:
                    raise RuntimeError("Native current-plan publisher unavailable")
                stage("native_plan_publish_empty")
                await publisher.publish(0, ())
                stage("native_plan_publish_inspect")
                await publisher.publish(
                    1,
                    (
                        PlanItem(
                            text="inspect",
                            status="in_progress",
                        ),
                    ),
                )
    return {"task_id": item_id, "run_id": run_id, "context_id": context_id}


async def revoke_human_sessions(
    admin_client: Any, graph: str, principal_id: str
) -> None:
    """Revoke only the fixture human, then require native confirmation."""
    request = {"id": principal_id}
    before = await identity_operation(
        admin_client,
        graph,
        {
            "family": "session",
            "op": "list",
            "request": request,
        },
        "sessions",
    )
    if not isinstance(before, list):
        raise RuntimeError("Native session census unavailable")
    live = [
        item
        for item in before
        if isinstance(item, dict) and item.get("revoked") is False
    ]
    # The private seed session and the browser's second login must both be live.
    now = int(time.time() * 1000)
    if len(live) < 2 or any(
        item.get("principal_id") != principal_id
        or min(item.get("idle_expires_at_ms", 0), item.get("absolute_expires_at_ms", 0))
        <= now + 60000
        for item in live
    ):
        raise RuntimeError("Revocation proof requires fresh native sessions")
    result = await identity_operation(
        admin_client,
        graph,
        {"family": "session", "op": "revoke_all", "request": request},
        "done",
    )
    if not isinstance(result, dict) or result.get("changed") is not True:
        raise RuntimeError("Native revocation did not change live sessions")
    sessions = await identity_operation(
        admin_client,
        graph,
        {
            "family": "session",
            "op": "list",
            "request": request,
        },
        "sessions",
    )
    if not isinstance(sessions, list) or any(
        not isinstance(item, dict)
        or item.get("principal_id") != principal_id
        or item.get("revoked") is not True
        for item in sessions
    ):
        raise RuntimeError("Native session revocation was not confirmed")
