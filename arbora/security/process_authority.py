"""The host's process authority: the ``authority`` field of ``HostContext`` (ARBORA-HOST-R039.9).

The process mints one verified graph session and renews its credential lease on
a supervisor thread. This object owns that session slot, the renewal lock, the
stop event and the supervisor thread, so two hosts hold two separate
authorities and no module holds any of them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from arbora.core.settings import RuntimeConfig
from arbora.security.key_cache import JwksCache
from arbora.security.run_token import RunTokenSigner, token_secret_from_config

__all__ = ["ProcessAuthority"]


def _keys_or_new(keys: JwksCache | None, config: RuntimeConfig) -> JwksCache:
    return keys if keys is not None else JwksCache(config)


class ProcessAuthority:
    """The process's session slot and its renewal supervisor state.

    The session slot is bound only on the stdio transport; a network request
    never falls back to it. ``refresh_lock`` serializes renewals of the one
    credential lease, ``stop`` ends the supervisor loop and every wait that
    must yield to shutdown. ``signer`` mints and validates this host's run tokens.
    """

    __slots__ = (
        "_lock",
        "config",
        "_session",
        "_thread",
        "keys",
        "refresh_lock",
        "signer",
        "stop",
    )

    def __init__(
        self,
        keys: JwksCache | None = None,
        token_secret: Callable[[], str | None] = token_secret_from_config,
        *,
        config: RuntimeConfig,
    ) -> None:
        #: The host's settings view; renewal reads its identity settings from it.
        self.config = config
        #: The host's key cache; boot and renewal validate the process token against it.
        self.keys = _keys_or_new(keys, self.config)
        self.signer = RunTokenSigner(token_secret)
        self._lock = threading.Lock()
        self._session: Any = None
        self._thread: threading.Thread | None = None
        self.refresh_lock = threading.Lock()
        self.stop = threading.Event()

    def session(self) -> Any:
        """The bound process session, or ``None`` when none is bound."""
        with self._lock:
            return self._session

    def bind_session(self, session: Any) -> None:
        """Bind (or with ``None`` clear) the process session."""
        with self._lock:
            self._session = session

    def bind_thread(self, thread: threading.Thread) -> None:
        """Record the supervisor thread that renews this authority."""
        with self._lock:
            self._thread = thread

    def take_thread(self) -> threading.Thread | None:
        """Hand back the supervisor thread and forget it."""
        with self._lock:
            thread, self._thread = self._thread, None
            return thread
