"""Explicit local stdio backend for tuig; never a second terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import replace
from datetime import timedelta
from typing import Any


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="arbora-acp",
        description="Internal ACP stdio backend for tuig's explicit local runtime profile.",
    )
    result.add_argument(
        "--local",
        action="store_true",
        help="Explicitly select local execution; never a remote fallback",
    )
    result.add_argument(
        "--model",
        required=True,
        help="Explicit provider:model selected by the operator",
    )
    result.add_argument(
        "--workspace-root",
        required=True,
        help="Exact absolute POSIX client workspace root",
    )
    result.add_argument("--request-limit", type=int, default=16)
    result.add_argument("--timeout-seconds", type=float, default=120.0)
    result.add_argument(
        "--client-reads",
        action="store_true",
        help="Enable declared client reads; no local filesystem tools",
    )
    return result


def compose_local(args: argparse.Namespace) -> Any:
    """Compose one ephemeral authority and the existing AU run mechanism.

    Completed text turns use bounded process-local Pydantic history. No disk
    history, engine, fleet or listener is started.
    """
    from agent_utilities import host_ports
    from agent_utilities.runtime import RunRequest, default_run_plan

    from arbora.api.contracts.session import SessionExpiredError
    from arbora.security.request_identity import mint_local_process_session

    from .adapter import HostedACPAgent
    from .local_history import LocalConversationHistory
    from .session_policy import AcpSessionPolicy, PrincipalContext

    if host_ports.run_decision_provider() is not None:
        raise RuntimeError("local runtime requires an isolated process")
    session = mint_local_process_session()
    history = LocalConversationHistory(args.model)

    def principal() -> PrincipalContext:
        nonlocal session
        try:
            session.ensure_authority_current(minimum_ttl_seconds=30)
        except SessionExpiredError:
            session = mint_local_process_session()
        session.require_scope("kg:write")
        return PrincipalContext(session.actor.actor_id, session.tenant, session.scopes)

    def plan(value: RunRequest):
        selected = default_run_plan(value, {})
        return replace(
            selected,
            budget=replace(
                selected.budget,
                request_limit=args.request_limit,
                timeout_seconds=args.timeout_seconds,
            ),
            harness=replace(
                selected.harness,
                plan_scaffold="off",
                client_tools=("read_file",) if args.client_reads else (),
            ),
        )

    host_ports.register_run_decision_provider(plan)
    host_ports.register_run_history_provider(history.read)
    return HostedACPAgent(
        principal,
        policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({args.workspace_root}),
        request_factory=history.request,
        completed=history.complete,
        closed=history.close,
        max_prompt_bytes=16384,
        max_output_bytes=65536,
    )


def _validate_arguments(
    arguments: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if not args.local:
        arguments.error(
            "local execution requires --local; select central Arbora in tuig otherwise"
        )
    if ":" not in args.model or not all(args.model.split(":", 1)):
        arguments.error("--model must be an explicit provider:model")
    if not args.workspace_root.startswith("/") or "\\" in args.workspace_root:
        arguments.error("this local profile requires an absolute POSIX workspace root")
    if (
        args.request_limit < 1
        or not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
    ):
        arguments.error("run limits must be positive and finite")


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    _validate_arguments(arguments, args)
    try:
        from .adapter import serve_acp_stdio

        agent = compose_local(args)
        asyncio.run(serve_acp_stdio(agent))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "Local ACP runtime unavailable; check the installed ACP/AU versions and local configuration.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
