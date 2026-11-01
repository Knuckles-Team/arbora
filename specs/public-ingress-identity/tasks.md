# ARBORA-INGRESS — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). Rows: [requirements.md](requirements.md).

Each task line reads `- [ ] T001 [P] [US1] <ID>-R012.4 description`. Tasks run producer first.

- [x] T001 [US1] ARBORA-INGRESS-R001.1 Add the typed `PublicIngressProfile` model with closed-schema validation and refusal tests in `arbora/deployment/public_ingress_identity.py` and `tests/deployment/test_public_ingress_identity.py`.
- [ ] T002 [US1] ARBORA-INGRESS-R001.2 Add `EnvironmentProfile.public_ingress`, parse it in `load_environment_profile`, and emit its values in `profile_summary` and `write_config`.
- [ ] T003 [US2] ARBORA-INGRESS-R001.2 Feed the rendered origin to `runtime_config.webui_origin` and add `test_profile_renders_origin_callback_and_proxy`.
- [ ] T004 [US3] ARBORA-INGRESS-R001.3 Add `legacy_hostname` and `legacy_redirect_until` validation and the bounded 308 route output with `test_legacy_redirect_is_bounded`.
- [ ] T005 [US3] ARBORA-INGRESS-R001.4 Add the cutover probe test `tests/qualification/identity/test_cutover_probe.py` over the probe of `ARBORA-IDENTITY-R024`.
