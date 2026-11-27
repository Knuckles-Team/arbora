"""``arbora-identity``: the operator identity commands (ARBORA-IDENTITY-R013, R015).

``reconcile`` is served by the client-contract reconciler. The account lifecycle
commands (``claim``, ``link-claim``, ``transition``, ``reset-admin``, ``rotate``)
send engine ``Identity`` operations through the one administration port. Arbora
holds no identity record: every answer is the engine's. Each command prints one
JSON object ``{"command", "ok", "detail"}`` and exits 0 (done) or 2 (refused),
and records one audit line. Nothing here accepts a password on a command line:
``claim`` reads it from the terminal prompt, and ``reset-admin`` shows its
one-time token once, on standard error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNAVAILABLE_EXIT = 3
USAGE_EXIT = 2
REFUSED_EXIT = 2

#: The administrator a fresh store is claimed for.
BOOTSTRAP_PRINCIPAL = "usr:bootstrap"
DEFAULT_OVERLAP_SECONDS = 600
OVERLAP_RANGE = (300, 86400)
RESET_TTL_MS = 15 * 60 * 1000

#: Account lifecycle commands served over the engine ``Identity`` operations.
PENDING_COMMANDS: tuple[str, ...] = (
    "claim",
    "link-claim",
    "transition",
    "reset-admin",
    "rotate",
)
COMMANDS: tuple[str, ...] = ("reconcile", *PENDING_COMMANDS)

logger = logging.getLogger("arbora.identity.audit")


class CommandRefused(Exception):
    """A typed refusal: one stable code, and a message that names no secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass
class KeyRing:
    """The issuer signing-key ring ``arbora/identity/issuer-signing-keys``.

    Private halves stay in the ring (mode 0600); the engine receives only the
    public key. ``rotated_at`` is the last rotation instant, which opens the
    overlap window that keeps the previous key valid.
    """

    root: Path
    clock: Callable[[], float] = time.time

    def last_rotation(self) -> float | None:
        marker = self.root / "rotated_at"
        return float(marker.read_text()) if marker.is_file() else None

    def store(self, kid: str, private_pem: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self.root / f"{kid}.pem"
        key.write_bytes(private_pem)
        key.chmod(0o600)
        (self.root / "rotated_at").write_text(str(self.clock()))


@dataclass
class Environment:
    """What the commands act through; tests bind recorded ports here."""

    admin: Any
    client: Any
    claims: dict[str, Any]
    ring: KeyRing
    prompt_password: Callable[[], str] = field(
        default=lambda: getpass.getpass("administrator password: ")
    )
    show_once: Callable[[str], None] = field(
        default=lambda text: print(text, file=sys.stderr)
    )
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS
    audit: Callable[[str, bool, str], None] = field(
        default=lambda command, ok, code: logger.info(
            "identity.cli command=%s ok=%s code=%s", command, ok, code
        )
    )

    async def identity(
        self, family: str, op: str, request: dict[str, Any] | None, expect: str
    ) -> Any:
        return await self.admin.identity(
            self.client, self.claims, family, op, request, expect=expect
        )


def _refusal_code(error: Exception) -> str:
    return str(getattr(error, "code", "")) or ""


NOT_INITIALIZED = "IDENTITY_NOT_INITIALIZED"


async def _config(env: Environment) -> dict[str, Any] | None:
    """The engine ``IdentityConfig``, or ``None`` while the store is uninitialised.

    ``config.get`` answers ``kind: config`` with the record (``epoch``, ``mode``,
    ``issuer_kid_current`` ...); a fresh store refuses ``IDENTITY_NOT_INITIALIZED``.
    """
    try:
        value = await env.identity("config", "get", None, "config")
    except Exception as error:
        if _refusal_code(error) == NOT_INITIALIZED:
            return None
        raise
    return value if isinstance(value, dict) else None


async def _users(env: Environment, limit: int) -> list[dict[str, Any]]:
    """``user.list`` answers ``kind: users`` whose value is the list of views."""
    try:
        value = await env.identity("user", "list", {"limit": limit}, "users")
    except Exception as error:
        if _refusal_code(error) == NOT_INITIALIZED:
            return []
        raise
    return list(value) if isinstance(value, list) else []


async def claim(env: Environment, username: str) -> dict[str, Any]:
    """Claim ``usr:bootstrap`` of a fresh store and enrol its password."""
    if await _config(env) is not None:
        raise CommandRefused("CLAIM_ALREADY_DONE")
    if await _users(env, 1):
        raise CommandRefused("STORE_NOT_FRESH")
    await env.identity(
        "config",
        "initialize",
        {
            "admin_username": username,
            "admin_password": env.prompt_password(),
            "mode": "local",
        },
        "principal",
    )
    return {"principal": BOOTSTRAP_PRINCIPAL, "username": username}


def _other_owners(rows: Any) -> list[Any]:
    """The provisioned rows of a subject that belong to another principal."""
    owned = (r for r in rows if isinstance(rows, list) and isinstance(r, dict))
    return [
        r
        for r in owned
        if (r.get("user") or {}).get("principal_id") != BOOTSTRAP_PRINCIPAL
    ]


async def link_claim(env: Environment, provider: str, subject: str) -> dict[str, Any]:
    """Link the claimed administrator to a provider identity.

    The preview is ``idp.list_provisioned``: a link never reassigns, so a
    subject already provisioned to another principal refuses
    ``LINK_OWNERSHIP_CHANGES`` before the link is sent.
    """
    rows = await env.identity(
        "idp",
        "list_provisioned",
        {"idp_id": provider, "subject": subject, "limit": 1},
        "provisioned",
    )
    moved = _other_owners(rows)
    if moved:
        raise CommandRefused("LINK_OWNERSHIP_CHANGES", f"{len(moved)} principal(s)")
    await env.identity(
        "idp",
        "link",
        {"idp_id": provider, "principal_id": BOOTSTRAP_PRINCIPAL, "subject": subject},
        "done",
    )
    return {"provider": provider, "principal": BOOTSTRAP_PRINCIPAL}


async def _revoke_sessions(env: Environment) -> int:
    rows = await _users(env, 1000)
    for row in rows:
        await env.identity("session", "revoke_all", {"id": row["principal_id"]}, "done")
    return len(rows)


async def transition(env: Environment, target: str) -> dict[str, Any]:
    """Run the wizard steps non-interactively and compare-and-set the mode."""
    from arbora.identity.admin_console import AdminConsoleTab, transition_mode_step

    transition_mode_step(AdminConsoleTab("policy"), target)
    state = await _config(env)
    if state is None or "epoch" not in state:
        raise CommandRefused("TRANSITION_PRECONDITION_FAILED", "no mode epoch")
    keys = await env.identity("token", "read_issuer_keys", None, "issuer_keys")
    active = (
        next(
            (
                key.get("kid")
                for key in keys
                if isinstance(key, dict) and key.get("state") == "active"
            ),
            None,
        )
        if isinstance(keys, list)
        else None
    )
    if not active:
        raise CommandRefused("TRANSITION_PRECONDITION_FAILED", "no issuer key")
    try:
        await env.identity(
            "config",
            "transition",
            {"expected_epoch": state["epoch"], "issuer_kid": active, "to": target},
            "config",
        )
    except Exception as error:
        if _refusal_code(error) in {"CONFLICT", "IDENTITY_EPOCH_CONFLICT"}:
            raise CommandRefused(
                "TRANSITION_PRECONDITION_FAILED", "epoch moved"
            ) from error
        raise
    revoked = await _revoke_sessions(env)
    return {"to": target, "sessions_revoked": revoked}


async def reset_admin(env: Environment) -> dict[str, Any]:
    """Issue a one-time reset token for the administrator, shown once."""
    token = secrets.token_urlsafe(32)
    await env.identity(
        "token",
        "issue_admin_reset",
        {"principal_id": BOOTSTRAP_PRINCIPAL, "token": token, "ttl_ms": RESET_TTL_MS},
        "reset_delivery",
    )
    env.show_once(token)
    return {
        "principal": BOOTSTRAP_PRINCIPAL,
        "ttl_ms": RESET_TTL_MS,
        "shown": "once on the terminal",
    }


async def rotate(env: Environment) -> dict[str, Any]:
    """Replace the issuer signing key; the previous key stays for the overlap."""
    last = env.ring.last_rotation()
    if last is not None and env.ring.clock() - last < env.overlap_seconds:
        raise CommandRefused("ROTATION_OVERLAP_ACTIVE", f"{env.overlap_seconds}s")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    kid = "iss-" + secrets.token_hex(8)
    await env.identity(
        "token",
        "rotate_issuer_key",
        {"algorithm": "EdDSA", "kid": kid, "public_key_hex": public.hex()},
        "issuer_keys",
    )
    env.ring.store(
        kid,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return {"kid": kid, "overlap_seconds": env.overlap_seconds}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arbora-identity")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reconcile")
    commands.add_parser("claim").add_argument("--username", required=True)
    link = commands.add_parser("link-claim")
    link.add_argument("--provider", required=True)
    link.add_argument("--subject", required=True)
    commands.add_parser("transition").add_argument(
        "--to", required=True, choices=("none", "local", "external")
    )
    commands.add_parser("reset-admin")
    commands.add_parser("rotate")
    return parser


async def _run(env: Environment, args: argparse.Namespace) -> dict[str, Any]:
    match args.command:
        case "claim":
            return await claim(env, args.username)
        case "link-claim":
            return await link_claim(env, args.provider, args.subject)
        case "transition":
            return await transition(env, args.to)
        case "reset-admin":
            return await reset_admin(env)
        case _:
            return await rotate(env)


def _no_environment() -> Environment:
    """The default of :func:`main`: no composition root supplied an engine environment."""
    from arbora.identity.engine import IdentityUnavailable

    raise IdentityUnavailable("no engine environment is composed")


def main(
    argv: Sequence[str] | None = None,
    environment: Callable[[], Environment] = _no_environment,
) -> int:
    """Run one command; the composition root (``arbora.identity_cli``) supplies
    ``environment``, so this package never imports a served package."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        print(json.dumps({"status": "usage", "commands": list(COMMANDS)}))
        return USAGE_EXIT
    if args[0] == "reconcile":
        from arbora.identity.client_contract import reconcile_command

        return reconcile_command(args[1:])
    try:
        parsed = _parser().parse_args(args)
    except SystemExit:
        return USAGE_EXIT
    from arbora.identity.engine import IdentityUnavailable

    env: Environment | None = None
    try:
        env = environment()
        detail = asyncio.run(_run(env, parsed))
    except CommandRefused as refused:
        out = {
            "code": refused.code,
            **({"detail": refused.detail} if refused.detail else {}),
        }
        _emit(env, parsed.command, False, out, refused.code)
        return REFUSED_EXIT
    except IdentityUnavailable:
        _emit(env, parsed.command, False, {"code": "UNAVAILABLE"}, "UNAVAILABLE")
        return UNAVAILABLE_EXIT
    except Exception as error:  # noqa: BLE001 - an engine refusal keeps its wire code
        code = _refusal_code(error)
        if not code:
            raise
        _emit(env, parsed.command, False, {"code": code}, code)
        return REFUSED_EXIT
    _emit(env, parsed.command, True, detail, "")
    return 0


def _emit(
    env: Environment | None, command: str, ok: bool, detail: dict[str, Any], code: str
) -> None:
    print(json.dumps({"command": command, "ok": ok, "detail": detail}))
    if env is not None:
        env.audit(command, ok, code)
