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


def test_browser_diagnostic_module_rejects_untrusted_values():
    """Execute the real pure JS recorder, without a browser or credentials."""
    module = Path(__file__).parent / "browser" / "browser_diagnostics.mjs"
    script = r"""
const {createDiagnostics} = await import(process.argv[1]);
const d = createDiagnostics();
d.stage('username-field');
d.status('navigation', 200);
d.flag('page_error', true);
d.stage('private-stage-token');
d.status('private-key', 401);
d.status('login', 'private-body-token');
d.status('operation', 600);
d.status('session', {secret: 'private-body-token'}); // # sanitizer:ignore - inert diagnostic test object, never authenticates
d.flag('authenticated', 'private-cookie-token');
d.flag('private-key', true);
const first = d.snapshot({name:'private-error-token', message:'private-message-token'});
d.stage('canonical-task-get');
d.status('operation', 403);
d.flag('authenticated', false);
const second = d.snapshot({name:'TimeoutError', message:'private-message-token'});
console.log(JSON.stringify([first, second]));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stderr == ""
    assert "private" not in result.stdout
    first, second = json.loads(result.stdout)
    assert first == {
        "stage": "username-field",
        "journey": "preflight",
        "error_class": "Error",
        "observations": {"navigation": 200, "page_error": True},
    }
    assert second == {
        "stage": "canonical-task-get",
        "journey": "preflight",
        "error_class": "TimeoutError",
        "observations": {
            "navigation": 200,
            "page_error": True,
            "operation": 403,
            "authenticated": False,
        },
    }


def test_browser_diagnostic_artifact_and_module_are_explicitly_allowlisted():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/installed-browser.yml").read_text()
    assert "cp tests/qualification/browser/browser_diagnostics.mjs " in workflow
    assert "browser-public/browser-diagnostics.json" in workflow
    browser = (Path(__file__).parent / "browser/browser.mjs").read_text()
    assert 'from "./browser_diagnostics.mjs"' in browser
    assert "diagnostics.snapshot(error, phase)" in browser
    assert "${safe.stage} (${safe.error_class})" in browser
    assert "${error?.name" not in browser


def test_browser_action_stages_are_recognized_by_the_real_recorder():
    import re

    directory = Path(__file__).parent / "browser"
    probe = (directory / "browser.mjs").read_text()
    stages = re.findall(r'diagnostics\.stage\("([a-z-]+)"\)', probe)
    assert len(set(stages)) >= 35
    script = r"""
const {createDiagnostics} = await import(process.argv[1]);
const d = createDiagnostics();
const stages = JSON.parse(process.argv[2]);
console.log(JSON.stringify(stages.map(stage => {
  d.stage(stage);
  return d.snapshot(new Error()).stage === stage;
})));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            (directory / "browser_diagnostics.mjs").resolve().as_uri(),
            json.dumps(stages),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert all(json.loads(result.stdout))


def test_browser_diagnostics_reset_attempts_and_read_error_name_once():
    module = Path(__file__).parent / "browser/browser_diagnostics.mjs"
    script = r"""
const {createDiagnostics} = await import(process.argv[1]);
const d = createDiagnostics();
d.stage('authenticated-session');
d.status('login', 200);
d.status('session', 200);
d.flag('authenticated', true);
d.reset();
d.stage('login-navigation');
let reads = 0;
const error = {get name() { return ++reads === 1 ? 'Error' : 'private-token'; }};
const failed = d.snapshot(error, 'private-journey');
const throwing = d.snapshot({get name() { throw new Error('private-token'); }});
console.log(JSON.stringify({failed, throwing, reads}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout
    parsed = json.loads(result.stdout)
    assert parsed["reads"] == 1
    for key in ("failed", "throwing"):
        assert parsed[key] == {
            "stage": "login-navigation",
            "journey": "preflight",
            "error_class": "Error",
            "observations": {},
        }


def test_login_response_is_consumed_before_form_reload_retires_body():
    """Deterministic response lifetime test; not a Chromium/native proof."""
    module = Path(__file__).parent / "browser/browser_login.mjs"
    script = r"""
const {observeLoginResponse} = await import(process.argv[1]);
function fixture() {
  let resolveResponse;
  let retired = false;
  const response = {
    url: () => 'https://fixture.invalid/auth/login',
    request: () => ({method: () => 'POST'}),
    status: () => 200,
    headers: () => ({'content-type': 'application/json'}),
    body: async () => {
      if (retired) throw new Error('Response body retired');
      return Buffer.from(JSON.stringify({outcome: 'ok', extra: 'private-payload'}));
    },
  };
  const pending = new Promise(resolve => { resolveResponse = resolve; });
  return {
    page: {waitForResponse: () => pending},
    click: async () => {
      resolveResponse(response);
      await Promise.resolve();
      await Promise.resolve();
      retired = true;
    },
  };
}
const old = fixture();
const waited = old.page.waitForResponse();
await old.click();
let oldFailed = false;
try { await (await waited).body(); } catch { oldFailed = true; }
const current = fixture();
const captured = observeLoginResponse(current.page);
await current.click();
console.log(JSON.stringify({oldFailed, reply: await captured}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout
    assert json.loads(result.stdout) == {
        "oldFailed": True,
        "reply": {
            "observed": True,
            "status": 200,
            "body_read": True,
            "outcome_ok": True,
            "body_failure": "none",
            "body_length": 42,
            "content_type": "json",
        },
    }


def test_login_observer_keeps_failure_and_outcome_checks_strict():
    module = Path(__file__).parent / "browser/browser_login.mjs"
    script = r"""
const {observeLoginResponse} = await import(process.argv[1]);
const results = [];
for (const kind of ['missing', 'body-error', 'wrong-outcome', 'refused']) {
  const page = {waitForResponse: async predicate => {
    if (kind === 'missing') throw new Error('private-message');
    const response = {
      url: () => 'https://fixture.invalid/auth/login',
      request: () => ({method: () => 'POST'}),
      status: () => kind === 'refused' ? 401 : 200,
      headers: () => ({'content-type': 'application/json; private-param'}),
      body: async () => {
        if (kind === 'body-error') throw new Error('private-message');
        return Buffer.from(JSON.stringify({outcome: kind === 'wrong-outcome' ? 'private-outcome' : 'ok'}));
      },
    };
    if (!predicate(response)) throw new Error('Predicate mismatch');
    return response;
  }};
  results.push(await observeLoginResponse(page));
}
console.log(JSON.stringify(results));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout
    missing, body_error, wrong_outcome, refused = json.loads(result.stdout)
    assert missing == {
        "observed": False,
        "status": None,
        "body_read": False,
        "outcome_ok": False,
        "body_failure": "unobserved",
        "body_length": None,
        "content_type": "missing",
    }
    assert body_error == {
        "observed": True,
        "status": 200,
        "body_read": False,
        "outcome_ok": False,
        "body_failure": "retrieval",
        "body_length": None,
        "content_type": "json",
    }
    assert wrong_outcome == {
        "observed": True,
        "status": 200,
        "body_read": True,
        "outcome_ok": False,
        "body_failure": "none",
        "body_length": 29,
        "content_type": "json",
    }
    assert refused["status"] == 401
    probe = (module.parent / "browser.mjs").read_text()
    assert "requireCondition(response.body_read)" in probe
    assert "response.status === 200 && response.outcome_ok" in probe
    assert "current.value.authenticated === true" in probe
    assert "await assertReads(page, expected, identity)" in probe
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/installed-browser.yml"
    ).read_text()
    assert "cp tests/qualification/browser/browser_login.mjs " in workflow


def test_login_body_diagnostics_distinguish_retrieval_decode_and_parse():
    module = Path(__file__).parent / "browser/browser_login.mjs"
    script = r"""
const {observeLoginResponse} = await import(process.argv[1]);
const {createDiagnostics} = await import(new URL('./browser_diagnostics.mjs', process.argv[1]));
const output = [];
for (const kind of ['retrieval', 'json', 'utf8', 'oversize', 'empty', 'valid']) {
  const response = {
    status: () => 200,
    headers: () => ({'content-type': 'application/json; private-param'}),
    body: async () => {
      if (kind === 'retrieval') throw new Error('private-exception');
      if (kind === 'utf8') return Buffer.from([255]);
      if (kind === 'oversize') return Buffer.alloc(4097, 65);
      if (kind === 'empty') return Buffer.alloc(0);
      if (kind === 'json') return Buffer.from('private-invalid-json');
      return Buffer.from(JSON.stringify({outcome:'ok', csrf_token:'private-value'}));
    }
  };
  const reply = await observeLoginResponse({waitForResponse: async () => response});
  const diagnostics = createDiagnostics();
  diagnostics.loginBody(reply);
  output.push({reply, observations: diagnostics.snapshot().observations});
}
const rejected = createDiagnostics();
rejected.loginBody({body_failure:'private-value',content_type:'private-value',body_length:4097});
output.push(rejected.snapshot().observations);
let reads = 0;
rejected.loginBody({get body_failure() { return reads++ ? 'private-value' : 'json'; }, content_type:'other',body_length:0});
output.push(rejected.snapshot().observations);
rejected.reset();
rejected.loginBody({get body_failure() { throw new Error('private-value'); }});
output.push(rejected.snapshot().observations);
console.log(JSON.stringify(output));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout
    *cases, rejected, changing, throwing = json.loads(result.stdout)
    assert rejected == throwing == {}
    assert changing == {
        "login_body_failure": "json",
        "login_content_type": "other",
        "login_body_length": 0,
    }
    assert [case["reply"]["body_failure"] for case in cases] == [
        "retrieval",
        "json",
        "utf8",
        "oversize",
        "json",
        "none",
    ]
    assert [case["reply"]["body_read"] for case in cases] == [False] * 5 + [True]
    assert cases[0]["reply"]["body_length"] is None
    assert cases[3]["reply"]["body_length"] is None
    assert cases[4]["reply"]["body_length"] == 0
    assert all(case["reply"]["content_type"] == "json" for case in cases)
    assert cases[-1]["reply"]["outcome_ok"] is True


def test_login_metadata_failure_never_rejects_during_pending_click():
    module = Path(__file__).parent / "browser/browser_login.mjs"
    script = r"""
const {observeLoginResponse} = await import(process.argv[1]);
const replies=[];
let unhandled = false;
process.on('unhandledRejection', () => { unhandled = true; });
for (const kind of ['status','headers','length','changing','bad-status']) {
  let reads=0;
  const response={
    status: () => { if(kind==='status') throw new Error('private-message'); return kind==='bad-status'?'private-value':200; },
    headers: () => { if(kind==='headers') throw new Error('private-message'); return {'content-type':'application/json; private-value'}; },
    body: async () => {
      if(kind==='length') return {get length(){throw new Error('private-message');}};
      if(kind==='changing') return {get length(){return reads++ ? 'private-value' : 0;}};
      return Buffer.from('{}');
    }
  };
  const pending=observeLoginResponse({waitForResponse:async()=>response});
  await new Promise(resolve=>setImmediate(resolve)); // click remains pending
  replies.push(await pending);
}
const sync=observeLoginResponse({waitForResponse(){throw new Error('private-message');}});
const predicate=observeLoginResponse({waitForResponse(predicate){return Promise.resolve().then(()=>predicate({get url(){throw new Error('private-message');}}));}});
await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({unhandled,replies, sync:await sync,predicate:await predicate}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["unhandled"] is False
    assert data["sync"]["body_failure"] == "unobserved"
    assert data["predicate"]["body_failure"] == "unobserved"
    assert [reply["body_failure"] for reply in data["replies"]] == [
        "metadata",
        "metadata",
        "retrieval",
        "utf8",
        "none",
    ]
    assert data["replies"][-1]["status"] is None


def test_durable_login_observer_binding_lifecycle_and_strict_failures():
    module = Path(__file__).parent / "browser/browser_cdp.mjs"
    script = r"""
import {EventEmitter} from 'node:events';
const {captureLoginResponse} = await import(process.argv[1]);
const results=[];
for(const kind of ['valid','base64','json','utf8','oversize','retrieval','redirect','network','timeout','unrelated','ordering','unsupported','setup-timeout','click-error','cleanup-error','duplicate','response-mismatch','refused','wrong-outcome']) {
  const calls=[];
  let detached=false;
  const client=new EventEmitter();
  client.send=async(method,params)=>{
    calls.push({method,params});
    if(kind==='setup-timeout' && method==='Network.configureDurableMessages' && params.maxTotalBufferSize) return new Promise(()=>{});
    if(kind==='unsupported' && method==='Network.configureDurableMessages' && params.maxTotalBufferSize) throw new Error('private-message');
    if(kind==='cleanup-error' && (method==='Network.disable' || method==='Network.configureDurableMessages' && !params.maxTotalBufferSize)) throw new Error('private-message');
    if(method==='Network.getResponseBody') {
      if(kind==='retrieval') throw new Error('private-message');
      if(kind==='json') return {body:'private-invalid-json',base64Encoded:false};
      if(kind==='utf8') return {body:Buffer.from([255]).toString('base64'),base64Encoded:true};
      if(kind==='oversize') return {body:'x'.repeat(4097),base64Encoded:false};
      const body=JSON.stringify({outcome:kind==='wrong-outcome'?'denied':'ok',csrf_token:'private-value'});
      return {body:kind==='base64'?Buffer.from(body).toString('base64'):body,base64Encoded:kind==='base64'};
    }
    return {};
  };
  client.detach=async()=>{detached=true;};
  let clicked=false;
  const emitRequest=(id,url='https://fixture.invalid/auth/login',method='POST',redirectResponse)=>client.emit('Network.requestWillBeSent',{requestId:id,request:{url,method},redirectResponse});
  const emitResponse=(id,status=200)=>client.emit('Network.responseReceived',{requestId:id,response:{url:'https://fixture.invalid/auth/login',status,headers:{'Content-Type':'application/json; private-param'}}});
  let reply,error=false;
  try {
    reply=await captureLoginResponse({context:()=>({newCDPSession:async()=>client})},'https://fixture.invalid',async()=>{
      clicked=true;
      if(kind==='click-error') throw new Error('private-message');
      if(kind==='timeout') return;
      if(kind==='unrelated') {
        emitRequest('foreign','https://other.invalid/auth/login');
        emitResponse('foreign'); client.emit('Network.loadingFinished',{requestId:'foreign'});
        emitRequest('get','https://fixture.invalid/auth/login','GET');
        emitRequest('query','https://fixture.invalid/auth/login?other');
      }
      emitRequest('selected');
      if(kind==='redirect') {emitRequest('selected','https://fixture.invalid/elsewhere','GET',{});return;}
      if(kind==='network') {client.emit('Network.loadingFailed',{requestId:'selected',errorText:'private-message'});return;}
      if(kind==='ordering') {client.emit('Network.loadingFinished',{requestId:'selected'});return;}
      if(kind==='duplicate') {emitRequest('second');return;}
      if(kind==='response-mismatch') {client.emit('Network.responseReceived',{requestId:'selected',response:{url:'https://other.invalid/auth/login',status:200,headers:{}}});return;}
      emitResponse('selected',kind==='refused'?401:200);
      client.emit('Network.loadingFinished',{requestId:'selected'});
      client.emit('Network.loadingFinished',{requestId:'selected'});
      await Promise.resolve(); // Simulate navigation retiring the renderer.
    },10);
  } catch(e) { error=e.message==='Login observation failed'; }
  if(!detached || client.eventNames().length) throw new Error('Cleanup assertion');
  const reads=calls.filter(c=>c.method==='Network.getResponseBody');
  if(reads.some(c=>c.params.requestId!=='selected')) throw new Error('Binding assertion');
  if(reads.length>1) throw new Error('Duplicate retrieval');
  results.push({kind,reply,error,clicked,reads:reads.length,commands:calls.map(c=>c.method),enable:calls[0].params});
}
console.log(JSON.stringify(results));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout + result.stderr
    cases = {case["kind"]: case for case in json.loads(result.stdout)}
    for kind in ("valid", "base64", "unrelated", "cleanup-error"):
        assert cases[kind]["reply"]["outcome_ok"] is True
        assert cases[kind]["reply"]["body_read"] is True
        assert cases[kind]["reads"] == 1
    for kind, category in {
        "json": "json",
        "utf8": "utf8",
        "oversize": "oversize",
        "retrieval": "retrieval",
        "redirect": "redirect",
        "network": "network",
        "timeout": "timeout",
        "ordering": "metadata",
        "duplicate": "redirect",
        "response-mismatch": "redirect",
    }.items():
        assert cases[kind]["reply"]["body_failure"] == category
        assert cases[kind]["reply"]["body_read"] is False
    assert cases["unsupported"]["error"] is True
    assert cases["unsupported"]["clicked"] is False
    assert cases["click-error"]["error"] is True
    assert cases["setup-timeout"]["error"] is True
    assert cases["setup-timeout"]["clicked"] is False
    assert cases["refused"]["reply"]["status"] == 401
    assert cases["wrong-outcome"]["reply"]["outcome_ok"] is False
    for case in cases.values():
        assert case["enable"] == {
            "maxTotalBufferSize": 4194304,
            "maxResourceBufferSize": 65536,
            "maxPostDataSize": 0,
        }
        assert case["commands"][-2:] == [
            "Network.configureDurableMessages",
            "Network.disable",
        ]


def test_negative_csrf_transport_uses_browser_xhr_and_observes_wire_header():
    module = Path(__file__).parent / "browser/browser_csrf.mjs"
    script = r"""
const {invokeWithoutCsrfWrapper} = await import(process.argv[1]);
let fetchCalls=0;
globalThis.fetch=()=>{fetchCalls++;throw new Error('Production wrapper must remain unused');};
const output=[];
for(const kind of ['missing','wrong','injected','header-failure','network']) {
  let requestResolve,predicate,details;
  const pending=new Promise(resolve=>requestResolve=resolve);
  globalThis.XMLHttpRequest=class {
    headers={};
    open(method,path){this.method=method;this.path=path;}
    setRequestHeader(name,value){this.headers[name]=value;}
    send(){
      if(kind==='injected')this.headers['X-CSRF-Token']='private-unexpected';
      const xhr=this;
      const request={url:()=>`https://fixture.invalid${this.path}`,method:()=>this.method,
        headerValue:async()=>{if(kind==='header-failure')throw new Error('private-message');return xhr.headers['X-CSRF-Token']??null;}};
      if(!predicate(request) || predicate({url:()=>`https://foreign.invalid${this.path}`,method:()=>this.method}) || predicate({url:()=>request.url(),method:()=> 'GET'}))throw new Error('Predicate binding');
      requestResolve(request);
      details={credentials:this.withCredentials,timeout:this.timeout,responseType:this.responseType,headerNames:Object.keys(this.headers)};
      if(kind==='network'){this.onerror();return;}
      this.status=401;this.response={ok:false,error:{code:'UNAUTHENTICATED',op:'api.browser'}};
      this.onload();
    }
  };
  try {
    const reply=await invokeWithoutCsrfWrapper({waitForRequest:fn=>{predicate=fn;return pending;},evaluate:(fn,arg)=>fn(arg)},'https://fixture.invalid','agents.tasks.get',{task_id:'fixture'},kind==='wrong'?'private-wrong-value':undefined);
    output.push({kind,reply,details});
  }catch(error){output.push({kind,fixedError:error.message==='Negative browser request failed'});}
}
console.log(JSON.stringify({fetchCalls,output}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.resolve().as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "private" not in result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["fetchCalls"] == 0
    cases = {case["kind"]: case for case in data["output"]}
    for kind in ("missing", "wrong"):
        assert cases[kind]["reply"]["request_observed"] is True
        assert cases[kind]["reply"]["csrf_header_expected"] is True
        assert cases[kind]["reply"]["status"] == 401
        assert cases[kind]["details"]["credentials"] is True
        assert cases[kind]["details"]["timeout"] == 20000
        assert cases[kind]["details"]["responseType"] == "json"
    assert cases["missing"]["reply"]["csrf_header_present"] is False
    assert cases["wrong"]["reply"]["csrf_header_present"] is True
    assert cases["injected"]["reply"]["csrf_header_expected"] is False
    assert cases["header-failure"]["reply"]["request_observed"] is False
    assert cases["network"]["fixedError"] is True
    assert cases["missing"]["details"]["headerNames"] == ["Content-Type"]
    assert cases["wrong"]["details"]["headerNames"] == ["Content-Type", "X-CSRF-Token"]
    probe = (module.parent / "browser.mjs").read_text()
    assert 'requireCondition(refused.value.error?.op === "api.browser")' in probe
    assert "requireCondition([401, 403].includes(refused.status))" in probe
