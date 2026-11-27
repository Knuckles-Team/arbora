"""``arbora skills export``: write the packaged skills stamped with the release digest."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from arbora.api.domain.fleet.mcp_resource_reconciliation import PublicationRefusedError
from arbora.fleet.agent_surface import deployed_surface, export_skills

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbora skills export")
    parser.add_argument("target", type=Path)
    parser.add_argument("--digest-stamp", action="store_true")
    args = parser.parse_args(argv)
    from arbora.api.ops.registry_factory import get_registry

    source = Path(__file__).resolve().parents[1] / "skills"
    surface = deployed_surface(get_registry(), clock_ms=lambda: int(time.time() * 1000))
    try:
        written = asyncio.run(
            export_skills(surface, source, args.target, digest_stamp=args.digest_stamp)
        )
    except PublicationRefusedError as refused:
        print(f"{refused.result.code}: {refused.result.reason}", file=sys.stderr)
        return 3
    print("\n".join(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
