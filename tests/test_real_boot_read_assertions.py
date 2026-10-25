"""Source checks for acceptance guards, not native serving qualification."""

import pytest

from tests.test_real_boot import _assert_native_read_refusal

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.7")


@pytest.mark.parametrize(
    "code",
    [
        "UNAUTHENTICATED",
        "ACCESS_DENIED",
        "SUBJECT_ACCESS_DENIED",
        "PRINCIPAL_NOT_ALLOWED",
        "SCOPE_REQUIRED",
        "POLICY_DENIED",
    ],
)
def test_only_explicit_authority_refusals_qualify(code):
    _assert_native_read_refusal({"ok": False, "error": {"code": code}})


@pytest.mark.parametrize(
    "code",
    [
        "INTERNAL",
        "INTERNAL_ERROR",
        "TIMEOUT",
        "UNKNOWN_OP",
        "UNAVAILABLE",
        "POLICY_UNAVAILABLE",
        "INDETERMINATE",
        "RATE_LIMITED",
    ],
)
def test_backend_failures_do_not_qualify_as_isolation(code):
    with pytest.raises(AssertionError):
        _assert_native_read_refusal({"ok": False, "error": {"code": code}})


def test_explicit_unavailable_read_qualifies():
    _assert_native_read_refusal(
        {"ok": True, "result": {"value": {"availability": "unavailable"}}}
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"ok": None},
        {"ok": True, "result": {"value": {"availability": "available"}}},
        {"ok": False, "error": {"code": "ACCESS_DENIED", "detail": "PRIVATE"}},
    ],
)
def test_missing_envelope_or_payload_leak_does_not_qualify(value):
    with pytest.raises(AssertionError):
        _assert_native_read_refusal(value)


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_mcp_handshake_only_accepts_authentication_status(monkeypatch, status):
    import contextlib

    import httpx2

    from tests import test_real_boot as boot

    response = httpx2.Response(
        status, request=httpx2.Request("POST", "http://fixture/mcp"), text="refused"
    )

    @contextlib.asynccontextmanager
    async def rejected():
        raise httpx2.HTTPStatusError(
            "refused", request=response.request, response=response
        )
        yield  # pragma: no cover - async context manager protocol

    async def session(stack):
        return rejected()

    monkeypatch.setattr(boot, "_mcp_session", session)
    if status in (401, 403):
        await boot._check_foreign_native_mcp({}, {})
    else:
        with pytest.raises(AssertionError):
            await boot._check_foreign_native_mcp({}, {})


async def test_mcp_transport_failure_is_not_swallowed(monkeypatch):
    import contextlib

    import httpx2

    from tests import test_real_boot as boot

    @contextlib.asynccontextmanager
    async def broken():
        raise httpx2.ConnectError("connection failed")
        yield  # pragma: no cover - async context manager protocol

    async def session(stack):
        return broken()

    monkeypatch.setattr(boot, "_mcp_session", session)
    with pytest.raises(httpx2.ConnectError):
        await boot._check_foreign_native_mcp({}, {})
