"""One loopback engine harness for the served deployment rows.

Rows ARBORA-DEPLOY-R013.5, R013.7, R009.2.2, R009.3 and R017.17 each drive the
real invoke pipeline over HTTP against a recorded engine. This module is that
shared stack: the served ASGI app, in-memory plan leases and effect journal, a
bound step pipeline that records every governed ``fleet.call``, and engine
handlers keyed by method id. Import the fixtures into a test module with
``from tests.api._loopback_engine import loopback  # noqa: F401``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arbora.api.invoke.steps import OpResult
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import _call, _value

KEY = "Idempotency-Key"
PLAN = "Arbora-Plan-Ref"
PRINCIPAL = "person:one"

Handler = Callable[[dict[str, Any], str], Any]


class StepPipeline:
    """The bound step pipeline: records each governed fleet call and answers it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.answers: dict[str, Any] = {}
        # ``journal_effects`` makes the pipeline answer a repeated idempotency
        # key from its first outcome, as the effect journal does: ``effects``
        # then holds each governed call once.
        self.journal_effects = False
        self.effects: list[tuple[str, dict[str, Any], str]] = []
        self.journal: dict[str, Any] = {}

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        call = (params["tool"], dict(params.get("arguments") or {}), key)
        self.calls.append(call)
        if key in self.journal:
            return self.journal[key]
        outcome = self._answer(params)
        if self.journal_effects:
            self.effects.append(call)
            self.journal[key] = outcome
        return outcome

    def _answer(self, params: Any) -> Any:
        answer = self.answers.get(params["tool"])
        if callable(answer):
            return answer(params)
        return answer if answer is not None else OpResult({"value": "done"})

    @property
    def tools(self) -> list[str]:
        return [tool for tool, _, _ in self.calls]


class Loopback:
    """The served app, its recorded engine and the bound step pipeline."""

    def __init__(
        self, app: Any, client: Any, steps: StepPipeline, mp: pytest.MonkeyPatch
    ) -> None:
        self.app, self.client, self.steps, self.monkeypatch = app, client, steps, mp
        self.engine_calls: list[tuple[str, dict[str, Any], str]] = []
        self.deploy_root = Path()
        self.config: dict[str, Any] = {}
        self.captured: dict[str, Any] = {}
        self.bindings: dict[str, Any] = {}

    @property
    def invocation(self) -> Any:
        """The served invocation, for a test that also enters over MCP."""
        return self.captured["invocation"]

    def serve(self, handlers: dict[str, Handler], *scopes: str) -> None:
        """Route these engine methods to ``handlers`` as a verified operator."""
        one = person("one")
        recorded: dict[str, Handler] = {}
        for method, handler in handlers.items():

            def record(
                params: dict[str, Any],
                principal: str,
                m: str = method,
                h: Handler = handler,
            ) -> Any:
                self.engine_calls.append((m, params, principal))
                return h(params, principal)

            recorded[method] = record
        route_methods(
            self.client,
            recorded,
            self.monkeypatch,
            {
                "fixture-verified": replace(
                    one, roles=(*one.roles, "fleet:control", *scopes)
                )
            },
        )

    def serve_unreleased(
        self, module: Any, handlers: dict[str, Handler], *models: str
    ) -> None:
        """Serve methods and shapes the pinned engine lacks (PIN-LAG).

        The module's ``engine_send`` is replaced by a recorded dispatch over
        ``handlers``; ``models`` are declared so the pin-lag guard passes. The
        operation under test and the invoke pipeline stay real.
        """
        import medulla.generated as generated
        from medulla.generated import models as generated_models

        declared = frozenset(generated.METHOD_IDS) | frozenset(handlers)
        self.monkeypatch.setattr(generated, "METHOD_IDS", declared)
        for name in models:
            self.monkeypatch.setattr(generated_models, name, object, raising=False)

        async def send(
            context: Any,
            method: str,
            params: dict[str, Any],
            *,
            idempotent: bool = False,
        ) -> Any:
            self.engine_calls.append((method, params, str(context.caller.principal)))
            return handlers[method](params, str(context.caller.principal))

        self.monkeypatch.setattr(module, "engine_send", send)
        self.serve({})

    def serve_decide(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        """Answer every typed ``Decide`` question from ``handler`` (recorded)."""
        from medulla.decision_client import DecisionClient

        async def decide(
            _client: Any, request: dict[str, Any], *a: Any, **k: Any
        ) -> Any:
            self.engine_calls.append(("Decide", request, PRINCIPAL))
            return handler(request)

        self.monkeypatch.setattr(DecisionClient, "decide", decide)
        self.serve({})

    def call(self, op: str, params: dict[str, Any], key: str, plan: str = "") -> Any:
        headers = {KEY: key, **({PLAN: plan} if plan else {})}
        return _call(self.app, op, params, **headers)

    def preview(self, op: str, params: dict[str, Any], key: str) -> str:
        shown = self.call(op, params, key)
        assert shown.json()["error"]["code"] == "CONFIRMATION_REQUIRED", shown.text
        return shown.json()["error"]["details"]["plan_ref"]

    def confirmed(self, op: str, params: dict[str, Any], key: str) -> Any:
        return self.call(op, params, key, self.preview(op, params, key))

    @staticmethod
    def value(response: Any) -> Any:
        return _value(response)


def _module_bindings(module: Any) -> dict[str, Any]:
    return getattr(module, "loopback_bindings", dict)()


@pytest.fixture
def loopback(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Loopback:
    import tests.test_served_operation_api as harness

    steps = StepPipeline()
    config: dict[str, Any] = {}
    deploy_root = tmp_path / "deploy"
    (deploy_root / "compose").mkdir(parents=True)
    (deploy_root / "compose" / "compose.yaml").write_text("services: {}\n")
    compose = harness.compose_served_invocation
    # A test module may bind more services (a plan engine, a model) by defining
    # ``loopback_bindings() -> dict``; the served stack composes them in.
    extra = _module_bindings(request.module)

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {
            **kwargs["bindings"],
            **extra,
            "plan_step_invoke": steps,
            "deploy_config": config,
            "deploy_root": deploy_root,
            "deploy_feature_schema": {
                "component_id": "deploy.feature_schema",
                "definition_digest": "sha256:" + "0" * 64,
                "kind": "feature_schema",
            },
        }
        return compose(**kwargs)

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    captured = confirmable_invocation(monkeypatch)
    # The served app composes its invocation on first use, after the patches.
    app, client = request.getfixturevalue("served")
    made = Loopback(app, client, steps, monkeypatch)
    made.deploy_root = deploy_root
    made.config = config
    made.captured = captured
    made.bindings = extra
    return made
