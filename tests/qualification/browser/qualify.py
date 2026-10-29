"""Isolated installed browser/native acceptance controller.

No exception payload, server log, credential, or key is printed or uploaded.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

# -I excludes the script directory. Add only copied fixture helpers, never source.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import native
import test_real_boot as boot
from infrastructure import (
    browser_ca,
    configure_signer,
    https_origin,
    private_write,
    vault,
)


def admin_claims(scope: str) -> dict[str, Any]:
    """Explicit disposable external process authority on the isolated socket."""
    return {
        "principal": boot.PROCESS_SUBJECT,
        "tenant": boot.TENANT,
        "audience": boot.AUDIENCE,
        "agent_id": boot.PROCESS_SUBJECT,
        "roles": [],
        "scopes": [scope],
        "policy_version": boot.POLICY_VERSION,
        "delegation": [],
    }


async def connect(context: dict[str, Any], scope: str) -> Any:
    from epistemic_graph import EpistemicGraphClient

    return await EpistemicGraphClient.connect(
        socket_path=context["socket_path"],
        auth_secret=context["secret"],
        graph_name="__control__",
        verified_context=admin_claims(scope),
    )


async def verify_runtime(context: dict[str, Any]) -> str:
    """Use only Health's exact fixture capability, never the identity broker."""
    context["fixture_step"] = "native_runtime_version"
    client = await connect(context, "service:control")
    try:
        return await native.require_native_version(client)
    finally:
        await client.close()


async def observed_client(
    context: dict[str, Any], connector: Any, **kwargs: Any
) -> Any:
    """Observe only fixed method names on the real transport, forwarding unchanged."""
    client = await connector(**kwargs)
    send = client._send

    async def observed(method: str, *args: Any, **options: Any) -> Any:
        if method == "Health":
            context["human_health_probed"] = True
        if method in {
            "GetRunWorkItem",
            "SubmitWorkItem",
            "ClaimWorkItem",
            "GetCurrentPlan",
            "PublishCurrentPlan",
            "Health",
        }:
            context["native_method"] = method
        else:
            context["native_method"] = "other"
        return await send(method, *args, **options)

    client._send = observed
    return client


async def prepare(
    context: dict[str, Any], backend: Any, credentials: dict, denied: dict
) -> dict[str, str]:
    from epistemic_graph import EpistemicGraphClient

    from arbora.core.settings import runtime_config
    from arbora.epistemic import EpistemicClient, EpistemicClientPool
    from arbora.identity.browser_broker import ConfiguredBrowserBroker
    from arbora.identity.browser_composition import _IdentityClient
    from arbora.identity.browser_runtime import BrowserCallerResolver
    from arbora.identity.engine_ports import EngineSessionAuthority
    from arbora.identity.issuer import IssuerSettings, LocalIssuer
    from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.mcp_server.served_invocation import (
        compose_served_invocation,
        refused_fleet_effect,
    )
    from arbora.security.request_identity import (
        actor_from_bearer_token,
        mint_graph_session,
        verify_local_bearer_token,
    )

    # These settings belong only to this fixture controller process.
    runtime_config.auth_jwt_jwks_uri = context["issuer"].jwks_uri
    runtime_config.auth_jwt_issuer = boot.ISSUER
    runtime_config.auth_jwt_audience = boot.AUDIENCE
    runtime_config.kg_policy_version = boot.POLICY_VERSION
    context["native_runtime_version"] = await verify_runtime(context)
    context["fixture_step"] = "fixture_admin_connect"
    admin = await connect(context, "identity:admin")

    async def connect_observed(**kwargs: Any) -> EpistemicClient:
        # Same explicit SDK-to-port binding used by process_engine_pool.
        return cast(
            EpistemicClient,
            await observed_client(context, EpistemicGraphClient.connect, **kwargs),
        )

    pool = EpistemicClientPool(
        connect_observed,
        socket_path=context["socket_path"],
        auth_secret=context["secret"],
    )
    try:
        context["fixture_step"] = "native_identity_initialize"
        await native.initialize_local_identity(
            admin,
            "__control__",
            admin_username="fixture-admin",
            admin_password=secrets.token_urlsafe(32),
        )
        broker_subject = "svc:browser-qualification"
        context["fixture_step"] = "native_managed_broker"
        await native.provision_managed_broker(
            admin,
            "__control__",
            principal_id=broker_subject,
            username="browser-broker",
            system_principal_id=boot.PROCESS_SUBJECT,
        )
        broker_token = context["issuer"].token(
            broker_subject, email=False, roles=("identity:authenticate",)
        )
        context["env"]["QUALIFICATION_BROKER_TOKEN"] = broker_token
        os.environ["QUALIFICATION_BROKER_TOKEN"] = broker_token
        del broker_token
        process_actor = await actor_from_bearer_token(context["process_token"])
        process_session = mint_graph_session(process_actor)

        async def borrowed_client():
            return admin

        broker_authority = ConfiguredBrowserBroker(
            "env://QUALIFICATION_BROKER_TOKEN",
            tenant=boot.TENANT,
            audience=boot.AUDIENCE,
            process_claims=process_session.engine_verified_context,
        )
        context["fixture_step"] = "configured_broker_verification"
        await broker_authority()
        broker = _IdentityClient(borrowed_client, broker_authority)
        context["fixture_step"] = "native_primary_human"
        principal = await native.provision_human(
            admin,
            "__control__",
            **credentials,
            tenant_graph=boot.TENANT_GRAPH,
            seed_permissions=True,
        )
        context["fixture_step"] = "native_denied_human"
        denied_principal = await native.provision_human(
            admin, "__control__", **denied, tenant_graph=boot.TENANT_GRAPH, denied=True
        )
        authority = EngineSessionAuthority(broker, graph="__control__", refresh=False)
        issuer = LocalIssuer(
            backend, IssuerSettings(boot.ISSUER, boot.AUDIENCE, boot.TENANT), authority
        )
        caller = BrowserCallerResolver(
            authority, verify_local_bearer_token, graph="__control__"
        )
        context["fixture_step"] = "native_primary_authentication"
        session, credential = await native.authenticate_seed_session(
            broker,
            "__control__",
            **credentials,
            principal_id=principal,
            issuer=issuer,
            caller=caller,
            stage=lambda stage: context.update(fixture_step=stage),
        )
        context["human_scopes"] = sorted(session.scopes)
        context["human_roles"] = sorted(session.actor.roles)
        context["fixture_step"] = "native_denied_authentication"
        denied_session, denied_credential = await native.authenticate_seed_session(
            broker,
            "__control__",
            **denied,
            principal_id=denied_principal,
            issuer=issuer,
            caller=caller,
            denied=True,
            stage=lambda stage: context.update(fixture_step="denied_" + stage),
        )
        context["denied_scopes"] = sorted(denied_session.scopes)
        del denied_credential
        engine = ServedEngine(pool)
        invocation = compose_served_invocation(
            pool=pool,
            graph="__control__",
            process_session=lambda: session,
            bindings={"current_plan_reader": NativeCurrentPlanReader()},
            fleet_effect=refused_fleet_effect,
        )
        context["fixture_step"] = "native_work_and_plan_seed"
        result = await native.seed_owned_run(
            engine,
            session,
            invocation,
            stage=lambda stage: context.update(fixture_step=stage),
        )
        context["fixture_step"] = "installed_arbora_startup"
        # Do not expose the seed credential; revoke_all later includes it.
        del credential
        return {
            **result,
            "principal_id": principal,
            "denied_principal_id": denied_principal,
        }
    finally:
        await pool.close()
        await admin.close()


async def revoke(context: dict[str, Any], principal: str) -> None:
    client = await connect(context, "identity:admin")
    try:
        await native.revoke_human_sessions(client, "__control__", principal)
    finally:
        await client.close()


def terminate(process: subprocess.Popen[Any]) -> None:
    """Reap the owned group even if its Node leader has already exited."""
    for action in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if action == signal.SIGKILL:
                raise


def browser_environment(private: Path, ca: Path) -> dict[str, str]:
    """Node/Chromium receives no broker, Vault or ambient CI credential."""
    allowed = ("PATH", "LANG", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH")
    return {
        **{key: os.environ[key] for key in allowed if key in os.environ},
        "HOME": str(private / "home"),
        "QUALIFICATION_BROWSER_HOME": str(private / "home"),
        "NODE_EXTRA_CA_CERTS": str(ca),
        "TMPDIR": str(private),
    }


def cleanup(private: Path, browser: subprocess.Popen[Any] | None) -> None:
    try:
        if browser is not None:
            terminate(browser)
    finally:
        os.environ.pop("QUALIFICATION_BROKER_TOKEN", None)
        shutil.rmtree(private, ignore_errors=False)


def safe_refusal_code(exc: BaseException) -> str | None:
    """Only fixed public wire categories; never return detail or arbitrary codes."""
    code = getattr(exc, "code", None)
    allowed = {
        "UNAUTHORIZED",
        "FORBIDDEN",
        "ACCESS_DENIED",
        "PERMISSION_DENIED",
        "INVALID_REQUEST",
        "INVALID_ARGUMENT",
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "NOT_READY",
        "INTERNAL_ERROR",
        "INTERNAL",
        "AUTHENTICATION_FAILED",
        "AUTHORIZATION_DENIED",
        "POLICY_DENIED",
        "SECURITY_ERROR",
    }
    return code if isinstance(code, str) and code in allowed else None


def safe_failure_reason(exc: BaseException) -> str:
    """Fixture diagnostics only: exact constant equality, never a raw fallback."""
    known = {
        "configured browser broker authority unavailable": "broker-verification-unavailable",
        "qualified engine session unavailable": "native-session-unavailable",
        "persisted session evidence unavailable": "persisted-session-unavailable",
        "configured issuer signing key unavailable": "issuer-key-unavailable",
        "configured issuer signing key invalid": "issuer-key-invalid",
        "resolution deployment binding disagrees": "resolution-deployment-mismatch",
        "qualified credential source expiry unavailable": "credential-expiry-unavailable",
        "Trusted HTTPS sign-in readiness": "https-signin-readiness-timeout",
        "Browser phase deadline": "browser-lifecycle-timeout",
    }
    if str(exc) in known:
        return known[str(exc)]
    # Fixed source-authored refusal strings for this fixture's public graph.
    details = {
        "verified principal lacks Read access to graph '__control__'": "control-graph-read-refused",
        "verified principal lacks Write access to graph '__control__'": "control-graph-write-refused",
        "verified request context lacks required scope 'work:read'": "work-read-scope-refused",
        "verified request context lacks required scope 'work:submit'": "work-submit-scope-refused",
        "verified request context lacks required scope 'service:control'": "service-control-scope-refused",
        "SubmitWorkItem context does not match verified request authority": "submit-context-mismatch",
    }
    detail = getattr(exc, "detail", None)
    return details.get(detail, "unknown") if isinstance(detail, str) else "unknown"


def https_presentation_ready(
    origin: str, tls: ssl.SSLContext, context: dict[str, Any]
) -> bool:
    """One genuine probe; retain only fixed public diagnostic categories."""
    observation: dict[str, Any] = {"outcome": "unavailable"}
    context["https_readiness"] = observation
    try:
        with urllib.request.urlopen(
            origin + "/auth/session", context=tls, timeout=3
        ) as response:
            status = response.status
            observation["status"] = (
                status if type(status) is int and 100 <= status <= 599 else None
            )
            presentation = json.load(response)
            mapping = isinstance(presentation, dict)
            signed_out = mapping and presentation.get("authenticated") is False
            local = mapping and presentation.get("mode") == "local"
            observation.update(
                outcome="response", signed_out=signed_out, local_mode=local
            )
            return status == 200 and signed_out and local
    except urllib.error.HTTPError as exc:
        observation["outcome"] = "http-error"
        observation["status"] = (
            exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
        )
    except urllib.error.URLError as exc:
        observation["outcome"] = (
            "tls-error" if isinstance(exc.reason, ssl.SSLError) else "url-error"
        )
    except ssl.SSLError:
        observation["outcome"] = "tls-error"
    except TimeoutError:
        observation["outcome"] = "timeout"
    except OSError:
        observation["outcome"] = "os-error"
    except ValueError:
        observation["outcome"] = "invalid-json"
    return False


def webui_startup_facts(context: dict[str, Any], upstream: int) -> dict[str, Any]:
    """Inspect private fixture output without copying any raw diagnostic text."""
    observed: dict[str, Any] = {"listening": boot._listening(upstream)}
    try:
        with (context["root"] / "arbora.log").open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read(65536).decode("utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        observed["log_available"] = False
        return observed
    observed["log_available"] = True
    for name, marker in {
        "thread_started": "co-service agent-webui started",
        "crashed": "co-service agent-webui crashed",
        "restart_bound": "co-service agent-webui exceeded its restart bound",
        "listener_announced": "agent-webui co-service serving in-process on",
    }.items():
        observed[name] = marker in tail
    allowed = {
        "ImportError",
        "ModuleNotFoundError",
        "AttributeError",
        "TypeError",
        "ValueError",
        "RuntimeError",
        "PermissionError",
        "OSError",
        "arbora.identity.engine.IdentityUnavailable",
    }
    observed["exception_classes"] = sorted(
        {
            line.partition(":")[0]
            for line in tail.splitlines()
            if line.partition(":")[0] in allowed
        }
    )
    return observed


def main() -> None:
    versions = native.require_installed()
    private = Path(os.environ["RUNNER_TEMP"]) / "browser-private"
    private.mkdir(mode=0o700, exist_ok=False)
    output = Path(os.environ["QUALIFICATION_OUTPUT"])
    output.mkdir(mode=0o700, exist_ok=True)
    context: dict[str, Any] = {}
    public: dict[str, str] = {}
    credentials = {"username": "browser-human", "password": secrets.token_urlsafe(32)}
    denied = {"username": "browser-denied", "password": secrets.token_urlsafe(32)}
    browser = None
    phase = "temporary-infrastructure"
    try:
        ca, cert, key = browser_ca(private)
        private_write(private / "credentials.json", json.dumps(credentials).encode())
        private_write(private / "denied.json", json.dumps(denied).encode())
        upstream = boot._free_port()
        with (
            vault(
                private,
                Path(os.environ["QUALIFICATION_VAULT_BINARY"]),
                boot._free_port(),
            ) as vault_client,
            https_origin(0, upstream, cert, key, diagnostics=context) as origin,
        ):

            def before_serve(value: dict[str, Any]) -> None:
                from arbora.security.secrets import VaultBackend

                context.update(value)
                context["fixture_step"] = "before_serve_hook_entered"
                token = configure_signer(vault_client, value["issuer"]._key)
                value["env"]["VAULT_TOKEN"] = token
                config_path = value["config_dir"] / "config.json"
                config = json.loads(config_path.read_text())
                config.update(
                    {
                        "ENABLE_WEB_UI": "true",
                        "WEBUI_AUTH_MODE": "eg",
                        "WEBUI_ORIGIN": origin,
                        "WEBUI_BROKER_TOKEN_REF": "env://QUALIFICATION_BROKER_TOKEN",
                        "SECRETS_BACKEND": "vault",
                        "SECRETS_VAULT_URL": vault_client.url,
                        "VAULT_AUTH_METHOD": "token",
                    }
                )
                config_path.write_text(json.dumps(config))
                value["env"]["ARBORA_WEBUI_PORT"] = str(upstream)
                backend = VaultBackend(
                    url=vault_client.url, token=token, auth_method="token"
                )
                public.update(
                    asyncio.run(prepare(context, backend, credentials, denied))
                )

            phase = "native-setup-and-installed-arbora-boot"
            with boot._booted_stack(
                private_root=private,
                before_serve=before_serve,
                boot_stage=lambda stage: context.update(fixture_step=stage),
                process_token_email=False,
                process_token_roles=boot.ROLES,
            ):
                phase = "installed-browser-lifecycle"
                tls = ssl.create_default_context(cafile=str(ca))
                ready_deadline = time.monotonic() + 90
                context["fixture_step"] = "trusted_https_signin_readiness"
                while True:
                    if https_presentation_ready(origin, tls, context):
                        break
                    if time.monotonic() >= ready_deadline:
                        context["webui_startup"] = webui_startup_facts(
                            context, upstream
                        )
                        raise TimeoutError("Trusted HTTPS sign-in readiness")
                    time.sleep(0.2)
                private_write(
                    private / "ready.json",
                    json.dumps({"origin": origin, **public}).encode(),
                )
                context["fixture_step"] = "chromium_browser_lifecycle"
                env = browser_environment(private, ca)
                browser = subprocess.Popen(
                    [
                        "node",
                        os.environ["QUALIFICATION_BROWSER_SCRIPT"],
                        str(private),
                        str(private / "credentials.json"),
                        str(output),
                        str(private / "denied.json"),
                    ],
                    env=env,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 300
                acknowledged = False
                while browser.poll() is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Browser phase deadline")
                    control = private / "control.json"
                    if control.exists() and not acknowledged:
                        command = json.loads(control.read_text())
                        if (
                            set(command) != {"action", "request_id"}
                            or command["action"] != "revoke_qualification_sessions"
                        ):
                            raise RuntimeError("Unknown fixture control command")
                        asyncio.run(revoke(context, public["principal_id"]))
                        temp = private / "control-result.tmp"
                        private_write(
                            temp,
                            json.dumps(
                                {
                                    "request_id": command["request_id"],
                                    "completed": True,
                                }
                            ).encode(),
                        )
                        temp.rename(private / "control-result.json")
                        acknowledged = True
                    time.sleep(0.1)
                if browser.returncode != 0 or not acknowledged:
                    raise RuntimeError("Browser acceptance refused")
                result = json.loads((output / "browser-result.json").read_text())
                if result.get("status") != "passed":
                    raise RuntimeError("Missing browser acceptance")
                receipt = {
                    "status": "passed",
                    "versions": versions,
                    "native_runtime_version": context["native_runtime_version"],
                    "native_human": public["principal_id"],
                    "arbora_commit": os.environ["QUALIFICATION_ARBORA_COMMIT"],
                    "overstory_commit": os.environ["QUALIFICATION_OVERSTORY_COMMIT"],
                    "fixture_human_has_seed_permissions": True,
                    "fixture_human_health_negotiation": context.get(
                        "human_health_probed", False
                    ),
                    "human_scopes": context["human_scopes"],
                    "human_roles": context["human_roles"],
                    "denied_scopes": context["denied_scopes"],
                    "production_changes": False,
                    "renewal_qualified": False,
                    "cross_origin_qualified": False,
                    "hard_crash_cleanup_qualified": False,
                }
                (output / "receipt.json").write_text(
                    json.dumps(receipt, indent=2) + "\n"
                )
    except BaseException as exc:  # noqa: BLE001 - redact all secret-bearing failure payloads
        # Even boot assertions may contain raw logs. Never print the exception.
        (output / "receipt.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "phase": phase,
                    "error_class": type(exc).__name__,
                    "refusal_code": safe_refusal_code(exc),
                    "reason": safe_failure_reason(exc),
                    "fixture_step": context.get("fixture_step"),
                    "native_method": context.get("native_method"),
                    "https_readiness": context.get("https_readiness"),
                    "proxy_failure": context.get("proxy_failure"),
                    "webui_startup": context.get("webui_startup"),
                }
            )
            + "\n"
        )
        raise RuntimeError(f"Qualification failed in {phase}") from None
    finally:
        cleanup(private, browser)


def interrupted(signum: int, _frame: Any) -> None:
    raise InterruptedError("Qualification interrupted")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except BaseException:  # noqa: BLE001 - never expose fixture exception text
        print(
            "FAIL isolated installed browser qualification; see redacted receipt",
            file=sys.stderr,
        )
        sys.exit(1)
