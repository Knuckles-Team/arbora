"""Every environment read goes through the declared accessor (ARBORA-HOST-R010.2)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arbora.core import environment as env
from arbora.core import environment_manifest as em
from arbora.core.environment_manifest import EnvironmentRefusal

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "arbora"
TABLE = SOURCE / "core" / "environment_names.py"


def _text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in SOURCE.rglob("*.py") if p != TABLE
    )


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_an_undeclared_name_refuses_before_it_reads(monkeypatch) -> None:
    monkeypatch.setenv("UNDECLARED_PROBE_NAME", "never-returned")
    dynamic = "UNDECLARED_PROBE" + "_NAME"
    for read in (env.read_env, env.read_text):
        with pytest.raises(EnvironmentRefusal) as refused:
            read(dynamic)
        assert refused.value.code == "ENV_UNDECLARED_READ"
        assert refused.value.names == (dynamic,)
        assert "never-returned" not in str(refused.value)
    with pytest.raises(EnvironmentRefusal):
        env.read_reference("not a name")
    with pytest.raises(EnvironmentRefusal) as write:
        env.write_env("lower_case", "x")
    assert write.value.code == "ENV_UNDECLARED_WRITE"


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_declared_values_are_typed_by_their_declaration(monkeypatch) -> None:
    assert env.read_env("A2A_PUSH_TIMEOUT_S") == "10"
    monkeypatch.setenv("A2A_PUSH_TIMEOUT_S", "7")
    assert env.read_env("A2A_PUSH_TIMEOUT_S") == "7"
    assert env.read_env("ACTION_IRREVERSIBILITY_AVERSION") is False
    monkeypatch.setenv("ACTION_IRREVERSIBILITY_AVERSION", "yes")
    assert env.read_env("ACTION_IRREVERSIBILITY_AVERSION") is True
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "2.5")
    assert env.read_env("ENGINE_BREAKER_COOLDOWN") == 2.5
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "soon")
    assert env.read_env("ENGINE_BREAKER_COOLDOWN") == 15.0
    monkeypatch.delenv("ARBORA_TOKEN_SECRET", raising=False)
    assert env.read_env("ARBORA_TOKEN_SECRET") is None
    assert env.read_env("ARBORA_TOKEN_SECRET", "computed") == "computed"
    monkeypatch.setenv("REFERENCE_TARGET_X", "from-env")
    assert env.read_reference("REFERENCE_TARGET_X") == "from-env"
    assert env.read_reference("REFERENCE_UNSET_X") == ""
    assert env.read_reference("REFERENCE_UNSET_X", None) is None


def _unread(names: list[str], text: str, read: set[str | None]) -> list[str]:
    """The literal names that no source mentions and the census never read."""
    return [n for n in names if "{" not in n and n not in text and n not in read]


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_every_table_row_names_its_type_default_secret_flag_and_owner_row() -> None:
    from arbora.core.environment_names import NAMES

    rows = set()
    for spec in (ROOT / "specs").glob("*/requirements.md"):
        rows.update(re.findall(r"\| `([A-Z0-9-]+-R[0-9.]+)` \|", spec.read_text()))
    text = _text()
    read = {r.name for r in em.package_census()}
    names = [row[0] for row in NAMES]
    assert _unread(names, text, read) == [], "declared but no source reads them"
    assert names == sorted(set(names)), "one row per name, in order"
    for name, kind, default, owner, sites, sensitive in NAMES:
        assert kind in em._TYPES, name
        assert owner in rows, (name, owner)
        assert not (sensitive and default), name
        assert default is None or em._valid(kind, default) or kind == "string", name
        for site in sites:
            module = SOURCE.parent / (site.replace(".", "/") + ".py")
            assert module.exists() or module.with_suffix("").is_dir(), (name, site)


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_a_tool_family_toggle_is_a_declared_pattern_with_a_fixed_shape(
    monkeypatch,
) -> None:
    assert env.read_env("CMDBTOOL", True) is True
    monkeypatch.setenv("CMDBTOOL", "false")
    assert env.read_env("CMDBTOOL", True) is False
    for refused in ("cmdbtool", "CMDB_TOOLS", "1TOOL"):
        with pytest.raises(EnvironmentRefusal) as raised:
            env.read_env(refused)
        assert raised.value.code == "ENV_UNDECLARED_READ"


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_a_platform_family_declares_exactly_the_platforms_that_exist(
    monkeypatch,
) -> None:
    from arbora.messaging.capabilities import CAPABILITY_MATRIX

    for platform in CAPABILITY_MATRIX:
        monkeypatch.setenv(f"MESSAGING_{platform.upper()}_TOKEN", "t")
        assert env.read_env(f"MESSAGING_{platform.upper()}_TOKEN") == "t"
    for refused in ("MESSAGING_NOSUCHPLATFORM_TOKEN", "MESSAGING_TOKEN"):
        with pytest.raises(EnvironmentRefusal) as raised:
            env.read_env(refused)
        assert raised.value.code == "ENV_UNDECLARED_READ"


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_the_process_view_reads_declared_names_and_projects_writes(monkeypatch) -> None:
    view = env.PROCESS_ENVIRONMENT
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert view.get("SSL_CERT_FILE") is None
    view["SSL_CERT_FILE"] = "/trust/ca.pem"
    assert view["SSL_CERT_FILE"] == "/trust/ca.pem"
    assert env.read_text("SSL_CERT_FILE") == "/trust/ca.pem"
    del view["SSL_CERT_FILE"]
    assert "SSL_CERT_FILE" not in env.environment_names()
    monkeypatch.setenv("UNDECLARED_VIEW_NAME", "never-returned")
    with pytest.raises(EnvironmentRefusal) as refused:
        view.get("UNDECLARED_VIEW_NAME")
    assert refused.value.code == "ENV_UNDECLARED_READ"
    copy = env.copy_environment(view)
    assert copy["UNDECLARED_VIEW_NAME"] == "never-returned"
    assert env.copy_environment({"A": "1"}) == {"A": "1"}
