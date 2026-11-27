"""The A2A identity of a natively admitted run, behind the ``NativeRunIdentity`` port (ARBORA-OPS-R050)."""

from __future__ import annotations

from typing import Any

from arbora.a2a import authority

__all__ = ["A2ARunIdentity"]


class A2ARunIdentity:
    """Owner reference, digest and ownership check the task authority already enforces."""

    metadata_schema = authority._A2A_METADATA_SCHEMA  # noqa: SLF001

    def owner_ref(self, session: Any) -> str:
        return authority.WorkItemA2AAuthority._owner_ref(session)  # noqa: SLF001

    def digest(self, value: Any) -> str:
        return authority._digest(value)  # noqa: SLF001

    def opaque_work_item_id(self, item_id: str) -> str:
        return authority.WorkItemA2AAuthority._work_item_id(item_id)  # noqa: SLF001

    def owned(self, item: Any, owner_ref: str) -> bool:
        return authority.WorkItemA2AAuthority._owned(item, owner_ref)  # noqa: SLF001
