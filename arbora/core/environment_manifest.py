"""The host environment manifest and startup validation (ARBORA-HOST-R010.2 to R010.8).

``load_environment_manifest`` composes one value-free list of every environment name
the host consumes: Arbora's own settings and reads, the ``scion_sdk`` public export and
the ``meristem`` public export. ``validate_startup`` resolves the environment once and
checks every enabled component before any listener or child process opens. Evidence is
names, codes and digests, never a value.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from arbora.core.settings import ArboraConfig, credential_shaped

__all__ = [
    "ARBORA_COMPONENT",
    "SCHEMA_VERSION",
    "EnvironmentDeclaration",
    "EnvironmentRead",
    "EnvironmentRefusal",
    "HostEnvironmentManifest",
    "HostStartupConfiguration",
    "census",
    "check_census",
    "check_guidance",
    "enforce_startup",
    "example_names",
    "load_environment_manifest",
    "validate_startup",
]

SCHEMA_VERSION = 1
ARBORA_COMPONENT = "arbora"
_TYPES = frozenset({"string", "int", "bool", "secret_reference", "enum"})
_CONDITION = re.compile(r"^when:([A-Za-z][A-Za-z0-9_]*)$")
_SECRET = re.compile(r"^(env|vault|openbao|file|secret)://", re.IGNORECASE)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ARBORA_SITE = "arbora.core.settings.ArboraConfig"


class EnvironmentRefusal(RuntimeError):
    """A named refusal. ``names`` holds variable names and components, never values."""

    def __init__(
        self, code: str, names: Iterable[str] = (), *, pin_lag: bool = False
    ) -> None:
        self.code = code
        #: True when every named owner lacks the export only because the pinned
        #: release predates it, not because the export is empty.
        self.pin_lag = pin_lag
        self.names = tuple(sorted(set(names)))
        super().__init__(f"{code}: {', '.join(self.names)}" if self.names else code)


@dataclass(frozen=True)
class EnvironmentDeclaration:
    """One environment name a component reads, declared without its value."""

    name: str
    component: str
    type: str
    required_when: str
    default: str | None
    sensitive: bool
    call_sites: tuple[str, ...]
    source_revision: str = ""
    schema_version: int = SCHEMA_VERSION

    def row(self) -> list[Any]:
        return [
            self.name,
            self.component,
            self.type,
            self.required_when,
            self.default,
            self.sensitive,
            list(self.call_sites),
        ]


@dataclass(frozen=True)
class HostEnvironmentManifest:
    schema_version: int
    component: str
    source_revision: str
    declarations: tuple[EnvironmentDeclaration, ...]

    @property
    def digest(self) -> str:
        rows = [d.row() for d in self.declarations]
        return _digest([self.schema_version, self.component, rows])

    def names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.declarations)


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Arbora's own declarations ---------------------------------------------------

#: Reads outside ``ArboraConfig``: name, type, call site. The census keeps this exact.
_OTHER_READS: tuple[tuple[str, str, str], ...] = (
    ("ARBORA_AUTH_NONE_EXPOSE", "bool", "arbora.security.anonymous"),
    ("ARBORA_DEPLOYED_WORKTREE", "string", "arbora.deployment.deployed_source"),
    ("ARBORA_DEPLOY_CONFIG", "string", "arbora.identity.client_enrollment_cli"),
    ("ARBORA_MCP_TASK_CHANNEL_SECRET", "secret_reference", "arbora.fleet.multiplexer"),
    ("ARBORA_PLUGIN_FILE_STORE", "string", "arbora.harness_plugins.login"),
    ("ARBORA_PLUGIN_SKILL_ROOTS", "string", "arbora.harness_plugins.cli"),
    ("ARBORA_PUBLIC_URL", "string", "arbora.harness_plugins.cli"),
    ("ARBORA_SOURCE_REPO", "string", "arbora.deployment.deployed_source"),
    ("ARBORA_TOKEN", "secret_reference", "arbora.deployment.cli"),
    ("ARBORA_URL", "string", "arbora.deployment.cli"),
    ("ARBORA_WEBUI_PORT", "int", "arbora.webui_host.webui_co_service"),
    ("HOME", "string", "arbora.harness_plugins.cli"),
    ("XDG_CACHE_HOME", "string", "arbora.harness_plugins.cli"),
    ("XDG_CONFIG_HOME", "string", "arbora.harness_plugins.install"),
    ("XDG_RUNTIME_DIR", "string", "arbora.identity.client_enrollment"),
)

#: Reads whose name arrives from the caller. The caller's literal names are declared on
#: their own, so a pass-through reader adds no declaration.
_PASS_THROUGH: frozenset[tuple[str, str]] = frozenset(
    {
        ("arbora/agent_runs.py", "api_key_env"),
        ("arbora/core/settings.py", "str(info.alias)"),
        ("arbora/core/settings.py", "alias"),
        ("arbora/core/settings.py", "env_key"),
        ("arbora/mcp_server/backing/tools/analysis_tools.py", "env_key"),
    }
)


def _field_type(annotation: Any, alias: str) -> str:
    if credential_shaped(alias) and alias.endswith("_REF"):
        return "secret_reference"
    if get_origin(annotation) is Literal:
        return "enum"
    members = {a for a in get_args(annotation) if a is not type(None)} or {annotation}
    if members == {bool}:
        return "bool"
    if members <= {int}:
        return "int"
    return "string"


def _field_default(info: Any, kind: str, sensitive: bool) -> str | None:
    if sensitive or kind == "secret_reference" or info.default_factory is not None:
        return None
    value = info.default
    if value is None or not isinstance(value, str | int | bool):
        return None
    return str(value).lower() if isinstance(value, bool) else str(value)


def _arbora_field_declarations(revision: str) -> list[EnvironmentDeclaration]:
    out: list[EnvironmentDeclaration] = []
    for info in ArboraConfig.model_fields.values():
        alias = str(info.alias)
        kind = _field_type(info.annotation, alias)
        sensitive = credential_shaped(alias)
        out.append(
            EnvironmentDeclaration(
                name=alias,
                component=ARBORA_COMPONENT,
                type=kind,
                required_when="optional",
                default=_field_default(info, kind, sensitive),
                sensitive=sensitive,
                call_sites=(_ARBORA_SITE,),
                source_revision=revision,
            )
        )
    return out


def _arbora_other_declarations(revision: str) -> list[EnvironmentDeclaration]:
    return [
        EnvironmentDeclaration(
            name=name,
            component=ARBORA_COMPONENT,
            type=kind,
            required_when="optional",
            default=None,
            sensitive=kind == "secret_reference",
            call_sites=(site,),
            source_revision=revision,
        )
        for name, kind, site in _OTHER_READS
    ]


def arbora_declarations(revision: str = "") -> tuple[EnvironmentDeclaration, ...]:
    """Arbora's own declarations: every settings alias and every other literal read."""
    items = _arbora_field_declarations(revision) + _arbora_other_declarations(revision)
    return tuple(sorted(items, key=lambda d: d.name))


# --- composition with the owners -------------------------------------------------


def _adopt(item: Any, component: str, revision: str) -> EnvironmentDeclaration:
    return EnvironmentDeclaration(
        name=item.name,
        component=component,
        type=item.type,
        required_when=item.required_when or "optional",
        default=item.default,
        sensitive=bool(item.sensitive),
        call_sites=tuple(item.call_sites),
        source_revision=revision,
    )


def _sdk_export() -> tuple[Iterable[Any], str]:
    import importlib

    from arbora.pin_lag.scion_sdk import require_environment_declarations

    module = require_environment_declarations()
    version = importlib.import_module("scion_sdk.version").__version__
    return module.environment_declarations(version).declarations, version


def _meristem_export() -> tuple[Iterable[Any], str]:
    from arbora.pin_lag.meristem import environment_declarations

    return environment_declarations(), ""


_Export = Callable[[], tuple[Iterable[Any], str]]
_OWNERS: tuple[tuple[str, _Export], ...] = (
    ("scion-sdk", _sdk_export),
    ("meristem", _meristem_export),
)


def _conflicts(items: Iterable[EnvironmentDeclaration]) -> list[str]:
    seen: dict[str, tuple[Any, ...]] = {}
    clash: list[str] = []
    for d in items:
        shape = (d.type, d.required_when, d.default, d.sensitive)
        if seen.setdefault(d.name, shape) != shape:
            clash.append(d.name)
    return clash


def _merge(
    items: Iterable[EnvironmentDeclaration],
) -> tuple[EnvironmentDeclaration, ...]:
    """One declaration per name; equal shapes union their call sites and components."""
    merged: dict[str, EnvironmentDeclaration] = {}
    for d in items:
        held = merged.get(d.name)
        if held is None:
            merged[d.name] = d
            continue
        sites = tuple(sorted({*held.call_sites, *d.call_sites}))
        merged[d.name] = EnvironmentDeclaration(
            **{**held.__dict__, "call_sites": sites},
        )
    return tuple(merged[name] for name in sorted(merged))


def _owner_declarations(
    owners: Iterable[tuple[str, _Export]],
) -> list[EnvironmentDeclaration]:
    out: list[EnvironmentDeclaration] = []
    unavailable: list[str] = []
    empty: list[str] = []
    for component, export in owners:
        try:
            declared, revision = export()
        except (ImportError, AttributeError, RuntimeError):
            unavailable.append(component)
            continue
        adopted = [_adopt(d, component, revision) for d in declared]
        if not adopted:
            empty.append(component)
        out.extend(adopted)
    if unavailable or empty:
        raise EnvironmentRefusal(
            "ENV_MANIFEST_UNAVAILABLE", [*unavailable, *empty], pin_lag=not empty
        )
    return out


def load_environment_manifest(
    *,
    source_revision: str = "",
    owners: Iterable[tuple[str, _Export]] | None = None,
) -> HostEnvironmentManifest:
    """Compose the host manifest, or raise a named refusal.

    ``ENV_MANIFEST_UNAVAILABLE`` names each owner whose public export is absent or
    empty; ``ENV_DECLARATION_CONFLICT`` names a variable two components declare with
    different shapes. Arbora adds no declaration for an owner's reads.
    """
    own = list(arbora_declarations(source_revision))
    items = own + _owner_declarations(_OWNERS if owners is None else owners)
    clash = _conflicts(items)
    if clash:
        raise EnvironmentRefusal("ENV_DECLARATION_CONFLICT", clash)
    return HostEnvironmentManifest(
        SCHEMA_VERSION, ARBORA_COMPONENT, source_revision, _merge(items)
    )


# --- the census ------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentRead:
    path: str
    line: int
    name: str | None
    expression: str

    @property
    def site(self) -> str:
        return f"{self.path}:{self.line}"


_READ_CALLS = frozenset({"os.environ.get", "os.getenv", "os.environ.pop"})


def _constants(tree: ast.AST) -> dict[str, str]:
    return {
        node.targets[0].id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _name_argument(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.Call) and node.args:
        return node.args[0] if ast.unparse(node.func) in _READ_CALLS else None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and ast.unparse(node.value) == "os.environ"
    ):
        return node.slice
    return None


def _file_reads(path: str, source: str) -> list[EnvironmentRead]:
    tree = ast.parse(source, filename=path)
    constants = _constants(tree)
    reads: list[EnvironmentRead] = []
    for node in ast.walk(tree):
        arg = _name_argument(node)
        if arg is None:
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            name: str | None = arg.value
        elif isinstance(arg, ast.Name) and arg.id in constants:
            name = constants[arg.id]
        else:
            name = None
        reads.append(EnvironmentRead(path, node.lineno, name, ast.unparse(arg)))
    return reads


def census(root: Path) -> tuple[EnvironmentRead, ...]:
    """Every ``os.environ``/``os.getenv`` read under ``root`` (an ``arbora`` tree)."""
    reads: list[EnvironmentRead] = []
    for file in sorted(root.rglob("*.py")):
        rel = f"{root.name}/{file.relative_to(root).as_posix()}"
        reads.extend(_file_reads(rel, file.read_text(encoding="utf-8")))
    return tuple(reads)


def check_census(
    reads: Iterable[EnvironmentRead],
    declared: Iterable[str],
    pass_through: Iterable[tuple[str, str]] = _PASS_THROUGH,
) -> None:
    """Refuse ``ENV_UNDECLARED_READ`` naming each read site no declaration covers."""
    known = set(declared)
    bound = set(pass_through)
    unknown = [
        r.site
        for r in reads
        if (r.name is None and (r.path, r.expression) not in bound)
        or (r.name is not None and r.name not in known)
    ]
    if unknown:
        raise EnvironmentRefusal("ENV_UNDECLARED_READ", unknown)


@cache
def package_census() -> tuple[EnvironmentRead, ...]:
    """The census of the installed ``arbora`` package."""
    return census(Path(__file__).resolve().parents[1])


# --- startup validation ----------------------------------------------------------


@dataclass(frozen=True)
class HostStartupConfiguration:
    """The resolved, validated startup configuration; carries no value."""

    manifest_digest: str
    components: tuple[str, ...]
    settings: tuple[tuple[str, str], ...]  # (name, "set" or "default")

    @property
    def digest(self) -> str:
        return _digest([self.manifest_digest, self.components, self.settings])


def _set(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "") != ""


def _truthy(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in _TRUE


def _valid(kind: str, text: str) -> bool:
    if kind == "int":
        return re.fullmatch(r"[+-]?\d+", text.strip()) is not None
    if kind == "bool":
        return text.strip().lower() in _TRUE | _FALSE
    if kind == "secret_reference":
        return _SECRET.match(text) is not None
    return True


def _check_defaults(manifest: HostEnvironmentManifest) -> list[str]:
    bad: list[str] = []
    for d in manifest.declarations:
        if d.default is None:
            continue
        if d.sensitive or d.type == "secret_reference" or not _valid(d.type, d.default):
            bad.append(d.name)
    return bad


def _unresolved_conditions(manifest: HostEnvironmentManifest) -> list[str]:
    names = manifest.names()
    return [
        d.name
        for d in manifest.declarations
        if d.required_when not in {"always", "optional"}
        and not ((m := _CONDITION.match(d.required_when)) and m.group(1) in names)
    ]


def _required(d: EnvironmentDeclaration, environ: Mapping[str, str]) -> bool:
    if d.required_when == "always":
        return True
    match = _CONDITION.match(d.required_when)
    return bool(match) and _truthy(environ, match.group(1))  # type: ignore[union-attr]


def _arbora_value_error(d: EnvironmentDeclaration, text: str) -> str | None:
    """The refusal code for a malformed Arbora settings value, else ``None``."""
    from arbora.core.settings import read_field_text

    try:
        read_field_text(d.name, text)
    except ValueError:
        return (
            "CONFIG_PROFILE_UNKNOWN"
            if d.name == "DEPLOYMENT_PROFILE"
            else "CONFIG_MALFORMED"
        )
    return None


def _value_refusals(
    manifest: HostEnvironmentManifest, environ: Mapping[str, str]
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for d in manifest.declarations:
        text = environ.get(d.name, "")
        if text == "":
            if _required(d, environ):
                found.setdefault("CONFIG_REQUIRED_MISSING", []).append(
                    f"{d.component}:{d.name}"
                )
            continue
        code = (
            _arbora_value_error(d, text)
            if d.call_sites == (_ARBORA_SITE,)
            else (None if _valid(d.type, text) else "CONFIG_MALFORMED")
        )
        if code:
            found.setdefault(code, []).append(f"{d.component}:{d.name}")
    return found


_CODE_ORDER = (
    "CONFIG_PROFILE_UNKNOWN",
    "CONFIG_MALFORMED",
    "CONFIG_REQUIRED_MISSING",
)


def validate_startup(
    environ: Mapping[str, str] | None = None,
    *,
    manifest: HostEnvironmentManifest | None = None,
    owners: Iterable[tuple[str, _Export]] | None = None,
) -> HostStartupConfiguration:
    """Validate every enabled component before any listener or child process starts.

    Arbora's own settings are checked first, so a malformed value refuses by name
    even when an owner export is unavailable. Refusals name the component and the
    variable, never the value: ``CONFIG_REQUIRED_MISSING``, ``CONFIG_MALFORMED``,
    ``CONFIG_PROFILE_UNKNOWN``, ``CONFIG_CONDITION_UNRESOLVED`` and
    ``CONFIG_DEFAULT_INVALID``; an unavailable owner export is
    ``ENV_MANIFEST_UNAVAILABLE``.
    """
    import os

    env = os.environ if environ is None else environ
    if manifest is None:
        own = HostEnvironmentManifest(
            SCHEMA_VERSION, ARBORA_COMPONENT, "", arbora_declarations()
        )
        _raise_first(_value_refusals(own, env))
        manifest = load_environment_manifest(owners=owners)
    unresolved = _unresolved_conditions(manifest)
    if unresolved:
        raise EnvironmentRefusal("CONFIG_CONDITION_UNRESOLVED", unresolved)
    bad_defaults = _check_defaults(manifest)
    if bad_defaults:
        raise EnvironmentRefusal("CONFIG_DEFAULT_INVALID", bad_defaults)
    _raise_first(_value_refusals(manifest, env))
    components = tuple(sorted({d.component for d in manifest.declarations}))
    settings = tuple(
        (d.name, "set" if _set(env, d.name) else "default")
        for d in manifest.declarations
    )
    return HostStartupConfiguration(manifest.digest, components, settings)


def _raise_first(found: Mapping[str, list[str]]) -> None:
    for code in _CODE_ORDER:
        if code in found:
            raise EnvironmentRefusal(code, found[code])


def _validate_own_only(refusal: EnvironmentRefusal) -> HostStartupConfiguration:
    """Pin lag: an owner's export is not in the pinned release, so only Arbora's own
    declarations can be checked. The owners are named as unvalidated, never assumed."""
    import logging
    import os

    own = HostEnvironmentManifest(
        SCHEMA_VERSION, ARBORA_COMPONENT, "", arbora_declarations()
    )
    logging.getLogger(__name__).warning(
        "environment manifest unavailable for %s; only arbora is validated",
        ", ".join(refusal.names),
    )
    return validate_startup(os.environ, manifest=own)


def enforce_startup() -> HostStartupConfiguration:
    """Validate the process environment; a refusal prints names and codes and exits 2."""
    import sys

    try:
        return validate_startup()
    except EnvironmentRefusal as refusal:
        if refusal.pin_lag:
            return _validate_own_only(refusal)
        print(f"arbora: startup refused: {refusal}", file=sys.stderr)
        raise SystemExit(2) from None


# --- guidance --------------------------------------------------------------------


def example_names(manifest: HostEnvironmentManifest) -> tuple[str, ...]:
    """The names a configuration example must show: every required or conditional one."""
    return tuple(d.name for d in manifest.declarations if d.required_when != "optional")


def check_guidance(manifest: HostEnvironmentManifest, *examples: str) -> str:
    """Refuse ``GUIDANCE_NAME_MISSING`` naming a required name an example omits.

    Returns the digest of the examples checked, which is evidence and holds no text.
    """
    missing = [
        name
        for name in example_names(manifest)
        if any(re.search(rf"\b{re.escape(name)}\b", text) is None for text in examples)
    ]
    if missing:
        raise EnvironmentRefusal("GUIDANCE_NAME_MISSING", missing)
    return _digest(list(examples))
