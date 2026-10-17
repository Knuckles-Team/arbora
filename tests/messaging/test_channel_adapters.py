"""The public channel adapters are supplied by Arbora."""

from __future__ import annotations

import sys
from importlib import import_module

import pytest

from arbora.messaging.base import MessagingBackend


@pytest.mark.parametrize(
    ("platform", "class_name"),
    [
        ("mattermost", "MattermostBackend"),
        ("telegram", "TelegramBackend"),
    ],
)
def test_arbora_owns_channel_adapter(platform: str, class_name: str) -> None:
    adapter = getattr(
        import_module(f"arbora.messaging.backends.{platform}"), class_name
    )

    assert issubclass(adapter, MessagingBackend)
    assert adapter.__module__ == f"arbora.messaging.backends.{platform}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "class_name", "sdk_module", "extra"),
    [
        ("mattermost", "MattermostBackend", "mattermostdriver", "messaging-mattermost"),
        ("telegram", "TelegramBackend", "telegram.ext", "messaging-telegram"),
    ],
)
async def test_connect_names_the_extra_when_the_sdk_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    class_name: str,
    sdk_module: str,
    extra: str,
) -> None:
    """arbora.messaging imports cleanly without any platform SDK installed.

    Each backend's own ``connect()`` defers its SDK import so the module itself
    is always importable; only calling ``connect()`` without that one platform's
    SDK installed should fail, with a message naming the extra to install.
    """
    adapter = getattr(
        import_module(f"arbora.messaging.backends.{platform}"), class_name
    )
    # A None entry in sys.modules makes the next `import <sdk_module>` raise
    # ImportError, simulating the SDK not being installed, without uninstalling it.
    monkeypatch.setitem(sys.modules, sdk_module, None)

    with pytest.raises(ImportError, match=extra):
        await adapter().connect()
