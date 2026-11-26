"""The acceptance runner: an MCP client of the gateway driven by one declared file.

``arbora acceptance run --config acceptance.yml [--scenario NAME]`` talks to the
gateway the way any client does (streamable HTTP MCP) and writes one JSON document
of rows plus a markdown report. Every input is a field of the config file; the
bearer is a secret reference that is resolved again whenever it nears expiry, so
no token is read from the environment and none is printed. Exit 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import ssl
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import yaml
from scion_sdk.credentials.references import SecretReferenceError
from scion_sdk.credentials.resolution import resolve_secret_reference
from scion_sdk.credentials.resolver import CredentialUnavailableError

#: Results a row may carry. GAP is an environment gap, never a pass.
RESULTS = ("PASS", "FAIL", "GAP", "SKIP")
_REQUIRED = frozenset({"gateway_url", "ca_bundle", "token"})
_OPTIONAL = frozenset(
    {"connectors", "concurrency", "output", "tui_python", "scenarios", "fleet_config"}
)
DEFAULT_CONCURRENCY = 4
_REFRESH_MARGIN_SECONDS = 60


class AcceptanceConfigError(ValueError):
    """The acceptance file is not valid; the message names the field."""


@dataclass(frozen=True)
class AcceptanceConfig:
    """The closed schema of ``acceptance.yml``."""

    gateway_url: str
    ca_bundle: str
    token: str
    connectors: tuple[str, ...] = ()
    concurrency: int = DEFAULT_CONCURRENCY
    output: str = "acceptance.json"
    tui_python: str | None = None
    scenarios: str | None = None
    fleet_config: str | None = None

    @classmethod
    def load(cls, path: Path) -> AcceptanceConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AcceptanceConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise AcceptanceConfigError("acceptance file must be a mapping")
        unknown = sorted(set(raw) - _REQUIRED - _OPTIONAL)
        if unknown:
            raise AcceptanceConfigError(f"unknown fields: {', '.join(unknown)}")
        missing = sorted(_REQUIRED - set(raw))
        if missing:
            raise AcceptanceConfigError(f"missing fields: {', '.join(missing)}")
        for name in ("gateway_url", "ca_bundle", "token"):
            if not isinstance(raw[name], str) or not raw[name].strip():
                raise AcceptanceConfigError(f"{name} must be a non-empty string")
        connectors = raw.get("connectors") or []
        if not isinstance(connectors, list) or not all(
            isinstance(item, str) for item in connectors
        ):
            raise AcceptanceConfigError("connectors must be a list of strings")
        concurrency = raw.get("concurrency", DEFAULT_CONCURRENCY)
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            raise AcceptanceConfigError("concurrency must be a positive integer")
        for name in ("output", "tui_python", "scenarios", "fleet_config"):
            if name in raw and not isinstance(raw[name], str):
                raise AcceptanceConfigError(f"{name} must be a string")
        return cls(
            gateway_url=raw["gateway_url"].rstrip("/"),
            ca_bundle=raw["ca_bundle"],
            token=raw["token"],
            connectors=tuple(connectors),
            concurrency=concurrency,
            output=raw.get("output", "acceptance.json"),
            tui_python=raw.get("tui_python"),
            scenarios=raw.get("scenarios"),
            fleet_config=raw.get("fleet_config"),
        )


@dataclass
class Row:
    """One check: scenario, check, connector, PASS/FAIL/GAP/SKIP and its evidence."""

    scenario: str
    check: str
    connector: str
    result: str
    evidence: str
    data: dict[str, Any] = field(default_factory=dict)


class ResultWriter:
    """Collect rows and write the JSON document and the markdown report."""

    def __init__(self, echo: Callable[[str], None] = print) -> None:
        self.rows: list[Row] = []
        self._echo = echo

    def row(
        self,
        scenario: str,
        check: str,
        connector: str,
        result: str | bool,
        evidence: str,
        **data: Any,
    ) -> bool:
        if isinstance(result, bool):
            result = "PASS" if result else "FAIL"
        if result not in RESULTS:
            raise ValueError(f"unknown result {result!r}")
        row = Row(scenario, check, connector, result, evidence[:400], data)
        self.rows.append(row)
        self._echo(
            f"{row.result:4}  {scenario:<14} {check:<34} {connector:<20} {row.evidence}"
        )
        return result == "PASS"

    def counts(self) -> dict[str, int]:
        return {name: sum(r.result == name for r in self.rows) for name in RESULTS}

    @property
    def failed(self) -> bool:
        return any(r.result == "FAIL" for r in self.rows)

    def summary(self, base: str, started: float) -> dict[str, Any]:
        counts = self.counts()
        return {
            "base": base,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "seconds": round(time.time() - started, 1),
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "gaps": counts["GAP"],
            "skipped": counts["SKIP"],
            "rows": [asdict(r) for r in self.rows],
        }

    def write(self, output: Path, base: str, started: float) -> dict[str, Any]:
        summary = self.summary(base, started)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        output.with_suffix(".md").write_text(self.markdown(), encoding="utf-8")
        return summary

    def markdown(self) -> str:
        counts = self.counts()
        lines = [
            f"acceptance: {counts['PASS']} pass, {counts['FAIL']} fail, "
            f"{counts['GAP']} gap, {counts['SKIP']} skip",
            "",
            "| scenario | check | connector | result | evidence |",
            "|---|---|---|---|---|",
        ]
        for r in self.rows:
            evidence = r.evidence.replace("|", "\\|")
            lines.append(
                f"| {r.scenario} | {r.check} | {r.connector} | {r.result} | {evidence} |"
            )
        return "\n".join(lines) + "\n"


def excerpt(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return " ".join(text.split())[:limit]


def token_expiry(token: str, now: float) -> float:
    """The ``exp`` claim of a JWT, or two minutes ahead when it carries none."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return now + 120


class RefreshingBearer(httpx2.Auth):
    """Resolve the bearer again before it lapses (access tokens are short-lived)."""

    def __init__(
        self, mint: Callable[[], str], clock: Callable[[], float] = time.time
    ) -> None:
        self._mint = mint
        self._clock = clock
        self._token: str | None = None
        self._exp = 0.0

    def current(self) -> str:
        now = self._clock()
        if now > self._exp - _REFRESH_MARGIN_SECONDS:
            self._token = self._mint()
            self._exp = token_expiry(self._token, now)
        return self._token or ""

    def auth_flow(self, request: Any) -> Any:
        request.headers["Authorization"] = f"Bearer {self.current()}"
        yield request


def secret_minter(reference: str) -> Callable[[], str]:
    """A mint function resolving ``reference``; it never returns or logs a value on error."""

    def mint() -> str:
        try:
            value = str(resolve_secret_reference(reference) or "").strip()
        except (SecretReferenceError, CredentialUnavailableError) as exc:
            raise AcceptanceConfigError(
                f"token reference does not resolve ({type(exc).__name__})"
            ) from exc
        if not value:
            raise AcceptanceConfigError("token reference resolved to an empty value")
        return value

    return mint


def err(envelope: dict[str, Any]) -> str:
    error = envelope.get("error")
    if isinstance(error, dict):
        return f"{error.get('code')}: {excerpt(error.get('message', ''), 160)}"
    return excerpt(error, 160) if error else "none"


def ok(envelope: dict[str, Any]) -> bool:
    return envelope.get("ok") is True


def unwrap(envelope: dict[str, Any]) -> Any:
    """The operation value: ``result``, then any ``{"value": ...}`` wrapper."""
    value = envelope.get("result")
    while (
        isinstance(value, dict) and set(value) <= {"value", "meta"} and "value" in value
    ):
        value = value["value"]
    return value


class Arbora:
    """One MCP session against the gateway plus its bearer."""

    def __init__(self, config: AcceptanceConfig, mint: Callable[[], str]) -> None:
        self.config = config
        self.auth = RefreshingBearer(mint)
        self.session: Any = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Arbora:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._stack = AsyncExitStack()
        context = ssl.create_default_context(cafile=self.config.ca_bundle)
        client = httpx2.AsyncClient(
            verify=context,
            auth=self.auth,
            timeout=httpx2.Timeout(600, connect=15),
            follow_redirects=False,
        )
        await self._stack.enter_async_context(client)
        streams = await self._stack.enter_async_context(
            streamable_http_client(f"{self.config.gateway_url}/mcp", http_client=client)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(streams[0], streams[1])
        )
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def verb(self, name: str, **arguments: Any) -> dict[str, Any]:
        """Call one intent tool and return its structured envelope."""
        arguments = {k: v for k, v in arguments.items() if v is not None}
        result = await self.session.call_tool(name, arguments)
        payload = (
            result.structuredContent
            if isinstance(result.structuredContent, dict)
            else None
        )
        if payload is None:
            for content in result.content:
                if getattr(content, "type", "") == "text":
                    try:
                        payload = json.loads(content.text)
                    except ValueError:
                        payload = {
                            "ok": False,
                            "error": {
                                "code": "non-json",
                                "message": content.text[:200],
                            },
                        }
                    break
        payload = payload or {}
        if result.isError and payload.get("ok") is not False:
            payload = {
                "ok": False,
                "error": {
                    "code": "isError",
                    "message": excerpt(payload or result.content),
                },
            }
        return payload

    async def op(
        self, verb: str, op: str, params: dict[str, Any], idempotent: bool = False
    ) -> dict[str, Any]:
        return await self.verb(
            verb,
            op=op,
            params=params,
            idempotency_key=uuid.uuid4().hex if idempotent else None,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arbora acceptance")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="run the declared scenarios and the fleet sweep"
    )
    run.add_argument("--config", required=True, help="path of acceptance.yml")
    run.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only this scenario (repeatable)",
    )
    return parser


async def run_all(
    config: AcceptanceConfig,
    writer: ResultWriter,
    only: list[str],
    mint: Callable[[], str],
) -> None:
    """Run the scenario files and then the connector sweep against one session."""
    from .fleet_audit import sweep
    from .scenarios import load_scenarios, run_scenario

    scenarios = load_scenarios(Path(config.scenarios)) if config.scenarios else {}
    unknown = sorted(set(only) - set(scenarios))
    if unknown:
        raise AcceptanceConfigError(f"unknown scenario: {', '.join(unknown)}")
    async with Arbora(config, mint) as arbora:
        for name, scenario in scenarios.items():
            if only and name not in only:
                continue
            await run_scenario(scenario, arbora.op, writer, config.concurrency)
        if not only:
            await sweep(arbora, config, writer)


def main(argv: list[str] | None = None, mint: Callable[[], str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = AcceptanceConfig.load(Path(args.config))
        writer = ResultWriter()
        started = time.time()
        asyncio.run(
            run_all(
                config, writer, list(args.scenario), mint or secret_minter(config.token)
            )
        )
    except AcceptanceConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    summary = writer.write(Path(config.output), config.gateway_url, started)
    print(
        f"acceptance: {summary['passed']} pass, {summary['failed']} fail, "
        f"{summary['gaps']} gap, {summary['skipped']} skip -> {config.output}"
    )
    return 1 if writer.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
