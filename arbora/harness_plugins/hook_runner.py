"""The command hook runner (ARBORA-HARNESS-PLUGINS-R007, R003.4).

``arbora plugin hook <lifecycle point> --harness ID`` reads the harness hook JSON
on stdin, maps it to one workstream operation, calls the door with the login
credential and prints the profile's output form. It always exits 0 and stops
after 5 seconds: a hook never blocks the prompt or the tool call. A missing or
refused credential prints a one-line ``AUTH_REQUIRED`` note instead.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from arbora.harness_plugins.login import CredentialState, TokenStore, renew
from arbora.harness_plugins.profiles import HarnessProfile, Lifecycle

TIMEOUT_S = 5.0
OPS = {
    Lifecycle.FIRST_PROMPT: "workstream.attach",
    Lifecycle.BEFORE_COMPACTION: "workstream.checkpoint",
    Lifecycle.SESSION_END: "workstream.detach",
}

Post = Callable[[str, Mapping[str, Any], Mapping[str, str]], httpx.Response]


def auth_required_note(profile: HarnessProfile) -> str:
    return f"arbora: AUTH_REQUIRED - run `arbora plugin login --harness {profile.id}` to share this session's workstream."


def params_for(
    point: Lifecycle, profile: HarnessProfile, hook: Mapping[str, Any]
) -> dict[str, Any]:
    session = str(hook.get("session_id") or hook.get("sessionID") or "")
    if point is Lifecycle.FIRST_PROMPT:
        return {
            "harness": profile.id,
            "harness_session_id": session,
            "workspace_root": str(hook.get("cwd") or hook.get("directory") or ""),
        }
    if point is Lifecycle.BEFORE_COMPACTION:
        trigger = str(hook.get("trigger") or "auto")
        return {
            "harness_session_id": session,
            "summary": f"Context compacted ({trigger}); run the handoff command for a full handoff.",
            "trigger": "compact",
        }
    return {"harness_session_id": session}


#: The version a skill hook reports: the harness hook input carries none.
SKILL_VERSION = "unversioned"


def skill_log_body(hook: Mapping[str, Any], now_ns: int) -> dict[str, Any] | None:
    """One OTLP log record for one ended skill use (ARBORA-HARNESS-PLUGINS-R012).

    Only the skill name, the session id and the outcome are read from the hook
    input; the prompt, the arguments and the output never enter the body. A
    skill the user typed as a command is not a tool call, so a tool hook only
    sees an agent choice.
    """
    tool_input = hook.get("tool_input")
    skill = (
        str(tool_input.get("skill") or "") if isinstance(tool_input, Mapping) else ""
    )
    session = str(hook.get("session_id") or hook.get("sessionID") or "")
    if not skill or not session:
        return None
    response = hook.get("tool_response")
    failed = bool(hook.get("error")) or (
        isinstance(response, Mapping) and bool(response.get("is_error"))
    )
    attributes = {
        "skill.ref": skill,
        "skill.version": SKILL_VERSION,
        "skill.chosen_by": "agent",
        "session.run_id": session,
        "skill.outcome": "error" if failed else "ok",
    }
    if failed:
        attributes["skill.error_code"] = "SKILL_FAILED"
    record = {
        "timeUnixNano": str(now_ns),
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in sorted(attributes.items())
        ],
    }
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}


def output_form(point: Lifecycle, profile: HarnessProfile, context: str) -> str:
    """Hook stdout in the form the harness reads."""
    if not context:
        return ""
    if profile.id == "opencode":
        return json.dumps({"context": context})
    event = profile.event(point) or ""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    )


def _default_post(
    url: str, body: Mapping[str, Any], headers: Mapping[str, str]
) -> httpx.Response:
    return httpx.post(url, json=dict(body), headers=dict(headers), timeout=TIMEOUT_S)


def run(
    point: Lifecycle,
    profile: HarnessProfile,
    stdin_text: str,
    *,
    base_url: str,
    store: TokenStore,
    post: Post = _default_post,
    now_ns: Callable[[], int] = time.time_ns,
) -> str:
    """The hook output; never raises."""
    try:
        hook = json.loads(stdin_text or "{}")
        if point is Lifecycle.SKILL_END:
            return _report_skill(
                profile,
                hook if isinstance(hook, Mapping) else {},
                base_url=base_url,
                store=store,
                post=post,
                now_ns=now_ns(),
            )
        params = params_for(point, profile, hook if isinstance(hook, Mapping) else {})
        if not params["harness_session_id"]:
            return ""
        headers: dict[str, str] = {}
        credential = store.load(profile.id)
        if credential is not None:
            if credential.state is CredentialState.AUTHORIZED:
                credential = renew(credential, store=store)
            if credential.state is not CredentialState.AUTHORIZED:
                return auth_required_note(profile)
            headers["Authorization"] = f"Bearer {credential.access_token}"
        response = post(
            f"{base_url.rstrip('/')}/api/v1/ops/{OPS[point]}", params, headers
        )
        if response.status_code in (401, 403):
            return auth_required_note(profile)
        payload = response.json()
        value = (payload.get("result") or {}).get("value") or {}
        return output_form(point, profile, str(value.get("context") or ""))
    except Exception:
        logging.getLogger(__name__).debug(
            "a hook never blocks the harness", exc_info=True
        )
        return ""


def _report_skill(
    profile: HarnessProfile,
    hook: Mapping[str, Any],
    *,
    base_url: str,
    store: TokenStore,
    post: Post,
    now_ns: int,
) -> str:
    """Post one skill log record; with no usable credential nothing is sent and
    nothing is printed, so a skill use is never blocked or noisy (R003.4)."""
    body = skill_log_body(hook, now_ns)
    credential = store.load(profile.id)
    if body is None or credential is None:
        return ""
    if credential.state is CredentialState.AUTHORIZED:
        credential = renew(credential, store=store)
    if credential.state is not CredentialState.AUTHORIZED:
        return ""
    post(
        f"{base_url.rstrip('/')}/api/v1/logs",
        body,
        {"Authorization": f"Bearer {credential.access_token}"},
    )
    return ""


__all__ = [
    "OPS",
    "skill_log_body",
    "auth_required_note",
    "output_form",
    "params_for",
    "run",
]
