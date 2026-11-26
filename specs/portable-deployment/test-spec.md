# Test contract

## Required on every PR from a clean checkout

Run `uv sync --extra test`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora tests`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, and `uv build --wheel --out-dir dist`. CI installs the wheel in a new environment and invokes `arbora-release-canary --json` with a packaged engine wheel. Tests use local temporary paths, fake secret resolvers and deterministic network stubs. The gate must not require an operator's private environment, unprovided token or sibling source tree.

| Positive case | Expected result |
|---|---|
| Load all public sample profiles | Closed schema validates; normalized digest stable across runs. |
| Render development/Compose/Kubernetes plans | All required mounts, identity policy, secret references and digest-pinned images are visible. |
| Run preflight for core and optional WebUI | Core prerequisites are separate; WebUI toolchain checked only when selected. |
| Build and install wheel | Console entrypoints import from installed packages and canary reports aggregate pass. |
| Run disposable first boot in CI | Readiness, authorized request, denied request, idempotent effect and rollback receipts persist. |

| Negative case | Required failure |
|---|---|
| Unknown/missing profile key or missing secret reference | Parse error with source section and key; no apply. |
| Plaintext secret, unpinned production image, unwritable runtime path | Validation refusal before process launch. |
| Wrong engine contract or missing optional WebUI | Typed mismatch/unavailable, without a fallback graph or core host failure. |
| Non-loopback disabled identity or missing first-admin transition | Security refusal with redacted diagnostic. |
| Failing readiness or functional probe | Rollout remains unaccepted and rollback plan is available. |
| Missing/miswired pre-push hook | CI hook audit fails instead of reporting empty green. |

Repository scanner checks include CCCC, jscpd, dupehound and KISS alongside Python gates when source changes; actual versions and differential rules are those pinned by this repository's shared pipeline. A failing required source gate must be fixed or narrowly reclassified with a reproducible portable substitute, never silently skipped. Hosted cluster checks carry an explicit optional/runtime label and do not gate a cloud PR unless the CI job provisions that environment itself.

## ARBORA-DEPLOY-R016 serving container is the in-pod host

| Check | Expected result |
|---|---|
| Manifest contract test | No `daemon` container and no `arbora-daemon` command in any placement. |
| Role | `KG_DAEMON_ROLE=host` in child and sidecar placement; `client` for an out-of-process shared engine. |
| Engine sharing | In sidecar placement `GRAPH_SERVICE_ENDPOINTS` names the shared Unix socket, so no second engine starts. |

## ARBORA-DEPLOY-R017 engine-routed deployment placement and assembly

| Check | Expected result |
|---|---|
| Placement Decide | One typed request; engine decision returned verbatim; arbora names no backend. |
| Assembly, view, confirm | Assembled graph shown read-only and executed only after `arbora.plan/confirm`. |
| Refusal | Stubbed assemble or confirm yields a distinct typed refusal with no side effect. |
| Existing artifacts | compose, helm, swarm resolve to files under `deploy/`, digest-pinned; systemd refused as unsupported. |
| In-process | Live start calls `start_composed_services`. |
| Command | `setup-config deploy` prints the engine's decision and graph and imports no planning code. |
| ARBORA-DEPLOY-R018.1 | tests/deployment/test_engine_endpoint.py | Engine endpoint type. |
| ARBORA-DEPLOY-R018.2 | tests/deployment/test_engine_probe.py | Probe before start. |
| ARBORA-DEPLOY-R018.3 | tests/deployment/test_engine_owner.py | Start only when absent. |
| ARBORA-DEPLOY-R018.4 | tests/deployment/test_engine_attach.py | Second process attaches. |
| ARBORA-DEPLOY-R018.5 | tests/deployment/test_engine_standdown.py | Owner stands down the engine. |
| ARBORA-DEPLOY-R018.6 | tests/deployment/test_remote_engine.py | Remote engine skips local start. |

## Confirmed deployment and guidance acceptance

Use seed 4110 for 32 mixed inventories; derive target cases from every unique matrix triple.
Require explicit target, runtime and privilege fields, with no default or wildcard cell.
Compare actual cases with the declared triple set; omitted, duplicate and unknown cells fail coverage.
Independently require standalone and compose cases for both runtime families crossed with both rootful and rootless modes.
Delete each managed-runtime privilege cell in turn; both matrix validation and acceptance coverage must detect the omission.
For each cell, require a real served success and a no-effect refusal; unavailable adapters retain their missing positive evidence.
Test both native installation selections without replacing target coverage with a fixed count.
Enter `setup-config deploy`, present actual engine output, confirm exact bindings and observe admitted effects at controlled services.
For reuse, record zero create, update, delete, install and restart calls; use independent service identity and liveness probes.
Missing choices, changed digests, unavailable authority and unsupported rootless profiles refuse before the first effect.
An unreachable reused engine starts zero replacement engines. Two host processes share the chosen engine across restart.
Stop both hosts and prove the reused endpoint remains available; retain every original owned-engine lifecycle test.

For each supported target, exercise real startup and original first-boot acceptance with an isolated filesystem and target runtime.
Preserve administrator, identity, secrets, policy, readiness and SDK-federation checks; successful artifact rendering is insufficient.
Parse deployment examples extracted from all three documentation surfaces and run their declared command fixtures.
Probe both MCP transports and check unsupported flags, undeclared variables and stale profile bindings fail conformance.
Missing deployment producers or target runtimes leave their scenarios unexecuted; no helper result provides that evidence.

## Additional owner requirements

- `ARBORA-DEPLOY-R009.2`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-DEPLOY-R009.3`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-DEPLOY-R013.7`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-DEPLOY-R010.4`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

## Medulla companion rows (2026-10-28)

- `ARBORA-DEPLOY-R017.17`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.

## Row proof index

Each row below has no spec marker on a test yet. The listed test is the proof to bind: an existing one is bound by a `pytest.mark.spec` marker, a planned one is written with the code the row adds.

| Row | Test | Observation |
|---|---|---|
| ARBORA-DEPLOY-R001 | `arbora/deployment/artifacts.py::verify_digests`; `tests/deployment/test_helm_image_digests.py::test_default_render_fails_and_pinned_render_passes` | Rendered workload images are digest-pinned. |
| ARBORA-DEPLOY-R007.4 | `arbora/deployment/venv_sync.py::main` (existing, not yet bound by a spec marker); `tests/deployment/test_entrypoints.py::test_host_deployment_scripts_point_to_arbora_modules` (existing, not yet bound by a spec marker) | `arbora-venv` console-script collision resolved. |
| ARBORA-DEPLOY-R007.5 | `arbora/deployment/release_canary.py::main` (existing, not yet bound by a spec marker); `tests/deployment/test_release_canary.py::test_entry_points_ready_requires_exact_direct_targets` (existing, not yet bound by a spec marker); `tests/deployment/test_entrypoints.py::test_release_canary_checks_the_same_direct_entrypoints` (existing, not yet bound by a spec marker) | `arbora-release-canary` is declared only by arbora. |
| ARBORA-DEPLOY-R008 | `arbora/architecture/boundary_census.py::main`; `tests/architecture/test_boundary_census.py::test_census_refuses_an_import_outside_the_public_surface`; `tests/deployment/test_boundary_module_layout.py::test_each_named_module_exists_and_is_imported_locally` | Arbora hosts its own boundary modules. |
| ARBORA-DEPLOY-R011 | `tests/deployment/test_pr_static_scope.py::test_fast_subset_includes_every_deployment_test` | Deployment test suite passes in full. |
| ARBORA-DEPLOY-R012 | `tests/deployment/test_reference_pins.py::test_pinned_commit_fails_and_main_passes` (planned) | Pipelines dependency is referenced at main, never pinned. |
| ARBORA-DEPLOY-R014 | `tests/architecture/test_dependency_manifest_pins_published_packages.py::test_manifest_declares_each_split_repository_package` (existing, not yet bound by a spec marker) | Split packages are consumed by published version range. |
| ARBORA-DEPLOY-R015 | `tests/deployment/test_doctor_lakehouse.py::test_cpu_has_avx2_detects_flag_from_synthetic_cpuinfo_text` (existing, not yet bound by a spec marker) | AVX2 guard test runs on any hosted runner. |
| ARBORA-DEPLOY-R017.1 | `arbora/api/ops/deploy.py::handle_placement_plan` (planned); `arbora/deployment/placement.py::ask_placement` (planned); `tests/deployment/test_placement_decide.py::test_request_fields_record_passthrough_and_no_backend_names` (planned); `tests/api/test_deploy_ops_served.py::test_plan_returns_the_engine_placement_record` (planned) | Placement Decide mapping. |
| ARBORA-DEPLOY-R017.2 | `arbora/deployment/assembly.py::assemble_deployment` (planned); `tests/deployment/test_deploy_assembly_request.py::test_task_and_capabilities_sent_graph_returned_as_is` (planned); `tests/api/test_deploy_ops_served.py::test_plan_returns_assembled_graph_from_published_components` (planned) | Request assembly for a deployment task. |
| ARBORA-DEPLOY-R017.3 | `arbora/deployment/assembly_view.py::render_graph` (planned); `tests/deployment/test_deploy_assembly_view.py::test_nodes_listed_in_engine_order_and_no_dispatch_path` (planned) | Present the assembled graph read-only. |
| ARBORA-DEPLOY-R017.4 | `arbora/deployment/assembly.py::require_confirmed_plan` (planned); `tests/deployment/test_deploy_confirm.py::test_apply_runs_only_after_consumed_plan` (planned) | Confirm through plan confirmation. |
| ARBORA-DEPLOY-R017.5 | `arbora/deployment/assembly.py::refuse_unavailable` (planned); `tests/deployment/test_deploy_refusal.py::test_three_unavailable_causes_give_distinct_refusals_with_no_effect` (planned); `tests/api/test_deploy_ops_served.py::test_feature_absent_engine_refuses_over_http` (planned) | Typed refusal while a producer is unavailable. |
| ARBORA-DEPLOY-R017.6 | `arbora/deployment/artifacts.py::select_artifact`; `tests/deployment/test_deploy_artifacts.py::test_each_target_resolves_to_an_existing_file_and_systemd_is_refused` | Select the existing artifact per target. |
| ARBORA-DEPLOY-R017.7 | `arbora/mcp_server/composition.py::start_composed_services` (existing, not yet bound by a spec marker); `tests/deployment/test_deployment_in_process.py::test_live_start_calls_start_composed_services_with_detected_composition` | In-process start stays unchanged. |
| ARBORA-DEPLOY-R017.8 | `arbora/deployment/cli.py::_run_deploy`; `tests/deployment/test_deploy_cli.py::test_output_equals_the_engine_decision_and_graph` | Thin deploy subcommand. |
| ARBORA-DEPLOY-R017.9 | `arbora/deployment/image_digests.py::_check_resource`; `tests/deployment/test_image_digests.py::test_compose_document_is_walked_and_kubernetes_cases_still_pass` | Checker dispatches on document shape. |
| ARBORA-DEPLOY-R017.10 | `tests/deployment/test_image_digests.py::test_pinned_literal_accepted_tagged_and_missing_rejected_with_location` | Compose and swarm literal images use the existing grammar. |
| ARBORA-DEPLOY-R017.11 | `tests/deployment/test_image_digests.py::test_required_variable_form_accepted_default_and_bare_forms_validated` | Template mode accepts the required-variable form. |
| ARBORA-DEPLOY-R017.12 | `tests/deployment/test_image_digests.py::test_set_digest_passes_set_tag_fails_unused_name_errors` | Resolve mode validates the given value. |
| ARBORA-DEPLOY-R017.13 | `tests/deployment/test_deploy_artifacts.py::test_shipped_compose_and_swarm_files_pass_template_and_resolve_modes` | Run the checker on the shipped Compose and swarm files. |
| ARBORA-DEPLOY-R017.14 | `tests/deployment/test_helm_image_digests.py::test_pinned_render_passes_and_default_render_fails_on_replace_me` | Helm pinned and default renders. |
| ARBORA-DEPLOY-R017.15 | `tests/deployment/test_deploy_artifacts.py::test_systemd_refused_and_others_resolve` | systemd stays refused. |
| ARBORA-DEPLOY-R017.16 | `arbora/deployment/artifacts.py::digest_check_decision`; `tests/deployment/test_deploy_artifacts.py::test_prod_and_test_require_the_check_dev_reports_skipped_by_policy` | Environment policy drives the digest check. |
| ARBORA-DEPLOY-R010.1 | `tests/skills/test_development_skill_freshness.py::test_required_sections_are_present` (planned) | Development skill keeps every original topic. |
| ARBORA-DEPLOY-R010.2 | `tests/skills/test_deployment_examples.py::test_examples_agree_parse_and_probe_on_both_transports` (planned) | Deployment examples stay executable and synchronized. |
| ARBORA-DEPLOY-R010.3 | `tests/skills/test_deployment_examples.py::test_every_supported_profile_has_all_parts` (planned) | Supported-profile instructions are complete. |
| ARBORA-DEPLOY-R013.1 | `tests/deployment/test_kubernetes_first_boot.py::test_bad_cluster_observations_fail_closed` (existing, not yet bound by a spec marker); `tests/deployment/test_genesis_environments.py::test_default_profile_loads_and_validates` (existing, not yet bound by a spec marker) | Profile and first-boot obligations are retained. |
| ARBORA-DEPLOY-R013.2 | `tests/deployment/test_component_choices.py::test_all_new_all_reused_and_mixed_inventories_match_the_presented_choices` (planned) | Record explicit deploy or reuse choice per component. |
| ARBORA-DEPLOY-R013.3 | `tests/deployment/test_run_confirmed_steps.py::test_injected_update_for_reused_service_makes_zero_calls` (planned) | Reused components are connect-only. |
| ARBORA-DEPLOY-R013.4 | `tests/deployment/test_target_matrix.py::test_every_declared_triple_has_a_refusal_case_and_a_success_case_id` | Deployment target matrix is complete. |
| ARBORA-DEPLOY-R013.5 | `tests/api/test_deploy_ops_served.py::test_mixed_deployment_changes_only_deploy_marked_components` (planned) | Confirmed profiles bind to governed deployment. |
| ARBORA-DEPLOY-R013.6 | `tests/deployment/test_component_choices_served.py::test_seed_4110_inventories_and_matrix_cells` (planned) | Component choices and target profiles are proven served. |
| ARBORA-DEPLOY-R018.7 | `tests/deployment/test_engine_reuse.py::test_two_hosts_reuse_one_engine_and_unavailable_reuse_starts_zero` | A reused engine only attaches. |
| ARBORA-DEPLOY-R018.8 | `tests/deployment/test_engine_reuse.py::test_reused_engine_stays_reachable_after_both_clients_stop` | A reused engine survives host shutdown. |
| ARBORA-DEPLOY-R009.2.2 | `tests/deployment/test_single_image_parity_served.py::test_every_matrix_row_matches_the_single_image` (planned) | Single image serves a capability and refusal parity matrix. |
| ARBORA-DEPLOY-R600.1 | `tests/deployment/test_process_locks.py::test_exclusive_lock_contends_then_frees` | Lock model. |
| ARBORA-DEPLOY-R600.2 | `tests/deployment/test_process_locks.py::test_engine_guard_is_per_socket` | Spawn guard behavior. |
| ARBORA-DEPLOY-R600.3 | `tests/deployment/test_engine_lifecycle_served.py::test_unset_endpoints_start_one_loopback_engine_and_serve_a_call`; `tests/architecture/test_no_orphan_modules.py::test_no_host_lock_module` | Lock wiring. |
| ARBORA-DEPLOY-R600.4 | `tests/deployment/test_process_locks.py::test_busy_guard_refuses_without_spawning` | Lock refusal. |
| ARBORA-DEPLOY-R600.5 | `tests/deployment/test_process_locks.py::test_lock_file_holds_pid_and_opaque_digest_only` | Lock evidence. |
| ARBORA-DEPLOY-R600.6 | `tests/deployment/test_process_locks.py::test_engine_guard_acquire_time_budget` | Lock hot-path proof. |
| ARBORA-DEPLOY-R017.18 | `arbora/deployment/artifacts.py::verify_digests`; `arbora/deployment/image_digests.py::check_file`; `tests/deployment/test_image_digests.py::test_module_is_the_one_checker_and_script_is_removed` | Digest checker lives in the package. |
| ARBORA-DEPLOY-R017.19 | `tests/api/test_deploy_ops_served.py::test_plan_then_confirmed_apply_runs_once` (planned) | Serve the placement and apply operations. |
| ARBORA-DEPLOY-R017.20 | `arbora/deployment/assembly_library.py::publish_deployment_library` (planned); `tests/deployment/test_assembly_library.py::test_startup_publishes_templates_and_components_once_per_tenant` (planned) | Publish the deployment template and its tool components. |
| ARBORA-DEPLOY-R017.21 | `arbora/deployment/assembly.py::run_confirmed_steps` (planned); `tests/deployment/test_run_confirmed_steps.py::test_nodes_run_in_engine_order_once_per_key` (planned) | Run the confirmed step graph through governed fleet calls. |
| ARBORA-DEPLOY-R018.9 | `tests/deployment/test_engine_lifecycle_served.py::test_unset_endpoints_start_one_loopback_engine_and_serve_a_call` | Engine pool wires the local lifecycle. |
| ARBORA-DEPLOY-R601 | `tests/deployment/test_deploy_templates.py::test_templates_published_and_assemble_solved` (planned) | Publish deployment templates and tool components. |
| ARBORA-DEPLOY-R602 | `tests/deployment/test_deploy_commands.py::test_each_script_function_is_a_command_with_a_config_input` (planned) | The deployment configuration repository holds configuration only. |
| ARBORA-DEPLOY-R021 | `tests/deployment/test_registry_delivery.py::test_deploy_delivers_workspace_manifest_and_fails_on_unreadable_registry` (planned) | Every deploy delivers the current registry manifest and proves it readable. |
| ARBORA-DEPLOY-R603 | `tests/deployment/test_identity_reconcile.py::test_reconcile_is_idempotent_and_reports_drift` (planned) | Identity and application SSO provisioning is one declarative reconcile. |
| ARBORA-DEPLOY-R602.1 | `tests/deployment/test_render_command.py::test_same_topology_renders_byte_identical_manifests`, `tests/deployment/test_render_command.py::test_invalid_topology_exits_two_with_the_field_path` and `tests/deployment/test_render_command.py::test_module_holds_no_host_literal` (L2). | Manifest renderer becomes `arbora deploy render`. |
| ARBORA-DEPLOY-R602.2 | `tests/deployment/test_provision_commands.py::test_each_subcommand_reads_only_the_deploy_file`, `tests/deployment/test_provision_commands.py::test_provision_never_rewrites_an_existing_secret` and `tests/deployment/test_provision_commands.py::test_enrol_adds_to_the_current_role_set` (L2). | Provisioning helpers become `arbora deploy provision`. |
| ARBORA-DEPLOY-R602.3 | `tests/deployment/test_acceptance_command.py::test_runner_reads_every_input_from_the_config_file`, `tests/deployment/test_acceptance_command.py::test_scenario_interpreter_runs_a_dag_and_reports_a_failed_expectation` and `tests/deployment/test_acceptance_command.py::test_no_scenario_name_is_in_the_package` (L2). | Acceptance runner becomes `arbora acceptance run` with declarative scenarios. (planned: the named test file does not exist yet) |
| ARBORA-DEPLOY-R602.4 | `tests/security/test_secret_diagnostics.py::test_no_api_accepts_a_secret_value` and `tests/security/test_secret_diagnostics.py::test_environment_membership_only` (L2), moved from the deployment repository. | Secret diagnostics becomes `arbora secrets diagnose`. |
| ARBORA-DEPLOY-R602.5 | `tests/deployment/test_deploy_run_machine.py::test_each_legal_edge_has_a_test`, `tests/deployment/test_deploy_run_machine.py::test_every_illegal_pair_is_refused`, `tests/deployment/test_deploy_run_machine.py::test_resume_skips_completed_steps` and `tests/deployment/test_deploy_run_machine.py::test_rollback_applies_the_previous_digest` (L2). | The deploy script becomes `arbora deploy run`, an explicit state machine. |
| ARBORA-DEPLOY-R602.6 | `pipelines-hook environment-config-only` exits 0 on the repository (L2), and `tests/deployment/test_deploy_commands.py::test_each_script_function_is_a_command_with_a_config_input` (L2) lists every removed function with its command. | The deployment repository drops its code. |
| ARBORA-DEPLOY-R019 | `tests/deployment/test_sandbox_up_down.py::test_up_serves_the_real_stack_and_down_removes_it` (planned, L5). | One command starts a local no-auth sandbox, and one command stops it (rollup). |
| ARBORA-DEPLOY-R019.1 | `tests/deployment/test_deployment_docs_sandbox.py::test_documented_sandbox_commands_and_variables_match_the_code` (planned, L2). | The local deployment page documents the sandbox. |
| ARBORA-DEPLOY-R019.2 | `tests/deployment/test_sandbox_engine_pin.py::test_sandbox_refuses_an_engine_that_differs_from_the_lock` (planned, L2). | The sandbox engine is the version the lock pins. |
| ARBORA-DEPLOY-R019.3 | `tests/deployment/test_sandbox_provisioning_limit.py::test_repeat_up_on_a_fresh_store_is_not_rate_limited` (planned, L3). | The provisioning limit is stated, and a fresh store resets it. |
| ARBORA-DEPLOY-R019.4 | `tests/deployment/test_refusal_detail.py::test_refusal_prints_code_and_message_and_redacts_secrets` (L2). | A refusal shows its error code and message. |
| ARBORA-DEPLOY-R020 | `tests/deployment/test_provisioning_admitted_by_pinned_engine.py::test_fresh_store_accepts_all_arbora_content` (L5). | Content that Arbora provisions passes the pinned engine's admission (rollup). |
| ARBORA-DEPLOY-R020.1 | `tests/deployment/test_pack_class_rollup.py::test_every_pack_class_has_a_core_subclass_path` (L2). | Every pack class extends an engine core class. |
| ARBORA-DEPLOY-R020.2 | `tests/deployment/test_prompt_blueprints_valid.py::test_every_prompt_passes_the_pinned_engine_blueprint` (L2). | Every prompt passes the engine blueprint validation. |
| ARBORA-DEPLOY-R020.3 | `tests/deployment/test_provisioning_admitted_by_pinned_engine.py::test_fresh_store_accepts_all_arbora_content` (L5). | CI proves provisioning into a fresh store on the pinned engine. |
| ARBORA-DEPLOY-R017.22 | `tests/api/test_promote_ops.py::test_plan_requires_digest_and_green_pipeline` (L3). | Promote a built artifact through a governed operation. |
| ARBORA-DEPLOY-R017.23 | `tests/api/test_promote_ops.py::test_apply_needs_approval_records_deploy_and_is_idempotent` (L3). | Promotion applies only after approval and records a deploy record. |
| ARBORA-DEPLOY-R017.24 | `tests/api/test_promote_served.py::test_agent_promotes_after_approval_and_refusals_run_nothing` (L3). | Hot-path proof: an agent runs CI to deploy through the door. |
