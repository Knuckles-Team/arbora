# Messaging channel supervision lifecycle — test contract

Governing [spec](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| MSG-T01 | ARBORA-MESSAGING-R001.1 | For every pair `(current, target)` in the legal-transition table, `transition(current, target)` returns `target`. |
| MSG-T02 | ARBORA-MESSAGING-R001.1 | For a representative illegal set (`STOPPED -> RUNNING`, `DEGRADED -> RUNNING`, `STOPPED -> BACKING_OFF`, `STOPPING -> RUNNING`), `transition(current, target)` raises `IllegalSupervisionTransition` naming both states. |
| MSG-T03 | ARBORA-MESSAGING-R001 | (future slice) `InboundRouter` reports live per-backend state matching the fixture's observed lifecycle. |
| MSG-T04 | ARBORA-MESSAGING-R002 | (future slice) An unconnected fixture backend never reaches `RUNNING`; a connected one does. |
| MSG-T05 | ARBORA-MESSAGING-R003 | (future slice) Simulated failures produce a capped, doubling delay sequence; a sustained healthy run resets it to base. |
| MSG-T06 | ARBORA-MESSAGING-R004 | (future slice) `stop()` during a backoff wait drains with no further restart. |
| MSG-T07 | ARBORA-MESSAGING-R005.1 | Configure one missing/import-failing adapter and one available backend. The registry exposes the missing channel as DEGRADED with its typed reason, creates the available backend, and the available backend reaches RUNNING. |
| MSG-T08 | ARBORA-MESSAGING-R006 | A static import check over `arbora/messaging/{router,registry,service}.py` confirms none of the three files imports a meristem module. The test file is planned. |
| MSG-T09 | ARBORA-MESSAGING-R005.1 | Disable intake with any credential configuration. Report enabled=false, state=disabled, and alert_eligible=false. Enable intake with no configured backend. Report not_configured and alert_eligible=true. With every configured backend connected, report healthy and alert_eligible=false. |
| MSG-T10 | ARBORA-MESSAGING-R005.1 | Exercise missing adapters, failed imports, and unconnected backends while intake is enabled. Report degraded and mark each affected backend unavailable. Map AdapterMissing to adapter_missing. Map AdapterImportFailed to adapter_import_failed. Map false is_connected to backend_not_connected. Set alert_eligible=true. Keep other channels independently reportable. |
| MSG-T11 | ARBORA-MESSAGING-R005.1 | Evaluate component alerts using alert_eligible only. Credentials with enabled=false never fire. Enabled not-configured or degraded entries are eligible. A health read emits no notification. |
| MSG-T12 | ARBORA-MESSAGING-R005.1 | Call authenticated GET /api/dashboard/health and graph_configure(action="health"). Both surfaces must return the same schema and stable backend order. Keep output free of secrets. Confirm unauthenticated /health and /health/ready expose no component details. |

MSG-T09–MSG-T12 have no results in this specification publication. The cases require implementation and real endpoint/consumer fixtures. This change claims no runtime or test evidence.
Implementation acceptance covers uv run pytest tests/messaging and authenticated dashboard/configure integration cases. Ruff and mypy cover changed Python modules.

## Memory webhooks

| Test | Requirement | Setup and expected result |
|---|---|---|
| MSG-T13 | ARBORA-MESSAGING-R700.1 | Test: `tests/messaging/test_memory_webhooks.py::test_subscribe_list_unsubscribe_and_refusals` (served: HTTP and MCP). |
| MSG-T14 | ARBORA-MESSAGING-R700.2 | Test: `tests/messaging/test_memory_webhooks.py::test_signature_retry_backoff_and_dead_letter` (module test with an injected transport and clock). |
| MSG-T15 | ARBORA-MESSAGING-R700.3 | Test: `tests/messaging/test_memory_webhooks.py::test_pump_pulls_delivers_and_acks_once` (served: loopback engine and receiver). Test: `::test_crash_before_ack_redelivers_with_the_same_delivery_id`. |
| MSG-T16 | ARBORA-MESSAGING-R700.4 | Test: `tests/messaging/test_memory_webhooks.py::test_turn_to_signed_webhook_journey` (served: HTTP and MCP with a loopback engine and receiver). Test: `::test_other_tenant_events_never_match`. |

## Row coverage

| Row | Test |
|---|---|
| `ARBORA-MESSAGING-R005.1.1` | `tests/api/test_dashboard_health_components.py::test_unconfigured_connector_raises_no_alert` (planned) |
| `ARBORA-MESSAGING-R007.1` | `tests/messaging/test_sender_identity.py::test_mapped_sender_runs_turn_under_principal` (planned) |
| `ARBORA-MESSAGING-R008.1` | `tests/messaging/test_webhook_ingress.py::test_signed_event_reaches_backend_queue_and_bad_signature_refuses` (planned) |
| `ARBORA-MESSAGING-R009.1` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_bluebubbles_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.2` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_buzz_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.3` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_dingtalk_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.4` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_email_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.5` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_feishu_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.6` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_nostr_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.7` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_ntfy_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.8` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_photon_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.9` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_qqbot_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.10` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_raft_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.11` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_reef_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.12` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_simplex_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.13` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_sms_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.14` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_tlon_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.15` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_webhook_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.16` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_wecom_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.17` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_weixin_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.18` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_x_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.19` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_yuanbao_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.20` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_zalo_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R009.21` | `tests/messaging/test_all_backends.py::test_send_reaches_the_faked_platform_api` (written) ; `tests/messaging/test_channel_adapters.py::test_zalouser_inbound_event_reaches_router` (planned) |
| `ARBORA-MESSAGING-R800.3` | `tests/messaging/test_channel_adapters.py::test_connect_names_the_extra_when_the_sdk_is_missing` (written) |
| `ARBORA-MESSAGING-R800.6` | `tests/messaging/test_primitives.py::test_render_inline_forms` (written) ; `tests/messaging/bench_render_dispatch.py::test_ten_thousand_messages` (planned) |
| `ARBORA-MESSAGING-R800.1000` | `tests/messaging/test_lease.py::test_two_contenders_cannot_both_own_intake` (written) ; `tests/messaging/test_lease.py::test_lost_renewal_stops_intake_and_releases_the_lease` (written) |
