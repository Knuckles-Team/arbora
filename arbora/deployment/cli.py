#!/usr/bin/python
"""``setup-config`` console entry — generate / validate / document the full config.

Wraps :mod:`arbora.deployment.config_generator` so a deployment (or Claude
setting itself up) can produce a COMPLETE profile-seeded ``config.json``, validate a
config's completeness/health, or dump the grouped option reference — matching the
``graph_configure`` MCP actions and the ``arbora-deployment`` skill.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from .config_generator import (
    PROFILES,
    config_doctor,
    config_reference,
    write_config,
)
from .genesis_environments import (
    EnvironmentProfileError,
    list_environment_profiles,
    load_environment_profile,
    profile_summary,
)


def _run_release_plan(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .release_candidate import CandidateError, plan_candidate

    try:
        result = plan_candidate(
            Path(args.candidate),
            trusted_digest=args.trusted_digest,
            profile_name=args.profile,
        )
    except CandidateError as exc:
        print(json.dumps({"status": "blocked", "executed": False, "code": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _run_deploy(args: argparse.Namespace) -> int:
    """Ask the served door where a workload runs and print what it returns."""
    import asyncio
    import os
    from pathlib import Path

    from arbora.client.http import HttpTransport, OperationError
    from arbora.client.invoke import invoke

    base_url = args.url or os.environ.get("ARBORA_URL", "")
    token = os.environ.get("ARBORA_TOKEN", "")
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        transport = HttpTransport(base_url, token)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "code": "INVALID_REQUEST"}))
        print(str(exc), file=sys.stderr)
        return 2
    try:
        response = asyncio.run(invoke(transport, "deploy.placement.plan", request))
    except OperationError as exc:
        print(json.dumps({"status": "refused", "code": exc.code}))
        return 1
    result = response.get("result", {})
    result = result.get("value", result) if isinstance(result, dict) else {}
    print(
        json.dumps(
            {"decision": result.get("decision"), "graph": result.get("assembly")},
            indent=2,
        )
    )
    return 0


def _run_onboard(args: argparse.Namespace) -> int:
    """Record one deploy or reuse choice per declared component (ARBORA-DEPLOY-R013.2)."""
    from pathlib import Path

    from .component_choices import (
        ChoiceRefused,
        ask_once_per_component,
        build_inventory,
    )
    from .manifest import ManifestRefused, load_manifest

    try:
        manifest = load_manifest(Path(args.root), args.manifest)
        declared = [component.id for component in manifest.components]
        if args.answers:
            answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
            inventory = build_inventory(declared, answers)
        else:

            def ask(component: str) -> dict[str, str] | None:
                action = input(f"{component}: deploy or reuse? ").strip()
                if action == "reuse":
                    return {
                        "action": "reuse",
                        "endpoint_ref": input("endpoint_ref: ").strip(),
                    }
                return {"action": action} if action else None

            inventory = ask_once_per_component(declared, ask, profile=args.profile)
    except ManifestRefused as exc:
        print(json.dumps({"status": "refused", "code": exc.code}))
        return 1
    except ChoiceRefused as exc:
        print(json.dumps({"status": "refused", "code": exc.code, "detail": exc.detail}))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "code": "INVALID_REQUEST"}))
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "choices": [c.evidence() for c in inventory.choices],
                "choices_digest": inventory.choices_digest,
                "placement_components": inventory.placement_components(),
                "reuse_components": list(inventory.reuse_components),
            },
            indent=2,
        )
    )
    return 0


def _run_generate(args: argparse.Namespace) -> int:
    res = write_config(args.profile, args.out)
    print(json.dumps(res, indent=2))
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    res = config_doctor(args.profile, args.config)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("healthy") else 1


def _run_reference(_: argparse.Namespace) -> int:
    print(json.dumps(config_reference(), indent=2, default=str))
    return 0


def _run_environments(args: argparse.Namespace) -> int:
    if args.environments_command == "list":
        catalog = list_environment_profiles()
        print(
            json.dumps(
                {name: str(path) for name, path in sorted(catalog.items())},
                indent=2,
            )
        )
        return 0
    try:
        profile = load_environment_profile(args.name)
    except EnvironmentProfileError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    if args.environments_command == "show":
        print(json.dumps(profile_summary(profile), indent=2))
        return 0
    if args.environments_command == "validate":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "profile": args.name,
                    "source": str(profile.source),
                },
                indent=2,
            )
        )
        return 0
    return 2


def _run_engine_admission(args: argparse.Namespace) -> int:
    from .config_generator import generate_engine_admission

    print(
        json.dumps(
            generate_engine_admission(
                principal=args.principal,
                audience=args.audience,
                tenant=args.tenant,
                policy_version=args.policy_version,
                allowed_private_hosts=args.allowed_private_host,
            ),
            indent=2,
        )
    )
    return 0


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "onboard": _run_onboard,
    "engine-admission": _run_engine_admission,
    "release-plan": _run_release_plan,
    "deploy": _run_deploy,
    "generate": _run_generate,
    "doctor": _run_doctor,
    "reference": _run_reference,
    "environments": _run_environments,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup-config",
        description="Generate, validate, and document the full meristem config.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Write a complete config.json for a profile.")
    g.add_argument("--profile", choices=list(PROFILES), default="tiny")
    g.add_argument(
        "--out", default=None, help="Output path (default: XDG config.json)."
    )

    e = sub.add_parser(
        "engine-admission",
        help="Print the engine admission a served deployment must set (JSON).",
    )
    e.add_argument("--principal", required=True, help="Arbora process principal.")
    e.add_argument("--audience", required=True)
    e.add_argument("--tenant", required=True)
    e.add_argument("--policy-version", required=True)
    e.add_argument(
        "--allowed-private-host",
        action="append",
        default=[],
        metavar="HOST",
        help="Private or cluster hostname of a fleet connector (repeatable).",
    )

    d = sub.add_parser("doctor", help="Validate config completeness/health.")
    d.add_argument("--profile", choices=list(PROFILES), default=None)
    d.add_argument(
        "--config", default=None, help="config.json to check (default: live)."
    )

    sub.add_parser("reference", help="Print every option grouped by subsystem (JSON).")

    hf = sub.add_parser(
        "harness-fence",
        help="Write a governance-derived Claude Code permission fence (CONCEPT:MERISTEM-OS.deployment.governance-derived-claude-code).",
    )
    hf.add_argument(
        "--target",
        default=None,
        help="Claude config dir (default: ~/.claude). Writes settings.json + ../.claudeignore.",
    )
    hf.add_argument(
        "--policy", default=None, help="ActionPolicy YAML (default: shipped policy)."
    )
    hf.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the fence that would be written without touching disk.",
    )

    env = sub.add_parser(
        "environments",
        help="Named genesis k8s deployment-input profiles (dev/test/prod + extensions).",
    )
    env_sub = env.add_subparsers(dest="environments_command", required=True)
    env_sub.add_parser(
        "list", help="List discovered profile names and their source file."
    )
    env_show = env_sub.add_parser(
        "show",
        help="Print one profile's fully-resolved, reviewable values (no secret values).",
    )
    env_show.add_argument(
        "name", help="Profile name, e.g. dev, test, prod, or an extension."
    )
    env_validate = env_sub.add_parser(
        "validate",
        help="Load + validate one profile; exit non-zero and name the problem on failure.",
    )
    env_validate.add_argument("name", help="Profile name to validate.")

    release = sub.add_parser(
        "release-plan", help="Validate a trusted release candidate; never apply."
    )
    release.add_argument("candidate", help="Candidate JSON file.")
    release.add_argument(
        "--trusted-digest",
        required=True,
        help="Expected canonical sha256 digest; signature verification is always required.",
    )
    release.add_argument(
        "--profile", required=True, help="Existing named deployment profile."
    )

    from arbora.deployment import render

    render.add_arguments(
        sub.add_parser("render", help="Render the production topology manifests.")
    )

    onboard = sub.add_parser(
        "onboard",
        help="Record a deploy or reuse choice for every component of a manifest.",
    )
    onboard.add_argument("manifest", help="Manifest name under --root.")
    onboard.add_argument(
        "--root", required=True, help="Directory of deployment manifests."
    )
    onboard.add_argument(
        "--profile", default="", help="Profile recorded with each choice."
    )
    onboard.add_argument(
        "--answers", help="JSON list of choices; omit to be asked per component."
    )
    deploy = sub.add_parser(
        "deploy",
        help="Print the engine's placement decision and graph for a workload.",
    )
    deploy.add_argument(
        "request", help="JSON file with the deploy.placement.plan parameters."
    )
    deploy.add_argument("--url", help="Served Arbora base URL (default: ARBORA_URL).")

    args = parser.parse_args(argv)
    if args.command == "render":
        return render.run(args)
    handler = _COMMAND_HANDLERS.get(args.command)
    return handler(args) if handler is not None else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
