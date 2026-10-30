#!/usr/bin/env python3
"""Run an ephemeral loopback certification authority until interrupted."""

import json
import signal
import threading

from arbora.certification.oidc import EphemeralLoopbackOidcAuthority


def main() -> int:
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with EphemeralLoopbackOidcAuthority() as authority:
            print(
                json.dumps({"issuer": authority.issuer, "ca_pem": authority.ca_pem}),
                flush=True,
            )
            stopped.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
