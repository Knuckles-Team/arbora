"""``arbora-identity``: the operator identity commands (ARBORA-IDENTITY-R013, R015).

``reconcile`` is served by the client-contract reconciler. The account lifecycle commands
the runbook names (``claim``, ``link-claim``, ``transition``, ``reset-admin``, ``rotate``)
are registered here so the document and the code agree on the command set; each refuses
``UNAVAILABLE`` with exit status 3 until its row in ``ARBORA-IDENTITY-R013`` is built.
Nothing here prompts for or accepts a password on a command line.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

UNAVAILABLE_EXIT = 3
USAGE_EXIT = 2

#: Account lifecycle commands registered ahead of their engine-backed handlers.
PENDING_COMMANDS: tuple[str, ...] = (
    "claim",
    "link-claim",
    "transition",
    "reset-admin",
    "rotate",
)
COMMANDS: tuple[str, ...] = ("reconcile", *PENDING_COMMANDS)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        print(json.dumps({"status": "usage", "commands": list(COMMANDS)}))
        return USAGE_EXIT
    command, rest = args[0], args[1:]
    if command == "reconcile":
        from arbora.identity.client_contract import reconcile_command

        return reconcile_command(rest)
    print(
        json.dumps(
            {
                "status": "UNAVAILABLE",
                "command": command,
                "row": "ARBORA-IDENTITY-R013",
            }
        )
    )
    return UNAVAILABLE_EXIT
