"""The connector sweep: every fleet server, health, catalog, one read-only call and pack state.

The sweep reads the fleet configuration file named by ``fleet_config`` and probes
each server through served operations only (``fleet.catalog.search``,
``fleet.catalog.list`` and ``fleet.call``). A call is attempted only for a tool the
server itself marks read-only and whose name does not read as a write; its
arguments are trivial and every call is idempotent-keyed. Nothing here names a
connector: the set comes from the file and the optional ``connectors`` filter.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess  # nosec B404 -- fixed argv, no shell
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .runner import AcceptanceConfig, Arbora, ResultWriter, err, excerpt, ok, unwrap

SCENARIO = "fleet"
COMPONENT_KINDS = ("tool", "skill", "mcp_prompt", "mcp_resource")
#: A read-only tool whose name reads as one of these is never called.
_WRITE_WORDS = re.compile(
    r"(^|_)(ingest|sync|import|export|delete|remove|create|add|update|upsert|write|put|post|patch|set|"
    r"send|run|exec|execute|start|stop|restart|trigger|launch|cancel|kill|download|upload|transcribe|"
    r"record|rotate|revoke|grant|enable|disable|apply|deploy|install|move|copy|rename|archive|purge|"
    r"clear|reset|approve|publish|login|logout|unseal|seal|init|scan|refresh|reindex|backup|restore|"
    r"merge|push|pull|generate|convert|render|sign|encrypt|decrypt)(_|$)",
    re.IGNORECASE,
)
#: Cheap reads first: identity, health, version; then listings; then search.
_CHEAP = re.compile(
    r"version|health|status|whoami|(^|_)me$|info|settings|config|ping|about|system",
    re.IGNORECASE,
)
_LISTING = re.compile(
    r"(^|_)(list|get|search|find|query|read|show|describe)", re.IGNORECASE
)
_TRIVIAL = MappingProxyType(
    {k: "test" for k in ("query", "q", "search", "term", "keyword", "text")}
)
_PAGE_KEYS = (
    "limit",
    "per_page",
    "page_size",
    "max_results",
    "size",
    "top",
    "count",
    "pagesize",
    "perpage",
)


@dataclass
class FleetResult:
    server: str
    tools: str = "-"
    call: str = "-"
    pack: str = "-"
    result: str = "FAIL"
    causes: list[str] = field(default_factory=list)


def fleet_servers(path: Path) -> dict[str, dict[str, Any]]:
    """The servers of a fleet configuration file, sorted by name."""
    config = json.loads(path.read_text(encoding="utf-8"))
    return dict(sorted((config.get("mcpServers") or {}).items()))


def ledger_counts(path: Path | None) -> dict[str, list[tuple[str, int]]]:
    """package -> [(version, tool count)] in ledger order; later rows supersede."""
    out: dict[str, list[tuple[str, int]]] = {}
    if path is None or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        columns = line.split("\t")
        if len(columns) >= 3 and columns[2].isdigit():
            out.setdefault(columns[0], []).append((columns[1], int(columns[2])))
    return out


def trivial_args(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Arguments for a tool with no or only trivial required params; None otherwise."""
    props = schema.get("properties") or {}
    args: dict[str, Any] = {}
    for name in schema.get("required") or []:
        prop = props.get(name) or {}
        if name.lower() in _TRIVIAL and prop.get("type", "string") == "string":
            args[name] = _TRIVIAL[name.lower()]
        elif name.lower() in (*_PAGE_KEYS, "page") and prop.get("type") in (
            "integer",
            "number",
        ):
            args[name] = 1
        else:
            return None
    for name, prop in props.items():
        if (
            name not in args
            and name.lower() in _PAGE_KEYS
            and prop.get("type")
            in (
                "integer",
                "number",
            )
        ):
            args[name] = 1
    return args


def read_only_candidates(
    tools: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Tools safe to call, fewest parameters first. ``tools`` are catalog items."""
    picks = []
    for tool in tools:
        annotations = tool.get("annotations") or {}
        if (
            annotations.get("readOnlyHint") is not True
            or annotations.get("destructiveHint") is True
        ):
            continue
        name = str(tool.get("name", ""))
        if not name or _WRITE_WORDS.search(name):
            continue
        schema = tool.get("input_schema") or tool.get("inputSchema") or {}
        args = trivial_args(schema)
        if args is None:
            continue
        required = len(schema.get("required") or [])
        params = len(schema.get("properties") or {})
        rank = (
            required,
            min(params, 3),
            0 if _CHEAP.search(name) else 1 if _LISTING.search(name) else 2,
            len(name),
            name,
        )
        picks.append((rank, name, args))
    return [(name, args) for _, name, args in sorted(picks)]


def call_error(envelope: dict[str, Any]) -> str | None:
    """Why a ``fleet.call`` result is an error, or None for non-error content."""
    if not ok(envelope):
        return err(envelope)
    value = unwrap(envelope)
    if isinstance(value, dict) and value.get("isError"):
        return excerpt(value.get("content") or value, 120)
    return None


async def pack_states(arbora: Arbora, servers: list[str]) -> dict[str, dict[str, int]]:
    """Per server, the imported component count per kind, from ``fleet.catalog.list``."""
    states: dict[str, dict[str, int]] = {name: {} for name in servers}
    for kind in COMPONENT_KINDS:
        envelope = await arbora.op(
            "find",
            "fleet.catalog.list",
            {"servers": servers, "kinds": [kind], "limit": 1000},
        )
        value = unwrap(envelope)
        items = value.get("items", []) if isinstance(value, dict) else value or []
        for item in items:
            server = item.get("server") if isinstance(item, dict) else None
            if server in states:
                states[server][kind] = states[server].get(kind, 0) + 1
    return states


async def audit_server(
    arbora: Arbora, server: str, packs: dict[str, int]
) -> FleetResult:
    """The three checks of one server: catalog, one read-only call, pack."""
    result = FleetResult(server)
    envelope = await arbora.op(
        "find",
        "fleet.catalog.search",
        {"servers": [server], "kinds": ["tool"], "limit": 100},
    )
    value = unwrap(envelope)
    items = value.get("items", []) if isinstance(value, dict) else value or []
    tools = [
        i for i in items if isinstance(i, dict) and i.get("kind", "tool") == "tool"
    ]
    if not ok(envelope) or not tools:
        result.causes.append(
            f"catalog: {err(envelope) if not ok(envelope) else 'no tools'}"
        )
    else:
        result.tools = str(len(tools))
    candidates = read_only_candidates(tools)
    if not candidates:
        result.call = "none"
        result.causes.append("no read-only candidate tool")
    else:
        name, args = candidates[0]
        outcome = await arbora.op(
            "act",
            "fleet.call",
            {"server": server, "tool": name, "arguments": args},
            idempotent=True,
        )
        failure = call_error(outcome)
        result.call = name if failure is None else f"{name}: {failure}"
        if failure:
            result.causes.append(f"call: {failure}")
    result.pack = ",".join(f"{k}={v}" for k, v in sorted(packs.items())) or "empty"
    if not packs:
        result.causes.append("pack: nothing imported")
    result.result = "FAIL" if result.causes else "PASS"
    return result


def tui_interpreter(config: AcceptanceConfig, writer: ResultWriter) -> None:
    """One row for the declared ``tui_python`` interpreter: it must start."""
    if not config.tui_python:
        return
    try:
        done = subprocess.run(  # nosec B603 -- fixed argv from the operator's own file
            [config.tui_python, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        writer.row("tui", "interpreter", "-", "GAP", type(exc).__name__)
        return
    writer.row(
        "tui",
        "interpreter",
        "-",
        done.returncode == 0,
        done.stdout.strip() or "no output",
    )


async def sweep(arbora: Arbora, config: AcceptanceConfig, writer: ResultWriter) -> None:
    """Audit every selected fleet server; one row each."""
    tui_interpreter(config, writer)
    if not config.fleet_config:
        return
    servers = [
        name
        for name in fleet_servers(Path(config.fleet_config))
        if not config.connectors or name in config.connectors
    ]
    if not servers:
        writer.row(SCENARIO, "servers", "-", "GAP", "no server selected")
        return
    packs = await pack_states(arbora, servers)
    gate = asyncio.Semaphore(config.concurrency)

    async def one(server: str) -> FleetResult:
        async with gate:
            return await audit_server(arbora, server, packs.get(server, {}))

    for outcome in await asyncio.gather(*(one(s) for s in servers)):
        writer.row(
            SCENARIO,
            "connector",
            outcome.server,
            outcome.result,
            "; ".join(outcome.causes)
            or f"tools={outcome.tools} call={outcome.call} pack={outcome.pack}",
        )
