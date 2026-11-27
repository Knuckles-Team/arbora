"""File-part admission over the served A2A route (ARBORA-A2A-R001.5.3, R001.5.4)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.domain.a2a.parts import (
    MediaCapability,
    MediaLimits,
    capability_from_assembly,
    sniff_image_type,
)
from arbora.core.settings import runtime_config
from tests.a2a._parts_support import JPEG, PNG, WEBP, file_part, reason, send, served


def test_signatures_name_the_image_type() -> None:
    assert sniff_image_type(PNG) == "image/png"
    assert sniff_image_type(JPEG) == "image/jpeg"
    assert sniff_image_type(WEBP) == "image/webp"
    assert sniff_image_type(b"GIF89a....") is None


@pytest.mark.spec("ARBORA-A2A-R001.5.3")
def test_mismatched_signature_is_refused_with_zero_tasks() -> None:
    client, authority = served()
    # A JPEG declared as PNG, and an unlisted image type, each create no task.
    assert reason(send(client, [file_part(JPEG, "image/png")])) == (
        "file_part_type_mismatch"
    )
    assert reason(send(client, [file_part(PNG, "image/gif")])) == (
        "file_part_type_unsupported"
    )
    assert reason(send(client, [file_part(b"not an image at all", "image/webp")])) == (
        "file_part_type_mismatch"
    )
    assert authority.dispatched == []


@pytest.mark.spec("ARBORA-A2A-R001.5.4")
def test_request_byte_and_image_count_limits_refuse_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, authority = served()
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 2)
    three = [file_part(PNG, "image/png")] * 3
    assert reason(send(client, three)) == "image_part_count"
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 4)
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_request_media_bytes", 1_024)
    big = PNG + b"\x00" * 600
    assert reason(send(client, [file_part(big, "image/png")] * 2)) == (
        "request_media_too_large"
    )
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_file_part_bytes", 1_024)
    assert reason(send(client, [file_part(PNG + b"\x00" * 2_000, "image/png")])) == (
        "file_part_too_large"
    )
    assert authority.dispatched == []


def test_limit_table_is_bounded_to_its_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MediaLimits.from_config().max_image_parts == 4
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 9)
    with pytest.raises(ValueError):
        MediaLimits.from_config()


CAPABLE = MediaCapability(
    revision="a" * 64,
    accepts=(("image/png", 4_096), ("image/jpeg", 4_096)),
    max_parts=4,
)


def _send_revision(client: Any, parts: list[dict[str, Any]], revision: str) -> Any:
    from tests.a2a._parts_support import send_with_metadata

    return send_with_metadata(client, parts, {"arboraModelRevision": revision})


@pytest.mark.spec("ARBORA-A2A-R001.5.2")
def test_capable_model_accepts_png_and_unknown_stale_oversize_create_no_task() -> None:
    client, authority = served(CAPABLE)
    assert send(client, [file_part(PNG, "image/png")]).get("error") is None
    assert len(authority.dispatched) == 1

    # An unlisted type, an oversize part and a stale revision each refuse.
    refused = send(client, [file_part(WEBP, "image/webp")])
    assert refused["error"]["data"]["reason"] == "media_type_not_accepted"
    big = PNG + b"\x00" * 5_000
    assert send(client, [file_part(big, "image/png")])["error"]["data"]["reason"] == (
        "media_part_too_large"
    )
    stale = _send_revision(client, [file_part(PNG, "image/png")], "b" * 64)
    assert stale["error"]["message"] == "POLICY_DENIED"
    assert stale["error"]["data"]["reason"] == "media_capability_stale"
    current = _send_revision(client, [file_part(PNG, "image/png")], "a" * 64)
    assert current.get("error") is None
    assert len(authority.dispatched) == 2

    # An unknown capability never admits an image.
    unknown_client, unknown = served()
    body = send(unknown_client, [file_part(PNG, "image/png")])
    assert body["error"]["message"] == "UNAVAILABLE"
    assert body["error"]["data"]["reason"] == "media_capability_unknown"
    assert unknown.dispatched == []
    # Text parts and parts for the modality seam are never checked against it.
    text_only, text_authority = served()
    assert send(text_only, [file_part(b"hello", "text/plain")]).get("error") is None
    assert len(text_authority.dispatched) == 1


def test_assembly_answer_maps_to_the_capability() -> None:
    from types import SimpleNamespace

    answer = SimpleNamespace(
        model_revision="c" * 64,
        model_input=SimpleNamespace(
            media_types=["image/png", "image/webp"],
            max_aggregate_bytes=900,
            max_parts=2,
        ),
    )
    assert capability_from_assembly(answer) == MediaCapability(
        "c" * 64, (("image/png", 900), ("image/webp", 900)), 2
    )
    # A null model_input, or a client that predates the fields, is unknown.
    assert capability_from_assembly(SimpleNamespace(model_input=None)) is None
    assert capability_from_assembly(SimpleNamespace()) is None


class _Store:
    """A media store that scopes bytes to their owner and expires them."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, bytes, Any]] = {}

    async def put(self, owner: str, media_type: str, data: bytes, now: Any) -> Any:
        import hashlib
        from datetime import timedelta

        from arbora.api.domain.a2a.media_refs import MediaReceipt

        digest = hashlib.sha256(data).hexdigest()
        expires = now + timedelta(seconds=3_600)
        self.rows[(owner, digest)] = (media_type, data, expires)
        return MediaReceipt(f"sha256:{digest}", media_type, len(data), expires)

    async def get(self, owner: str, digest: str, now: Any) -> Any:
        from arbora.api.domain.a2a.media_refs import MediaRefRefused, StoredMedia

        row = self.rows.get((owner, digest))
        if row is None:
            raise MediaRefRefused("media_ref_unknown")
        if now >= row[2]:
            raise MediaRefRefused("media_ref_expired")
        return StoredMedia(row[0], row[1])


def _ref_part(digest: str, mime: str = "image/png") -> dict[str, Any]:
    return {"kind": "file", "file": {"uri": f"media:sha256:{digest}", "mimeType": mime}}


def _upload(store: _Store, owner: str, data: bytes, now: Any) -> str:
    import asyncio
    import base64
    from types import SimpleNamespace

    from arbora.api.ops import a2a_media

    context = SimpleNamespace(
        services={"media_ref_store": store},
        caller=SimpleNamespace(
            tenant=owner.split(":")[0], principal=owner.split(":")[1]
        ),
    )
    params = {"media_type": "image/png", "data": base64.b64encode(data).decode()}
    result = asyncio.run(
        a2a_media.handle_media_upload(
            context, params, a2a_media.operations()[0], clock=lambda: now
        )
    )
    value: dict[str, Any] = result["value"]
    assert set(value) == {"digest", "media_type", "bytes", "expires_at"}
    return str(value["digest"]).removeprefix("sha256:")


@pytest.mark.spec("ARBORA-A2A-R001.5.5")
def test_upload_reference_resolves_for_owner_and_refuses_foreign_expired_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    store = _Store()
    big = PNG + b"\x00" * 2_000
    digest = _upload(store, "tenant:ann", big, datetime.now(UTC))
    monkeypatch.setitem(runtime_config._overrides, "a2a_inline_media_max_bytes", 1_024)
    client, authority = served(CAPABLE, media_refs=store)
    # Over the inline threshold the bytes must travel by reference.
    assert reason(send(client, [file_part(big, "image/png")])) == "file_part_too_large"
    assert send(client, [_ref_part(digest)]).get("error") is None
    assert authority.dispatched[0].media_files()[0][1].decoded_bytes() == big
    # A reference of another caller reads exactly like an unknown one.
    foreign, f_authority = served(CAPABLE, media_refs=store, owner="tenant:bob")
    assert reason(send(foreign, [_ref_part(digest)])) == "media_ref_unknown"
    assert reason(send(client, [_ref_part("0" * 64)])) == "media_ref_unknown"
    old = _upload(
        store, "tenant:ann", PNG + b"\x01" * 2_000, datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert reason(send(client, [_ref_part(old)])) == "media_ref_expired"
    # With no store bound a reference is unavailable, never fetched.
    unbound, _ = served(CAPABLE)
    assert send(unbound, [_ref_part(digest)])["error"]["message"] == "UNAVAILABLE"
    assert f_authority.dispatched == [] and len(authority.dispatched) == 1


@pytest.mark.spec("ARBORA-A2A-R001.5.6")
def test_card_media_limits_equal_enforced_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = served()
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 3)
    monkeypatch.setitem(runtime_config._overrides, "a2a_inline_media_max_bytes", 2_048)
    card = client.get(
        "/.well-known/agent-card.json", headers={"Authorization": "Bearer verified"}
    ).json()["media"]
    limits = MediaLimits.from_config()
    assert card == {
        "allowedTypes": sorted(
            ["image/png", "image/jpeg", "image/webp", "text/plain", "text/markdown"]
        ),
        "maxFilePartBytes": limits.max_file_part_bytes,
        "maxRequestMediaBytes": limits.max_request_media_bytes,
        "maxImageParts": 3,
        "inlineMaxBytes": 2_048,
    }
    # The published image count is the enforced one.
    parts = [file_part(PNG, "image/png")] * 4
    assert reason(send(client, parts)) == "image_part_count"


@pytest.mark.spec("ARBORA-A2A-R001.5.7")
def test_run_request_carries_attachments_capability_and_fallback_mode() -> None:
    from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest

    client, authority = served(CAPABLE)
    assert send(client, [file_part(PNG, "image/png")]).get("error") is None
    media = authority.media[0]
    request = AgentExecutionRequest(agent_name="meristem-expert", task="look", **media)
    assert [a.data for a in request.attachments] == [PNG]
    assert request.input_media_types == ("image/png", "image/jpeg")
    assert request.media_fallback == "refuse"
    # A text-only send carries no media at all.
    text_client, text_authority = served()
    send(text_client, [file_part(b"hello", "text/plain")])
    assert text_authority.media == [None]


@pytest.mark.spec("ARBORA-A2A-R001.5.8")
def test_png_message_send_reaches_run_request_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    store = _Store()
    client, authority = served(CAPABLE, media_refs=store)
    assert send(client, [file_part(PNG, "image/png")]).get("error") is None
    assert [a.data for a in _request(authority.media[0]).attachments] == [PNG]
    # One upload-reference journey.
    big = PNG + b"\x00" * 2_000
    monkeypatch.setitem(runtime_config._overrides, "a2a_inline_media_max_bytes", 1_024)
    digest = _upload(store, "tenant:ann", big, datetime.now(UTC))
    assert send(client, [_ref_part(digest)]).get("error") is None
    assert [a.data for a in _request(authority.media[1]).attachments] == [big]
    # One refusal per reason of R001.5.3 to R001.5.5, none reaching a run.
    count = len(authority.dispatched)
    refused = {
        "file_part_type_mismatch": [file_part(JPEG, "image/png")],
        "file_part_type_unsupported": [file_part(PNG, "image/gif")],
        "file_part_too_large": [file_part(big, "image/png")],
        "media_ref_unknown": [_ref_part("1" * 64)],
    }
    for expected, parts in refused.items():
        assert reason(send(client, parts)) == expected
    assert len(authority.dispatched) == count


def _request(media: dict[str, Any]) -> Any:
    from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest

    return AgentExecutionRequest(agent_name="meristem-expert", task="look", **media)
