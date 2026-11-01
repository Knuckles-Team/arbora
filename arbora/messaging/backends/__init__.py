"""Arbora messaging channel adapters.

Each backend module in this package implements the ``MessagingBackend`` ABC
for a specific messaging platform. Backends are lazily imported via
``importlib.metadata.entry_points`` to avoid pulling in platform-specific
dependencies unless explicitly installed.

Supported backends: ``telegram``, ``mattermost``, ``discord``, ``slack``, ``whatsapp``,
``teams``, ``googlechat``, ``googlemeet``, ``matrix``, ``irc``, ``signal``, ``imessage``,
``line``, ``twitch``, ``synology``, ``voicecall`` and ``nextcloud``.

Install individual backends with::

    pip install arbora[messaging-telegram]

Or install all currently-shipped backends with::

    pip install arbora[messaging]
"""

# CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction — Native Messaging Backend Abstraction
