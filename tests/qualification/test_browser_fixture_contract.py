"""Pure hygiene contracts only; these do not execute installed/native fixtures."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import signal
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")


@pytest.fixture
def controller(monkeypatch):
    path = Path(__file__).parent / "browser" / "qualify.py"
    # Do not import or execute the process-boot helper in these pure contracts.
    monkeypatch.setitem(sys.modules, "test_real_boot", SimpleNamespace())
    original = list(sys.path)
    spec = importlib.util.spec_from_file_location("fixture_controller_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.path[:] = original


def test_browser_environment_never_inherits_credentials(
    controller, monkeypatch, tmp_path
):
    for name in (
        "QUALIFICATION_BROKER_TOKEN",
        "VAULT_TOKEN",
        "GITHUB_TOKEN",
        "DEBUG",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "fixture-private-value")
    monkeypatch.setenv("PATH", "/fixture/bin")
    env = controller.browser_environment(tmp_path, tmp_path / "ca.pem")
    assert set(env) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "PLAYWRIGHT_BROWSERS_PATH",
        "HOME",
        "QUALIFICATION_BROWSER_HOME",
        "NODE_EXTRA_CA_CERTS",
        "TMPDIR",
    }
    assert "fixture-private-value" not in env.values()
    assert env["HOME"] == str(tmp_path / "home")


def test_exited_browser_leader_still_reaps_owned_group(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller.os, "killpg", lambda pid, action: calls.append((pid, action))
    )
    process = SimpleNamespace(pid=123, poll=lambda: 0, wait=lambda **kw: 0)
    controller.terminate(process)
    assert calls == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_timed_out_browser_is_killed(controller, monkeypatch):
    calls = []

    def wait(**kwargs):
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("fixture", 5)
        return 0

    monkeypatch.setattr(
        controller.os, "killpg", lambda pid, action: calls.append(action)
    )
    controller.terminate(SimpleNamespace(pid=123, wait=wait))
    assert calls == [signal.SIGTERM, signal.SIGKILL]


def test_private_files_removed_even_when_termination_fails(
    controller, monkeypatch, tmp_path
):
    private = tmp_path / "private"
    private.mkdir()
    (private / "fixture.key").write_text("synthetic-private-placeholder")
    monkeypatch.setenv("QUALIFICATION_BROKER_TOKEN", "synthetic-private-placeholder")

    def fail(process):
        raise OSError("fixture termination failed")

    monkeypatch.setattr(controller, "terminate", fail)
    with pytest.raises(OSError):
        controller.cleanup(private, object())
    assert not private.exists()
    assert "QUALIFICATION_BROKER_TOKEN" not in controller.os.environ


def test_missing_process_group_is_safe(controller, monkeypatch):
    def absent(*args):
        raise ProcessLookupError

    monkeypatch.setattr(controller.os, "killpg", absent)
    controller.terminate(SimpleNamespace(pid=123, wait=lambda **kw: 0))


@pytest.mark.parametrize(
    "fault",
    [None, "missing", "duplicate", "algorithm", "hash", "size", "no_hash", "no_size"],
)
def test_native_executable_must_match_wheel_record(controller, tmp_path, fault):
    binary = tmp_path / "server"
    binary.write_bytes(b"synthetic executable bytes")
    digest = hashlib.sha256(binary.read_bytes()).digest()
    entry = SimpleNamespace(
        hash=SimpleNamespace(
            mode="sha256", value=base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        ),
        size=binary.stat().st_size,
    )
    if fault == "algorithm":
        entry.hash.mode = "md5"
    elif fault == "hash":
        entry.hash.value = "mismatch"
    elif fault == "size":
        entry.size += 1
    elif fault == "no_hash":
        entry.hash = None
    elif fault == "no_size":
        entry.size = None
    files = (
        []
        if fault == "missing"
        else [entry, entry]
        if fault == "duplicate"
        else [entry]
    )
    distribution = SimpleNamespace(files=files, locate_file=lambda item: binary)
    if fault is None:
        assert (
            controller.native.verify_server_record(binary, distribution) == digest.hex()
        )
    else:
        with pytest.raises(RuntimeError, match="Native server wheel"):
            controller.native.verify_server_record(binary, distribution)


@pytest.mark.parametrize("version", ["2.27.7", "2.27.6", None])
def test_native_runtime_version_is_exact(controller, version):
    async def health():
        return {"version": version}

    client = SimpleNamespace(health=health)
    if version == "2.27.7":
        assert asyncio.run(controller.native.require_native_version(client)) == version
    else:
        with pytest.raises(RuntimeError, match="Native runtime version"):
            asyncio.run(controller.native.require_native_version(client))


@pytest.mark.parametrize("code", ["FORBIDDEN", "private-token-value", None])
def test_refusal_metadata_never_copies_unknown_codes_or_details(controller, code):
    exc = RuntimeError("private diagnostic detail")
    exc.code = code
    assert controller.safe_refusal_code(exc) == (
        "FORBIDDEN" if code == "FORBIDDEN" else None
    )


def test_prepare_failure_stage_reaches_public_receipt(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("QUALIFICATION_OUTPUT", str(tmp_path / "public"))
    monkeypatch.setenv("QUALIFICATION_VAULT_BINARY", "/unused-fixture-tool")
    monkeypatch.setattr(controller.native, "require_installed", lambda: {})
    monkeypatch.setattr(controller, "browser_ca", lambda path: (path, path, path))
    monkeypatch.setattr(controller, "configure_signer", lambda *args: "synthetic")
    monkeypatch.setitem(
        sys.modules,
        "arbora.security.secrets",
        SimpleNamespace(VaultBackend=lambda **kw: object()),
    )

    @contextmanager
    def infrastructure(*args):
        yield SimpleNamespace(url="https://fixture.invalid")

    @contextmanager
    def boot(**kwargs):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("{}")
        kwargs["before_serve"](
            {
                "env": {},
                "config_dir": config_dir,
                "issuer": SimpleNamespace(
                    _key=SimpleNamespace(as_dict=lambda **kw: {})
                ),
            }
        )
        yield

    async def refused(context, *args):
        context["fixture_step"] = "native_fixture_refusal"
        raise RuntimeError("private diagnostic must not escape")

    monkeypatch.setattr(controller, "vault", infrastructure)
    monkeypatch.setattr(
        controller,
        "https_origin",
        lambda *args, **kwargs: nullcontext("https://fixture.invalid"),
    )
    monkeypatch.setattr(controller, "prepare", refused)
    monkeypatch.setattr(controller.boot, "_free_port", lambda: 12345, raising=False)
    monkeypatch.setattr(controller.boot, "ROLES", (), raising=False)
    monkeypatch.setattr(controller.boot, "_booted_stack", boot, raising=False)
    with pytest.raises(RuntimeError, match="Qualification failed"):
        controller.main()
    receipt = json.loads((tmp_path / "public" / "receipt.json").read_text())
    assert receipt["fixture_step"] == "native_fixture_refusal"
    assert "private diagnostic" not in json.dumps(receipt)
    assert not (tmp_path / "browser-private").exists()


@pytest.mark.parametrize("refuse", [False, True])
def test_runtime_attestation_uses_separate_exact_capability(
    controller, monkeypatch, refuse
):
    calls = []

    async def health():
        calls.append("health")
        if refuse:
            raise RuntimeError("synthetic refusal")
        return {"version": "2.27.7"}

    async def close():
        calls.append("close")

    async def connect(context, scope):
        calls.append(scope)
        return SimpleNamespace(health=health, close=close)

    monkeypatch.setattr(controller, "connect", connect)
    if refuse:
        with pytest.raises(RuntimeError):
            asyncio.run(controller.verify_runtime({}))
    else:
        assert asyncio.run(controller.verify_runtime({})) == "2.27.7"
    assert calls == ["service:control", "health", "close"]


@pytest.mark.parametrize(
    "message",
    [
        "private-token-secret",
        "configured issuer signing key unavailable: private-token-secret",
        "configured issuer signing key unavailable",
    ],
)
def test_failure_reason_only_accepts_exact_constant(controller, message):
    reason = controller.safe_failure_reason(RuntimeError(message))
    assert reason == (
        "issuer-key-unavailable"
        if message == "configured issuer signing key unavailable"
        else "unknown"
    )


def test_fixture_signer_exports_actual_private_key_for_production_issuer(controller):
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from arbora.identity.issuer import IssuerSettings, LocalIssuer

    stored = {}
    key = RSAKey.generate_key(2048, parameters={"kid": "ephemeral-test"})
    client = SimpleNamespace(
        secrets=SimpleNamespace(
            kv=SimpleNamespace(
                v2=SimpleNamespace(
                    create_or_update_secret=lambda **kw: stored.update(kw["secret"])
                )
            )
        ),
        sys=SimpleNamespace(create_or_update_policy=lambda *args: None),
        auth=SimpleNamespace(
            token=SimpleNamespace(
                create=lambda **kw: {"auth": {"client_token": "synthetic"}}
            )
        ),
    )
    controller.configure_signer(client, key)
    active = json.loads(stored["value"])["active"]
    assert active.get("d") and "is_private" not in active
    issuer = LocalIssuer(
        SimpleNamespace(get=lambda path: stored["value"]),
        IssuerSettings("https://fixture.invalid", "arbora", "fixture"),
        SimpleNamespace(resolve_credential=lambda value: None),
    )
    token = issuer._sign({"sub": "synthetic-human"})
    assert (
        jwt.decode(token, key, algorithms=["RS256"]).claims["sub"] == "synthetic-human"
    )


def test_method_observation_forwards_arguments_without_recording_them(controller):
    context = {}
    payload = {"private": "synthetic-secret"}
    seen = []

    async def send(*args, **kwargs):
        seen.append((args, kwargs))
        return payload

    async def connector(**kwargs):
        return SimpleNamespace(_send=send)

    async def run():
        client = await controller.observed_client(context, connector)
        assert (
            await client._send("GetRunWorkItem", payload, graph="__control__")
            is payload
        )
        assert context == {"native_method": "GetRunWorkItem"}
        await client._send("private-method-name", payload)
        assert context == {"native_method": "other"}

    asyncio.run(run())
    assert seen[0] == (("GetRunWorkItem", payload), {"graph": "__control__"})


@pytest.mark.parametrize(
    "detail",
    [
        "verified principal lacks Read access to graph '__control__'",
        "verified principal lacks Read access to graph 'private-secret'",
        "private-secret",
    ],
)
def test_native_detail_mapping_is_exact_and_redacted(controller, detail):
    exc = RuntimeError("private-secret")
    exc.detail = detail
    assert controller.safe_failure_reason(exc) == (
        "control-graph-read-refused"
        if detail == "verified principal lacks Read access to graph '__control__'"
        else "unknown"
    )


@pytest.mark.parametrize("denied", [False, True])
def test_only_seed_human_gets_explicit_sdk_negotiation_scope(
    controller, monkeypatch, denied
):
    operations = []

    async def operation(client, graph, request, kind):
        operations.append(request)
        if kind == "principal":
            return {"principal_id": "synthetic-human"}
        if kind == "user":
            return {
                "principal_id": "synthetic-human",
                "kind": "human",
                "is_bootstrap": False,
                "has_password": True,
            }
        return None

    monkeypatch.setattr(controller.native, "identity_operation", operation)
    asyncio.run(
        controller.native.provision_human(
            object(),
            "__control__",
            username="synthetic",
            password="synthetic",  # sanitizer:ignore - inert fake-operation test input, never authenticates
            tenant_graph="tenant__synthetic",
            seed_permissions=not denied,
            denied=denied,
        )
    )
    role = operations[0]["request"]
    if denied:
        assert role["scopes"] == ["identity:self"] and role["graph_grants"] == []
        assert any(
            op["op"] == "change_user_role" and op["request"]["change"] == "remove"
            for op in operations
        )
    else:
        assert set(role["scopes"]) == {
            "kg:read",
            "work:read",
            "work:submit",
            "work:claim",
            "work:write",
            "service:control",
        }
        assert all(
            grant["resource"]
            in ({"Graph": "__control__"}, {"Graph": "tenant__synthetic"})
            for grant in role["graph_grants"]
        )


@pytest.mark.parametrize(
    "presentation, expected",
    [
        ({"authenticated": False, "mode": "local", "private": "never-copy"}, True),
        ({"authenticated": True, "mode": "local"}, False),
        ({"authenticated": False, "mode": "other"}, False),
        (["never-copy"], False),
    ],
)
def test_https_readiness_copies_only_boolean_facts(
    controller, monkeypatch, presentation, expected
):
    import io

    response = io.StringIO(json.dumps(presentation))
    response.status = 200
    monkeypatch.setattr(controller.urllib.request, "urlopen", lambda *a, **k: response)
    context = {}
    assert (
        controller.https_presentation_ready(
            "https://fixture.invalid", object(), context
        )
        is expected
    )
    observed = context["https_readiness"]
    assert set(observed) == {"outcome", "status", "signed_out", "local_mode"}
    assert observed["outcome"] == "response"
    assert "never-copy" not in json.dumps(context)


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("http", "http-error"),
        ("url", "url-error"),
        ("tls", "tls-error"),
        ("timeout", "timeout"),
        ("json", "invalid-json"),
    ],
)
def test_https_readiness_error_details_never_escape(
    controller, monkeypatch, kind, expected
):
    errors = {
        "http": controller.urllib.error.HTTPError(
            "https://private.invalid", 503, "private-secret", {}, None
        ),
        "url": controller.urllib.error.URLError("private-secret"),
        "tls": controller.urllib.error.URLError(
            controller.ssl.SSLError("private-secret")
        ),
        "timeout": TimeoutError("private-secret"),
        "json": ValueError("private-secret"),
    }

    def refused(*args, **kwargs):
        raise errors[kind]

    monkeypatch.setattr(controller.urllib.request, "urlopen", refused)
    context = {}
    assert not controller.https_presentation_ready(
        "https://private.invalid", object(), context
    )
    assert context["https_readiness"]["outcome"] == expected
    assert "private" not in json.dumps(context)
    assert set(context["https_readiness"]) <= {"outcome", "status"}


def test_readiness_timeout_tag_requires_exact_constant(controller):
    assert (
        controller.safe_failure_reason(TimeoutError("Trusted HTTPS sign-in readiness"))
        == "https-signin-readiness-timeout"
    )
    assert (
        controller.safe_failure_reason(
            TimeoutError("Trusted HTTPS sign-in readiness private-secret")
        )
        == "unknown"
    )


@pytest.fixture
def tls_fixture(controller, monkeypatch, tmp_path):
    import tempfile

    import infrastructure

    # No NSS/system trust changes: only test the emitted certificate profile.
    monkeypatch.setattr(infrastructure.subprocess, "run", lambda *a, **k: None)
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        yield controller.browser_ca(Path(directory))


def _strict_memory_handshake(paths, *, trusted=True, hostname="localhost"):
    import ssl

    ca, cert, key = paths
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    if trusted:
        client_context.load_verify_locations(cafile=str(ca))
    assert client_context.verify_mode == ssl.CERT_REQUIRED
    assert client_context.check_hostname
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for name, connection in (("client", client), ("server", server)):
            if name not in completed:
                try:
                    connection.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
        client_in.write(server_out.read())
        server_in.write(client_out.read())
        if len(completed) == 2:
            return
    raise AssertionError("In-memory TLS handshake did not complete")


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1"])
def test_fixture_certificates_pass_strict_tls(tls_fixture, hostname):
    _strict_memory_handshake(tls_fixture, hostname=hostname)


@pytest.mark.parametrize(
    "trusted, hostname", [(False, "localhost"), (True, "wrong.invalid")]
)
def test_fixture_tls_still_refuses_untrusted_or_wrong_hostname(
    tls_fixture, trusted, hostname
):
    import ssl

    with pytest.raises(ssl.SSLCertVerificationError):
        _strict_memory_handshake(tls_fixture, trusted=trusted, hostname=hostname)


def test_fixture_certificate_profile_is_constrained(tls_fixture):
    import ipaddress

    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_path, leaf_path, key_path = tls_fixture
    ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
    leaf = x509.load_pem_x509_certificate(leaf_path.read_bytes())
    for certificate in (ca, leaf):
        assert certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).critical
        assert certificate.extensions.get_extension_for_class(x509.KeyUsage).critical
        assert not certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).critical
        assert not certificate.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).critical
    ca_constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert ca_constraints.ca and ca_constraints.path_length == 0
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    ca_usage = ca.extensions.get_extension_for_class(x509.KeyUsage).value
    leaf_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ca_usage.key_cert_sign and ca_usage.crl_sign
    assert leaf_usage.digital_signature and leaf_usage.key_encipherment
    assert not leaf_usage.key_cert_sign and not leaf_usage.crl_sign
    assert list(
        leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    ) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert (
        leaf.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value.key_identifier
        == ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    )
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["localhost"]
    assert san.get_values_for_type(x509.IPAddress) == [
        ipaddress.ip_address("127.0.0.1")
    ]
    assert key_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("refused", "connection-refused"),
        ("timeout", "timeout"),
        ("http", "http-error"),
        ("os", "os-error"),
        ("value", "invalid-response"),
        ("other", "unknown"),
    ],
)
def test_proxy_diagnostic_is_fixed_category(controller, kind, expected):
    import http.client

    import infrastructure

    errors = {
        "refused": ConnectionRefusedError("private-detail"),
        "timeout": TimeoutError("private-detail"),
        "http": http.client.RemoteDisconnected("private-detail"),
        "os": OSError("private-detail"),
        "value": ValueError("private-detail"),
        "other": RuntimeError("private-detail"),
    }
    assert infrastructure.proxy_failure_category(errors[kind]) == expected


def test_startup_facts_never_copy_private_log_content(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        controller.boot, "_listening", lambda port: False, raising=False
    )
    (tmp_path / "arbora.log").write_text(
        "private-prefix co-service agent-webui started\n"
        "private-prefix co-service agent-webui crashed\n"
        "ImportError: private-credential-and-path\n"
        "PrivateException: private-credential-and-path\n"
    )
    result = controller.webui_startup_facts({"root": tmp_path}, 12345)
    assert result == {
        "listening": False,
        "log_available": True,
        "thread_started": True,
        "crashed": True,
        "restart_bound": False,
        "listener_announced": False,
        "exception_classes": ["ImportError"],
    }
    assert "private" not in json.dumps(result).lower()


def test_startup_facts_missing_log_has_no_path(controller, monkeypatch, tmp_path):
    monkeypatch.setattr(
        controller.boot, "_listening", lambda port: False, raising=False
    )
    assert controller.webui_startup_facts({"root": tmp_path}, 12345) == {
        "listening": False,
        "log_available": False,
    }
