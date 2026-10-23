"""Shared action-dispatch helpers: discovery, aliases, did-you-mean."""

from __future__ import annotations

import threading

import anyio
import pytest

from arbora.mcp_server.backing.action_dispatch import (
    DISCOVERY_ACTIONS,
    canonicalize,
    dispatch,
    dispatch_async,
    is_destructive_action,
    parse_json_object,
    public_actions,
    resolve_action,
    suggest,
    unknown_action_error,
)


class _Client:
    def get_movie(self, **kwargs):
        return {"called": "get_movie", "kwargs": kwargs}

    def add_movie(self, **kwargs):
        return {"called": "add_movie"}

    def _private(self):  # excluded from discovery
        return None


def test_public_actions_excludes_private():
    actions = public_actions(_Client())
    assert "get_movie" in actions and "add_movie" in actions
    assert "_private" not in actions


@pytest.mark.parametrize("value", [None, "", "   ", b"", bytearray()])
def test_parse_json_object_accepts_empty_values(value):
    assert parse_json_object(value) == {}


def test_parse_json_object_returns_object():
    assert parse_json_object('{"page": 2}') == {"page": 2}


@pytest.mark.parametrize("value", ['{"token": "private"', '["private"]'])
def test_parse_json_object_rejects_invalid_shape_without_echo(value):
    with pytest.raises(ValueError) as exc:
        parse_json_object(value)

    assert "private" not in str(exc.value)


def test_parse_json_object_rejects_oversized_input_before_parsing():
    with pytest.raises(ValueError, match="input limit"):
        parse_json_object("{" + " " * (64 * 1024) + "}")


def test_suggest_finds_close_match():
    assert "get_movie" in suggest("get_movei", ["get_movie", "add_movie"])


def test_canonicalize_plural_and_alias():
    valid = ["get_movie", "get_series"]
    assert canonicalize("get_movies", valid) == "get_movie"  # plural -> singular
    assert canonicalize("get_movie", valid) == "get_movie"  # identity
    assert canonicalize("films", valid, aliases={"films": "get_movie"}) == "get_movie"
    assert canonicalize("nope", valid) is None


def test_dispatch_discovery_returns_actions():
    for keyword in DISCOVERY_ACTIONS:
        res = dispatch(_Client(), keyword, {}, service="radarr")
        assert res["service"] == "radarr"
        assert "get_movie" in res["actions"]


def test_dispatch_resolves_plural_and_calls():
    res = dispatch(_Client(), "get_movies", {"tmdbId": 1}, service="radarr")
    assert res == {"called": "get_movie", "kwargs": {"tmdbId": 1}}


def test_dispatch_unknown_raises_rich_error():
    with pytest.raises(ValueError) as exc:
        dispatch(_Client(), "get_movei", {}, service="radarr")
    msg = str(exc.value)
    assert "list_actions" in msg and "get_movie" in msg


def test_dispatch_result_coercer_applied():
    class _Model:
        def model_dump(self):
            return {"dumped": True}

    class _C:
        def get(self, **kwargs):
            return _Model()

    res = dispatch(_C(), "get", {}, result_coercer=lambda r: r.model_dump())
    assert res == {"dumped": True}


def test_resolve_action_for_explicit_dispatch():
    valid = ["list_workflows", "list_runs", "get_run"]
    # discovery
    disc = resolve_action("list_actions", valid, service="actions")
    assert disc == {"service": "actions", "actions": sorted(valid)}
    # canonical passthrough
    assert resolve_action("list_runs", valid) == "list_runs"
    # unknown -> rich error
    with pytest.raises(ValueError) as exc:
        resolve_action("list_run", valid, service="actions")
    assert "Did you mean" in str(exc.value)


def test_unknown_action_error_without_matches():
    err = unknown_action_error("zzzzz", ["get_movie"], target="radarr")
    assert "Unknown action 'zzzzz' on radarr" in str(err)
    assert "list_actions" in str(err)


def test_dispatch_folds_flat_fields_into_body_param():
    """dispatch() self-heals the REST-body convention like run_blocking does."""

    class _JiraLike:
        def create_issue(self, update_history=None, payload=None):
            return {"update_history": update_history, "payload": payload}

        def get_all_projects(self):
            return ["AU", "KAN"]

    c = _JiraLike()
    # LLM's flat fields folded into the single `payload` body param
    res = dispatch(c, "create_issue", {"fields": {"summary": "S"}}, service="jira")
    assert res == {"update_history": None, "payload": {"fields": {"summary": "S"}}}
    # read path (no body param) is a strict no-op
    assert dispatch(c, "get_all_projects", {}, service="jira") == ["AU", "KAN"]


def test_dispatch_async_offloads_sync_method():
    main_thread = threading.get_ident()

    class _SyncClient:
        def get_record(self):
            return {"thread": threading.get_ident()}

    result = anyio.run(dispatch_async, _SyncClient(), "get_record")

    assert result["thread"] != main_thread


def test_dispatch_async_awaits_native_method_and_async_coercer():
    class _AsyncClient:
        async def get_record(self, *, record_id):
            return {"record_id": record_id}

    async def _coerce(result):
        return {"coerced": result}

    async def _run():
        return await dispatch_async(
            _AsyncClient(),
            "get_record",
            {"record_id": "abc"},
            result_coercer=_coerce,
        )

    result = anyio.run(_run)

    assert result == {"coerced": {"record_id": "abc"}}


@pytest.mark.parametrize("action", ["delete_record", "projects_delete"])
def test_is_destructive_action_matches_exact_tokens(action):
    assert is_destructive_action(action) is True


def test_is_destructive_action_prefers_operation_metadata():
    assert is_destructive_action("undelete_record") is False
    assert is_destructive_action("get_record", {"http": "DELETE"}) is True
    assert is_destructive_action("delete_record", {"destructive": False}) is False


class _ElicitResult:
    action = "decline"
    data = True


class _DecliningContext:
    def __init__(self):
        self.asked = []

    async def elicit(self, message, response_type=bool):
        self.asked.append(message)
        return _ElicitResult()


def test_dispatch_async_requires_confirmation_before_destructive_invocation():
    class _TrackedClient:
        def __init__(self):
            self.called = []

        async def delete_record(self):
            self.called.append("delete_record")

    client = _TrackedClient()
    ctx = _DecliningContext()

    async def _run():
        return await dispatch_async(client, "delete_record", ctx=ctx)

    result = anyio.run(_run)

    assert result == {"cancelled": True, "operation": "delete_record"}
    assert client.called == []
    assert len(ctx.asked) == 1


def test_dispatch_async_denies_headless_destructive_invocation():
    class _DeleteClient:
        async def delete_record(self):
            raise AssertionError("destructive method must not run")

    result = anyio.run(dispatch_async, _DeleteClient(), "delete_record")

    assert result == {"cancelled": True, "operation": "delete_record"}


@pytest.mark.parametrize(
    ("target", "args", "kwargs", "expected"),
    [
        (lambda data=None: None, (1,), {"field": 2}, None),
        (lambda data=None: None, (), {}, None),
        (lambda data=None, **extras: None, (), {"field": 2}, None),
        (lambda item=None: None, (), {"field": 2}, None),
        (lambda data=None, payload=None: None, (), {"field": 2}, None),
        (lambda data=None: None, (), {"data": None, "field": 2}, None),
        (lambda item=None, data=None: None, (), {"item": 1}, None),
        (
            lambda item=None, data=None: None,
            (),
            {"item": 1, "field": 2},
            {"item": 1, "data": {"field": 2}},
        ),
        (
            lambda *, item=None, payload=None: None,
            (),
            {"item": 1, "field": 2},
            {"item": 1, "payload": {"field": 2}},
        ),
        (lambda body=None: None, (), {"field": 2}, {"body": {"field": 2}}),
        (lambda data=None, /: None, (), {"field": 2}, {"data": {"field": 2}}),
        (lambda item=None, /, data=None: None, (), {"item": 1}, {"data": {"item": 1}}),
        (lambda *items, data=None: None, (), {"items": 1}, {"data": {"items": 1}}),
    ],
)
def test_body_normalization_characterization(target, args, kwargs, expected):
    from arbora.mcp_server.backing.concurrency import _wrap_data_kwargs

    original = kwargs.copy()
    result = _wrap_data_kwargs(target, args, kwargs)
    assert kwargs == original
    if expected is None:
        assert result is kwargs
    else:
        assert result == expected
        assert result is not kwargs


@pytest.mark.parametrize("error", [TypeError, ValueError])
def test_body_normalization_uninspectable_callable_is_noop(monkeypatch, error):
    from arbora.mcp_server.backing import concurrency

    def unavailable(_):
        raise error("uninspectable")

    monkeypatch.setattr(concurrency.inspect, "signature", unavailable)
    kwargs = {"field": object()}
    assert concurrency._wrap_data_kwargs(object(), (), kwargs) is kwargs


def test_body_normalization_short_circuits_before_signature(monkeypatch):
    from arbora.mcp_server.backing import concurrency

    def unexpected(_):
        raise AssertionError("signature inspected before positional/empty guard")

    monkeypatch.setattr(concurrency.inspect, "signature", unexpected)
    kwargs = {"field": object()}
    assert concurrency._wrap_data_kwargs(object(), (1,), kwargs) is kwargs
    empty = {}
    assert concurrency._wrap_data_kwargs(object(), (), empty) is empty


@pytest.mark.parametrize("native_async", [False, True])
def test_body_normalization_dispatch_alias_and_value_identity(native_async):
    class Client:
        def get_record(self, *, item, payload):
            return item, payload

    class AsyncClient:
        async def get_record(self, *, item, payload):
            return item, payload

    value = object()
    kwargs = {"item": 7, "field": value}
    aliases = {"record": "get_record"}

    async def run():
        client = AsyncClient() if native_async else Client()
        return await dispatch_async(client, "record", kwargs, aliases=aliases)

    result = anyio.run(run)
    assert result == (7, {"field": value})
    assert result[1]["field"] is value
    assert dispatch(Client(), "record", kwargs, aliases=aliases) == result
    assert kwargs == {"item": 7, "field": value}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_body_normalization_keeps_explicit_body_typeerror(asynchronous):
    class Client:
        def get_record(self, payload=None):
            raise AssertionError("invalid arguments must refuse before invocation")

    async def run():
        return await dispatch_async(Client(), "get_record", {"payload": {}, "stray": 1})

    with pytest.raises(TypeError, match="unexpected keyword argument 'stray'"):
        if asynchronous:
            anyio.run(run)
        else:
            dispatch(Client(), "get_record", {"payload": {}, "stray": 1})
