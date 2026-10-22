"""Real WorkItem/effect journal loopback proof; no transport/storage doubles."""

import asyncio
import copy
import json
import sys
import uuid
from types import SimpleNamespace

from epistemic_graph import EpistemicGraphClient

from arbora.api.invoke.steps import OpResult
from arbora.core.settings import load_config
from arbora.mcp_server.effect_journal import EgEffectJournal, _request_context
from arbora.security.anonymous import session, session_context, validate_startup


async def main():
    load_config()
    validate_startup(SimpleNamespace(host="127.0.0.1", auth_type="none"))
    claims = session_context(session())
    client = await EpistemicGraphClient.connect(
        auth_mode="none", tcp_addr=sys.argv[1], graph_name="tenant__local"
    )

    async def get_client():
        return client

    journal = EgEffectJournal(get_client, claims=lambda: claims)
    try:
        key = "effect-proof-" + uuid.uuid4().hex
        reservation = await journal.reserve(key, "synthetic-effect-binding")
        assert reservation.state == "acquired", reservation
        await journal.complete(reservation.reference, OpResult({"proof": key}))
        replay = await journal.reserve(key, "synthetic-effect-binding")
        assert replay.state == "completed" and replay.outcome.value == {"proof": key}, (
            replay
        )
        conflict = await journal.reserve(key, "different-binding")
        assert conflict.state == "conflict", conflict
        base = {
            "schema_version": "1",
            "context": _request_context(claims, client, key),
            "work_item_id": "provenance-" + uuid.uuid4().hex,
            "idempotency_key": "provenance-" + uuid.uuid4().hex,
            "command_digest": "a" * 64,
            "kind": "arbora.acceptance",
            "priority": 0,
            "depends_on": [],
            "input_ref": "synthetic:provenance",
            "policy_digest": "b" * 64,
            "catalog_digest": "c" * 64,
            "model_digest": "none",
            "max_attempts": 1,
            "deadline_unix": None,
            "metadata": {},
            "provenance_refs": [],
            "max_tenant_in_flight": 4096,
        }
        accepted = await client.work_items.submit(base)
        assert accepted.get("created"), accepted
        negatives = []
        for field, value in [
            ("authentication_method", "oidc"),
            ("authentication_method", "local_process"),
            ("subject_id", "forged-subject"),
            ("tenant_id", "forged-tenant"),
        ]:
            request = copy.deepcopy(base)
            request["work_item_id"] = "negative-" + uuid.uuid4().hex
            request["idempotency_key"] = "negative-" + uuid.uuid4().hex
            request["context"][field] = value
            try:
                await client.work_items.submit(request)
            except Exception as exc:
                assert "context does not match verified request authority" in str(
                    exc
                ), str(exc)
                negatives.append(field + "=" + value)
            else:
                raise AssertionError("provenance forgery accepted: " + field)
        print(
            json.dumps(
                {
                    "effect_reserve_complete_replay_conflict": True,
                    "none_provenance_accepted": True,
                    "authority_refusals": negatives,
                    "effect_key": key,
                }
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
