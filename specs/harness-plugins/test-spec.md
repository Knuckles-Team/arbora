# ARBORA-HARNESS-PLUGINS — Test specification

Governing spec: [spec.md](spec.md).

| Test ID | Requirement | Test | Expected observation |
|---|---|---|---|
| T-001 | `ARBORA-HARNESS-PLUGINS-R001.1` | `tests/harness_plugins/test_profiles.py::test_profile_table_is_closed_and_complete` | The test observes the rule of the row: model: the closed harness profile table. |
| T-002 | `ARBORA-HARNESS-PLUGINS-R001.2` | `tests/harness_plugins/test_compose.py::test_bundle_holds_door_skills_agents_and_no_prompt_copy` | The test observes the rule of the row: core composer reads the registry and the content packages. |
| T-003 | `ARBORA-HARNESS-PLUGINS-R001.3` | `tests/harness_plugins/test_render.py::test_two_renders_give_one_digest` | The test observes the rule of the row: renderers write one deterministic tree per profile. |
| T-004 | `ARBORA-HARNESS-PLUGINS-R001.4` | `tests/harness_plugins/test_cli.py::test_bundle_print_path_prints_one_absolute_line` | The test observes the rule of the row: command: arbora plugin bundle. |
| T-005 | `ARBORA-HARNESS-PLUGINS-R001.5` | `tests/harness_plugins/test_cli.py::test_refusals_exit_two_and_write_nothing` | The test observes the rule of the row: refusal: typed reasons write no file. |
| T-006 | `ARBORA-HARNESS-PLUGINS-R001.6` | `tests/harness_plugins/test_render.py::test_bundle_holds_no_secret_and_no_escaping_path` | The test observes the rule of the row: no secret and no file outside the tree. |
| T-007 | `ARBORA-HARNESS-PLUGINS-R005.1` | `tests/harness_plugins/test_render_claude_code.py::test_layout_matches_the_profile` | The test observes the rule of the row: layout of the claude-code tree. |
| T-008 | `ARBORA-HARNESS-PLUGINS-R005.2` | `tests/harness_plugins/test_render_claude_code.py::test_hooks_are_mcp_tool_calls_to_the_door` | The test observes the rule of the row: hooks call the door as MCP tools. |
| T-009 | `ARBORA-HARNESS-PLUGINS-R005.3` | `tests/harness_plugins/test_render_claude_code.py::test_marketplace_entry_uses_the_bundle_command` | The test observes the rule of the row: marketplace entry with a command source. |
| T-010 | `ARBORA-HARNESS-PLUGINS-R005.4` | `tests/harness_plugins/test_render_claude_code.py::test_statusline_prints_one_line_quickly` | The test observes the rule of the row: status line command. |
| T-011 | `ARBORA-HARNESS-PLUGINS-R005` | Every child row. | See each child. |
| T-012 | `ARBORA-HARNESS-PLUGINS-R006.1` | `tests/harness_plugins/test_render_opencode.py::test_layout_matches_the_profile` | The test observes the rule of the row: layout: one generated server plugin directory. |
| T-013 | `ARBORA-HARNESS-PLUGINS-R006.2` | `tests/harness_plugins/test_render_opencode.py::test_setup_registers_door_skills_and_commands` | The test observes the rule of the row: setup registers the door, the skills and the commands. |
| T-014 | `ARBORA-HARNESS-PLUGINS-R006.3` | `tests/harness_plugins/test_render_opencode.py::test_hooks_call_workstream_operations_non_blocking` | The test observes the rule of the row: session hooks call the workstream operations. |
| T-015 | `ARBORA-HARNESS-PLUGINS-R006.4` | `tests/harness_plugins/test_render_opencode.py::test_loader_fixture_runs_the_generated_plugin` | The test observes the rule of the row: hot-path proof: a loader fixture runs the generated plugin. |
| T-016 | `ARBORA-HARNESS-PLUGINS-R006` | Every child row. | See each child. |
| T-017 | `ARBORA-HARNESS-PLUGINS-R001.7` | `tests/harness_plugins/test_bundle_served.py::test_console_builds_every_profile_and_each_validates` | The test observes the rule of the row: hot-path proof: the console builds every profile. |
| T-018 | `ARBORA-HARNESS-PLUGINS-R001` | Every child row. | See each child. |
| T-019 | `ARBORA-HARNESS-PLUGINS-R002.1` | `tests/harness_plugins/test_install_machine.py::test_edges_match_the_ontology_and_refuse_others` | The test observes the rule of the row: model: the install machine. |
| T-020 | `ARBORA-HARNESS-PLUGINS-R002.2` | `tests/harness_plugins/test_install_machine.py::test_install_status_remove_touch_only_owned_files` | The test observes the rule of the row: commands: install, status and remove. |
| T-021 | `ARBORA-HARNESS-PLUGINS-R002.3` | `tests/harness_plugins/test_install_machine.py::test_duplicate_door_refuses_and_edits_nothing` | The test observes the rule of the row: hard cutover: one door per harness. |
| T-022 | `ARBORA-HARNESS-PLUGINS-R002.4` | `tests/harness_plugins/test_install_machine.py::test_changed_registry_gives_stale` | The test observes the rule of the row: stale detection and the update channel. |
| T-023 | `ARBORA-HARNESS-PLUGINS-R002.5` | `tests/harness_plugins/test_bundle_served.py::test_install_change_status_remove` | The test observes the rule of the row: hot-path proof: install, change and status. |
| T-024 | `ARBORA-HARNESS-PLUGINS-R002` | Every child row. | See each child. |
| T-025 | `ARBORA-HARNESS-PLUGINS-R003.1` | `tests/harness_plugins/test_compose.py::test_mcp_entry_names_native_client_and_profile_callback` | The test observes the rule of the row: the bundle MCP entry names the native client and the profile callback. |
| T-026 | `ARBORA-HARNESS-PLUGINS-R003.2` | `tests/harness_plugins/test_login.py::test_login_stores_refresh_token_and_renews` | The test observes the rule of the row: command: arbora plugin login for command hooks. |
| T-027 | `ARBORA-HARNESS-PLUGINS-R003.3` | `tests/harness_plugins/test_login.py::test_credential_edges_match_the_ontology` | The test observes the rule of the row: model: the credential machine. |
| T-028 | `ARBORA-HARNESS-PLUGINS-R003.4` | `tests/harness_plugins/test_hook_runner.py::test_missing_credential_is_a_non_blocking_note` | The test observes the rule of the row: refusal: a hook without a credential never blocks. |
| T-029 | `ARBORA-HARNESS-PLUGINS-R003` | Every child row. | See each child. |
| T-030 | `ARBORA-HARNESS-PLUGINS-R004.1` | `tests/api/ops/test_workstreams.py::test_key_holds_digest_and_never_the_path` | The test observes the rule of the row: model: workstream params and results. (planned) |
| T-031 | `ARBORA-HARNESS-PLUGINS-R004.2` | `tests/api/ops/test_workstreams.py::test_attach_is_idempotent_and_bounded` | The test observes the rule of the row: the attach operation joins a harness session to the workstream. (planned) |
| T-032 | `ARBORA-HARNESS-PLUGINS-R004.3` | `tests/api/ops/test_workstreams.py::test_checkpoint_writes_one_masked_turn` | The test observes the rule of the row: the checkpoint operation records a handoff. (planned) |
| T-033 | `ARBORA-HARNESS-PLUGINS-R004.4` | `tests/api/ops/test_workstreams.py::test_cross_principal_read_matches_unknown` | The test observes the rule of the row: detach, get and list, with a uniform denial. (planned) |
| T-034 | `ARBORA-HARNESS-PLUGINS-R004.5` | `tests/api/ops/test_workstreams.py::test_attachment_edges_match_the_ontology` | The test observes the rule of the row: model: the attachment machine. (planned) |
| T-035 | `ARBORA-HARNESS-PLUGINS-R004.6` | `tests/api/ops/test_workstreams.py::test_operations_and_scopes_are_registered` | The test observes the rule of the row: registration and scopes. (planned) |
| T-036 | `ARBORA-HARNESS-PLUGINS-R004.7` | `tests/api/ops/test_workstreams_served.py::test_two_harnesses_share_one_workstream` | The test observes the rule of the row: hot-path proof: two harnesses share one workstream over MCP. (planned) |
| T-037 | `ARBORA-HARNESS-PLUGINS-R004` | Every child row. | See each child. |
| T-038 | `ARBORA-HARNESS-PLUGINS-R007` | `tests/harness_plugins/test_hook_runner.py::test_runner_maps_events_and_always_exits_zero` | The test observes the rule of the row: command hook runner. |
| T-039 | `ARBORA-HARNESS-PLUGINS-R008` | `tests/harness_plugins/test_render_codex.py::test_layout_hooks_and_mcp_entry` | The test observes the rule of the row: the codex renderer. |
| T-040 | `ARBORA-HARNESS-PLUGINS-R009` | `tests/harness_plugins/test_render_grok.py::test_layout_reuses_the_shared_parts` | The test observes the rule of the row: the grok renderer. |
| T-041 | `ARBORA-HARNESS-PLUGINS-R010` | Every child row. | See each child. |
| T-042 | `ARBORA-HARNESS-PLUGINS-R010.1` | `tests/harness_plugins/test_render_devin.py::test_connection_sheet_and_playbook_only` | The tree holds only the connection sheet and the playbook; install refuses `HOSTED_HARNESS`. (planned) |
| T-043 | `ARBORA-HARNESS-PLUGINS-R010.2` | `tests/identity/test_client_contract.py::test_hosted_client_has_vendor_redirect_and_no_loopback` | The hosted client carries the vendor redirect and the workstream scopes, and no loopback URI. (planned) |
| T-044 | `ARBORA-HARNESS-PLUGINS-R010.3` | `tests/harness_plugins/test_render_devin.py::test_playbook_attaches_and_checkpoints` | The playbook names attach at start and checkpoint before finish, with the repository workspace key. (planned) |

## Quality and release proof

Focused: `uv run pytest tests/harness_plugins tests/api/ops/test_workstreams.py tests/api/ops/test_workstreams_served.py -q`. The release proof of `PIPE-RELEASE-R028` runs each harness validator on a generated bundle. Gates: the repository complexity, duplication, dead-code and KISS gates and the spec gates.
