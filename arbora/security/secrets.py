"""Secret store client for arbora served operations.

:class:`VaultBackend` implements :class:`SecretsBackend` over a KV v2 store.
Tests and hosts may pass any other :class:`SecretsBackend`. The engine does not
serve a durable secret store yet (hand-off EG-IDENTITY-R951).

``resolve_ref`` resolves bounded ``env://``, ``vault://`` and ``secret://``
references.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.security.settings import setting

logger = logging.getLogger(__name__)


def _split_versioned_key(key: str) -> tuple[str, int | None]:
    """Split an optional trailing ``@<version>`` KV v2 pin off ``key``.

    an exact KV v2 version (``path#field@2``) instead of implicitly reading "whatever is
    latest". Pinning is what makes an overwrite recoverable: OpenBao KV v2 never deletes a
    prior version, so a later, bad write to the *latest* slot cannot silently reach a consumer
    that reads a specific, already-verified version number. Returns ``(base_key, None)`` when
    no ``@<digits>`` suffix is present (unchanged, "read latest" behaviour).
    """
    base, separator, version_part = key.rpartition("@")
    if separator and version_part.isdigit():
        return base, int(version_part)
    return key, None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SecretsConfig(BaseModel):
    """Configuration for the secrets client factory."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["vault"] = Field(
        default="vault",
        description="Backend type: encrypted engine storage or 'vault'.",
    )
    vault_url: str | None = Field(
        default=None,
        description="HashiCorp Vault URL (used with 'vault' backend).",
    )
    vault_mount: str = Field(
        default="secret",
        description="Vault KV v2 mount point.",
    )
    vault_auth_method: str = Field(
        default="auto",
        description=(
            "Vault authentication method: 'oidc', 'approle', 'token', "
            "'kubernetes', or 'auto' (auto-detect)."
        ),
    )
    vault_auth_mount: str = Field(
        default="jwt",
        description=(
            "Mount path of the Vault auth method.  Supports custom mounts "
            "(e.g. 'oidc', 'jwt', 'my-okta-auth').  Default: 'jwt'."
        ),
    )
    vault_role: str | None = Field(
        default=None,
        description="Vault role name for OIDC/JWT or Kubernetes login.",
    )
    vault_path_prefix: str | None = Field(
        default=None,
        description=(
            "Path prefix within the KV v2 mount.  E.g. 'agents/mcp/' scopes "
            "all secret reads/writes under 'secret/data/agents/mcp/'."
        ),
    )
    vault_role_id: str | None = Field(
        default=None,
        description="AppRole role_id for Vault authentication.",
    )
    vault_secret_id: str | None = Field(
        default=None,
        description="AppRole secret_id for Vault authentication.",
    )
    vault_k8s_sa_token_path: str = Field(
        default="/var/run/secrets/kubernetes.io/serviceaccount/token",
        description="Path to the Kubernetes service account token file.",
    )


# ---------------------------------------------------------------------------
# Abstract Backend
# ---------------------------------------------------------------------------


class SecretsBackend(abc.ABC):
    """Abstract base class for secrets storage backends."""

    @abc.abstractmethod
    def get(self, key: str) -> str | None:
        """Retrieve a secret by key. Returns ``None`` if not found."""

    @abc.abstractmethod
    def set(self, key: str, value: str, **metadata: Any) -> None:
        """Store a secret with optional metadata."""

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a secret. Returns ``True`` if it existed."""

    @abc.abstractmethod
    def list_keys(self) -> list[str]:
        """List all stored secret keys."""

    def set_if_absent(self, key: str, value: str, **metadata: Any) -> bool:
        """Provision ``key`` only when it is not already present.

        Returns ``True`` when *this* call created the value, ``False`` when a
        value already existed (left untouched). This default is a best-effort,
        non-atomic read-then-write for backends without a native conditional
        insert; the durable engine backend overrides it with an atomic
        membership-test-and-insert so two concurrent provisioners converge on one
        winner. Callers that need the winning value on a ``False`` result must
        re-read via :meth:`get`.
        """
        if self.get(key) is not None:
            return False
        self.set(key, value, **metadata)
        return True

    def compare_and_set(
        self, key: str, expected: str, value: str, **metadata: Any
    ) -> bool:
        """Replace ``key``'s value only when it currently equals ``expected``.

        Returns ``True`` on a successful conditional replace. This default is a
        best-effort, non-atomic read-check-write; the durable engine backend
        overrides it with an atomic compare-and-set so a versioned rotation is
        race-safe. Used by signing-key rotation (new active version, old -> grace).
        """
        if self.get(key) != expected:
            return False
        self.set(key, value, **metadata)
        return True


# ---------------------------------------------------------------------------
# Engine-backed Backend (the durable default)
# ---------------------------------------------------------------------------


class VaultBackend(SecretsBackend):
    """HashiCorp Vault KV v2 backend with multi-auth support.

    Requires the ``hvac`` package (``pip install hvac``).

    Supports four authentication strategies (in priority order):

    1. **OIDC/JWT** — Exchanges the SSO user token (from
       ``UserTokenMiddleware``) for a user-scoped Vault token via
       Vault's JWT/OIDC auth method.
    2. **AppRole** — Machine-to-machine auth via ``role_id`` +
       ``secret_id`` (ideal for CI/CD pipelines).
    3. **Static Token** — Classic ``VAULT_TOKEN`` env var (backward
       compatible).
    4. **Kubernetes** — Auto-detects pod-mounted service-account JWT
       (useful for K8s-native deployments).

    The ``auth_mount`` parameter supports custom mount paths, so the
    auth method does not need to be at the default ``/auth/jwt`` —
    any path (e.g. ``/auth/my-okta-oidc``) works.

    Path prefixes scope secret reads/writes within the KV v2 mount::

        VaultBackend(path_prefix="agents/mcp/")
        backend.get("gitlab/token")
        # reads: secret/data/agents/mcp/gitlab/token

    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8200",
        token: str | None = None,
        mount_point: str = "secret",
        auth_method: str = "auto",
        auth_mount: str = "jwt",
        role: str | None = None,
        path_prefix: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        k8s_sa_token_path: str | None = None,
        user_token: Callable[[], str | None] | None = None,
    ) -> None:
        self._user_token = user_token or (lambda: None)
        try:
            import hvac
        except ImportError as exc:
            raise ImportError(
                "The 'hvac' package is required for the Vault backend. "
                "Install it with: pip install hvac"
            ) from exc

        self._mount = mount_point
        self._path_prefix = path_prefix.rstrip("/") if path_prefix else None
        self._auth_method = auth_method
        self._auth_mount = auth_mount
        self._role = role or setting("VAULT_ROLE", "default")
        self._role_id = role_id or setting("VAULT_ROLE_ID")
        self._secret_id = secret_id or setting("VAULT_SECRET_ID")
        self._k8s_sa_token_path = (
            k8s_sa_token_path or "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )
        self._token_lease_duration: float = 0.0
        self._token_auth_time: float = 0.0

        # Initialise hvac client — may not have a token yet
        static_token = token or setting("VAULT_TOKEN")
        self._client = hvac.Client(url=url, token=static_token)

        # Authenticate using the configured method
        self._authenticate(static_token)

        logger.info("Vault backend initialised auth_method=%s", self._auth_method)

    # -- Authentication strategies -----------------------------------------

    def _authenticate(self, static_token: str | None = None) -> None:
        """Authenticate to Vault using the best available method.

        When ``auth_method='auto'``, tries in order:
        OIDC/JWT → AppRole → static token → Kubernetes.
        """
        import time as _time

        method = self._auth_method

        if method == "auto":
            if self._try_oidc():
                self._auth_method = "oidc"
                return
            if self._try_approle():
                self._auth_method = "approle"
                return
            if static_token and self._client.is_authenticated():
                self._auth_method = "token"
                logger.info("Vault: Authenticated via static token.")
                return
            if self._try_kubernetes():
                self._auth_method = "kubernetes"
                return
            # Fallback — hope the token is valid
            logger.warning(
                "Vault: No auth method succeeded; using unauthenticated client."
            )
            return

        if method == "oidc":
            if not self._try_oidc():
                raise RuntimeError(
                    "Vault OIDC auth failed. Ensure the MCP server has an active "
                    "SSO session and VAULT_ROLE is set."
                )
        elif method == "approle":
            if not self._try_approle():
                raise RuntimeError(
                    "Vault AppRole auth failed. Check VAULT_ROLE_ID and VAULT_SECRET_ID."
                )
        elif method == "kubernetes":
            if not self._try_kubernetes():
                raise RuntimeError(
                    "Vault Kubernetes auth failed. Ensure a service account token "
                    "is mounted and VAULT_ROLE is set."
                )
        elif method == "token":
            if not self._client.is_authenticated():
                raise RuntimeError(
                    "Vault token auth failed. Set VAULT_TOKEN or provide a token."
                )
            logger.info("Vault: Authenticated via static token.")
        else:
            raise ValueError(f"Unsupported vault_auth_method: {method!r}")

        self._token_auth_time = _time.monotonic()

    def _try_oidc(self) -> bool:
        """Authenticate using the SSO user token from ``UserTokenMiddleware``.

        Uses Vault's JWT/OIDC auth method at the configured ``auth_mount``
        path (default: ``jwt``).  Works with any custom mount path.
        """
        try:
            user_jwt = self._user_token()
            if not user_jwt:
                return False

            resp = self._client.auth.jwt.jwt_login(
                role=self._role,
                jwt=user_jwt,
                path=self._auth_mount,
            )
            self._client.token = resp["auth"]["client_token"]
            self._token_lease_duration = float(resp["auth"].get("lease_duration", 3600))
            import time as _time

            self._token_auth_time = _time.monotonic()
            logger.info("Vault OIDC/JWT authentication succeeded")
            return True
        except Exception as e:  # noqa: BLE001 — one candidate in _authenticate()'s auto-detect chain (oidc/approle/token/kubernetes); a loud logger.warning already fires there if every candidate fails
            logger.debug("Vault OIDC authentication failed: %s", e)
            return False

    def _try_approle(self) -> bool:
        """Authenticate using AppRole (role_id + secret_id)."""
        if not self._role_id or not self._secret_id:
            return False
        try:
            resp = self._client.auth.approle.login(
                role_id=self._role_id,
                secret_id=self._secret_id,
            )
            self._client.token = resp["auth"]["client_token"]
            self._token_lease_duration = float(resp["auth"].get("lease_duration", 3600))
            import time as _time

            self._token_auth_time = _time.monotonic()
            logger.info("Vault: AppRole auth successful.")
            return True
        except Exception as e:  # noqa: BLE001 — one candidate in _authenticate()'s auto-detect chain; a loud logger.warning already fires there if every candidate fails
            logger.debug("Vault AppRole authentication failed: %s", e)
            return False

    def _try_kubernetes(self) -> bool:
        """Authenticate using Kubernetes service account JWT."""
        sa_path = Path(self._k8s_sa_token_path)
        if not sa_path.exists():
            return False
        try:
            sa_jwt = sa_path.read_text().strip()
            resp = self._client.auth.kubernetes.login(
                role=self._role,
                jwt=sa_jwt,
            )
            self._client.token = resp["auth"]["client_token"]
            self._token_lease_duration = float(resp["auth"].get("lease_duration", 3600))
            import time as _time

            self._token_auth_time = _time.monotonic()
            logger.info("Vault: Kubernetes auth successful.")
            return True
        except Exception as e:  # noqa: BLE001 — one candidate in _authenticate()'s auto-detect chain; a loud logger.warning already fires there if every candidate fails
            logger.debug("Vault Kubernetes authentication failed: %s", e)
            return False

    def _ensure_authenticated(self) -> None:
        """Re-authenticate if the current Vault token is near expiry."""
        import time as _time

        if self._token_lease_duration <= 0:
            return  # Static token or unknown TTL — skip

        elapsed = _time.monotonic() - self._token_auth_time
        # Renew when 80% of TTL has elapsed
        if elapsed >= (self._token_lease_duration * 0.8):
            logger.info("Vault: Token nearing expiry, re-authenticating...")
            self._authenticate()

    # -- Path prefix helper ------------------------------------------------

    def _full_path(self, key: str) -> str:
        """Prepend the configured path prefix to a secret key.

        Example::

            prefix = "agents/mcp"
            _full_path("gitlab/token") → "agents/mcp/gitlab/token"
        """
        if self._path_prefix:
            return f"{self._path_prefix}/{key}"
        return key

    # -- SecretsBackend interface -------------------------------------------

    def get(self, key: str) -> str | None:
        """Read one KV v2 field, at its latest version or a pinned ``@<version>``.

        ``path[#field][@version]``: an omitted ``@version`` reads the current
        (latest) version exactly as before; a present one reads that specific,
        immutable version — the read path a consumer pins to survive a later
        overwrite of "latest" (see :func:`_split_versioned_key`).
        """
        self._ensure_authenticated()
        base_key, version = _split_versioned_key(key)
        path, separator, field = base_key.partition("#")
        full_key = self._full_path(path)
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=full_key, mount_point=self._mount, version=version
            )
            data = resp.get("data", {}).get("data", {})
            return data.get(field if separator else "value")
        except Exception as exc:
            # D-SWG-2: loud, not debug — this catches BOTH "secret genuinely
            # absent" and "Vault unreachable/unauthorized"; a caller receiving
            # None can't tell those apart, so a systemic Vault outage would
            # look identical to a missing key and degrade every consumer's
            # config silently unless this is visible at a level someone
            # actually watches.
            logger.warning("Vault secret lookup failed for %r: %s", full_key, exc)
            return None

    def set(self, key: str, value: str, **metadata: Any) -> None:
        self._ensure_authenticated()
        full_key = self._full_path(key)
        secret_data = {"value": value}
        if metadata:
            secret_data.update(metadata)
        self._client.secrets.kv.v2.create_or_update_secret(
            path=full_key, secret=secret_data, mount_point=self._mount
        )
        logger.info("Secret stored in the configured vault backend")

    def delete(self, key: str) -> bool:
        self._ensure_authenticated()
        full_key = self._full_path(key)
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=full_key, mount_point=self._mount
            )
            logger.info("Secret deleted from the configured vault backend")
            return True
        except Exception:
            return False

    def list_keys(self) -> list[str]:
        self._ensure_authenticated()
        prefix = self._full_path("")
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=prefix, mount_point=self._mount
            )
            return sorted(resp.get("data", {}).get("keys", []))
        except Exception:
            return []


# ---------------------------------------------------------------------------
# High-level Client
# ---------------------------------------------------------------------------


class SecretsClient:
    """High-level secrets client over one :class:`SecretsBackend`.

    It adds ``resolve_ref`` for bounded ``vault://``, ``secret://`` and
    ``env://`` references, and ``vault_sync`` to seed a service's keys.
    """

    def __init__(self, backend: SecretsBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> SecretsBackend:
        """The underlying storage backend."""
        return self._backend

    # -- Core operations ---------------------------------------------------

    def get(self, key: str) -> str | None:
        """Retrieve a secret by key."""
        return self._backend.get(key)

    def set(self, key: str, value: str, **metadata: Any) -> None:
        """Store a secret."""
        self._backend.set(key, value, **metadata)

    def set_if_absent(self, key: str, value: str, **metadata: Any) -> bool:
        """Provision a secret only if absent; ``True`` iff this call created it.

        Durable and atomic on the engine backend (one winner across concurrent
        provisioners); a ``False`` result means a value already exists — re-read it
        with :meth:`get`.
        """
        return self._backend.set_if_absent(key, value, **metadata)

    def compare_and_set(
        self, key: str, expected: str, value: str, **metadata: Any
    ) -> bool:
        """Conditionally replace ``key`` only if it currently equals ``expected``.

        Atomic on the engine backend; the substrate for versioned signing-key
        rotation. ``False`` means the stored value moved — re-read with :meth:`get`.
        """
        return self._backend.compare_and_set(key, expected, value, **metadata)

    def delete(self, key: str) -> bool:
        """Delete a secret."""
        return self._backend.delete(key)

    def list_keys(self) -> list[str]:
        """List all stored keys."""
        return self._backend.list_keys()

    # -- Extended operations -----------------------------------------------

    def resolve_ref(self, ref: str) -> str | None:
        """Resolve a URI-style secret reference.

        Supported schemes:

        - ``vault://path/to/secret`` → backend lookup
        - ``vault://path#field`` → backend lookup of one field within the secret
        - ``vault://path#field@2`` → that field at pinned KV v2 **version** 2, not
          "whatever is latest" — a :class:`VaultBackend` reads exactly that version
          (CONCEPT:AU-KG.ontology.release-key-rotation, D-OC-1); other backends simply
          have no second version and the pin is a no-op on them.
        - ``env://VAR_NAME`` → ``setting(VAR_NAME)``
        Args:
            ref: Secret reference string.

        Returns:
            The resolved secret value, or ``None``.
        """
        scheme, separator, target = str(ref or "").strip().partition("://")
        if (
            not separator
            or scheme not in {"env", "vault", "secret"}
            or not target
            or len(ref.encode("utf-8")) > 1_024
            or any(character.isspace() or ord(character) < 32 for character in ref)
            or ".." in target.split("/")
        ):
            raise ValueError("runtime secret reference is invalid")
        if scheme == "env":
            if not target.replace("_", "a").isalnum() or target[0].isdigit():
                raise ValueError("runtime secret reference is invalid")
            var_name = target
            return setting(var_name)
        if not all(
            character.isalnum() or character in "_./#@-" for character in target
        ):
            raise ValueError("runtime secret reference is invalid")
        return self._backend.get(target)

    def vault_sync(
        self,
        service: str,
        env_keys: list[str],
        values: dict[str, str] | None = None,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Reconcile a service's secrets with the store (read-existing + seed).

        operator (or Claude) never re-supplies a secret that already exists. For
        each env var name a service needs, it:

        1. **Reads** the existing value from the store at ``<service>/<KEY>`` (the
           standardized ``apps/<service>`` layout when the backend is mounted at
           ``apps``) — already-present keys are kept and reported, never re-prompted.
        2. **Writes** any value supplied in ``values`` for a key that is missing
           (or for every supplied key when ``overwrite=True``).
        3. Emits the ``vault://<service>/<KEY>`` reference for every key so the
           caller can drop resolvable refs straight into ``config.json`` (they
           round-trip through :meth:`resolve_ref`).

        Backend-agnostic: works against Vault or encrypted engine storage via the same
        ``get``/``set`` contract — ``vault://`` is just the canonical ref scheme.

        Args:
            service: Logical service name (the ``apps/<service>`` path segment).
            env_keys: The env var names the service consumes.
            values: Optional ``{KEY: value}`` to seed for missing (or all) keys.
            overwrite: When True, write every supplied value even if one exists.

        Returns:
            ``{service, refs: {KEY: "vault://<service>/<KEY>"}, present, written,
            missing}`` — ``present`` already existed, ``written`` were just stored,
            ``missing`` have neither a stored value nor a supplied one.
        """
        values = values or {}
        refs: dict[str, str] = {}
        present: list[str] = []
        written: list[str] = []
        missing: list[str] = []
        for key in env_keys:
            store_key = f"{service}/{key}"
            refs[key] = f"vault://{store_key}"
            existing = self._backend.get(store_key)
            if existing is not None and not overwrite:
                present.append(key)
                continue
            supplied = values.get(key)
            if supplied is not None and supplied != "":
                self._backend.set(store_key, supplied, service=service)
                written.append(key)
            elif existing is not None:
                # overwrite requested but no new value — keep the existing one.
                present.append(key)
            else:
                missing.append(key)
        return {
            "service": service,
            "refs": refs,
            "present": present,
            "written": written,
            "missing": missing,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_secrets_client(config: SecretsConfig | None = None) -> SecretsClient:
    """Create a :class:`SecretsClient` over the KV v2 store.

    Without ``config`` the settings come from ``SECRETS_VAULT_URL``,
    ``SECRETS_VAULT_MOUNT`` and the ``VAULT_*`` variables.
    """
    if config is None:
        config = SecretsConfig(
            backend=setting("SECRETS_BACKEND", "vault"),
            vault_url=setting("SECRETS_VAULT_URL"),
            vault_mount=setting("SECRETS_VAULT_MOUNT", "secret"),
            vault_auth_method=setting("VAULT_AUTH_METHOD", "auto"),
            vault_auth_mount=setting("VAULT_AUTH_MOUNT", "jwt"),
            vault_role=setting("VAULT_ROLE"),
            vault_path_prefix=setting("VAULT_PATH_PREFIX"),
            vault_role_id=setting("VAULT_ROLE_ID"),
            vault_secret_id=setting("VAULT_SECRET_ID"),
            vault_k8s_sa_token_path=setting(
                "VAULT_K8S_SA_TOKEN_PATH",
                "/var/run/secrets/kubernetes.io/serviceaccount/token",
            ),
        )

    url = config.vault_url or "http://127.0.0.1:8200"
    backend: SecretsBackend = VaultBackend(
        url=url,
        mount_point=config.vault_mount,
        auth_method=config.vault_auth_method,
        auth_mount=config.vault_auth_mount,
        role=config.vault_role,
        path_prefix=config.vault_path_prefix,
        role_id=config.vault_role_id,
        secret_id=config.vault_secret_id,
        k8s_sa_token_path=config.vault_k8s_sa_token_path,
    )
    logger.info("SecretsClient initialised with 'vault' backend.")
    return SecretsClient(backend=backend)
