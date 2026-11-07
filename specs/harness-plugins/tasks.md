# ARBORA-HARNESS-PLUGINS — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). Rows: [requirements.md](requirements.md).

Each task line reads `- [ ] T001 [P] [US1] <ID>-R012.4 description`. Tasks run producer first. The `claude-code` and `opencode` renderers come before `codex` and `grok`. The workstream rows wait for `MEDULLA-IDENTITY-R723`.

- [x] T001 [US1] ARBORA-HARNESS-PLUGINS-R001.1 Model: the closed harness profile table.
- [x] T002 [US1] ARBORA-HARNESS-PLUGINS-R001.2 Core composer reads the registry and the content packages.
- [x] T003 [US1] ARBORA-HARNESS-PLUGINS-R001.3 Renderers write one deterministic tree per profile.
- [x] T004 [US1] ARBORA-HARNESS-PLUGINS-R001.4 Command: arbora plugin bundle.
- [x] T005 [US1] ARBORA-HARNESS-PLUGINS-R001.5 Refusal: typed reasons write no file.
- [x] T006 [US1] ARBORA-HARNESS-PLUGINS-R001.6 No secret and no file outside the tree.
- [x] T007 [US1] ARBORA-HARNESS-PLUGINS-R005.1 Layout of the claude-code tree.
- [x] T008 [US1] ARBORA-HARNESS-PLUGINS-R005.2 Hooks call the door as MCP tools.
- [x] T009 [US1] ARBORA-HARNESS-PLUGINS-R005.3 Marketplace entry with a command source.
- [x] T010 [US1] ARBORA-HARNESS-PLUGINS-R005.4 Status line command.
- [x] T011 [US1] ARBORA-HARNESS-PLUGINS-R005 The claude-code renderer (rollup).
- [x] T012 [US1] ARBORA-HARNESS-PLUGINS-R006.1 Layout: one generated server plugin directory.
- [x] T013 [US1] ARBORA-HARNESS-PLUGINS-R006.2 Setup registers the door, the skills and the commands.
- [x] T014 [US1] ARBORA-HARNESS-PLUGINS-R006.3 Session hooks call the workstream operations.
- [x] T015 [US1] ARBORA-HARNESS-PLUGINS-R006.4 Hot-path proof: a loader fixture runs the generated plugin.
- [x] T016 [US1] ARBORA-HARNESS-PLUGINS-R006 The opencode renderer (rollup).
- [x] T017 [US1] ARBORA-HARNESS-PLUGINS-R001.7 Hot-path proof: the console builds every profile.
- [x] T018 [US1] ARBORA-HARNESS-PLUGINS-R001 One harness-neutral bundle core is composed from the registry (rollup).
- [x] T019 [US1] ARBORA-HARNESS-PLUGINS-R002.1 Model: the install machine.
- [x] T020 [US1] ARBORA-HARNESS-PLUGINS-R002.2 Commands: install, status and remove.
- [x] T021 [US1] ARBORA-HARNESS-PLUGINS-R002.3 Hard cutover: one door per harness.
- [x] T022 [US1] ARBORA-HARNESS-PLUGINS-R002.4 Stale detection and the update channel.
- [x] T023 [US1] ARBORA-HARNESS-PLUGINS-R002.5 Hot-path proof: install, change and status.
- [x] T024 [US1] ARBORA-HARNESS-PLUGINS-R002 The install lifecycle is an explicit state machine (rollup).
- [x] T025 [US2] ARBORA-HARNESS-PLUGINS-R003.1 The bundle MCP entry names the native client and the profile callback.
- [x] T026 [US2] ARBORA-HARNESS-PLUGINS-R003.2 Command: arbora plugin login for command hooks.
- [x] T027 [US2] ARBORA-HARNESS-PLUGINS-R003.3 Model: the credential machine.
- [x] T028 [US2] ARBORA-HARNESS-PLUGINS-R003.4 Refusal: a hook without a credential never blocks.
- [x] T029 [US2] ARBORA-HARNESS-PLUGINS-R003 The harness signs in through the native client, with no credential in the bundle (rollup).
- [ ] T030 [US3] ARBORA-HARNESS-PLUGINS-R004.1 Model: workstream params and results.
- [ ] T031 [US3] ARBORA-HARNESS-PLUGINS-R004.2 The attach operation joins a harness session to the workstream.
- [ ] T032 [US3] ARBORA-HARNESS-PLUGINS-R004.3 The checkpoint operation records a handoff.
- [ ] T033 [US3] ARBORA-HARNESS-PLUGINS-R004.4 Detach, get and list, with a uniform denial.
- [ ] T034 [US3] ARBORA-HARNESS-PLUGINS-R004.5 Model: the attachment machine.
- [ ] T035 [US3] ARBORA-HARNESS-PLUGINS-R004.6 Registration and scopes.
- [ ] T036 [US3] ARBORA-HARNESS-PLUGINS-R004.7 Hot-path proof: two harnesses share one workstream over MCP.
- [ ] T037 [US3] ARBORA-HARNESS-PLUGINS-R004 Workstream operations share session context across harnesses (rollup).
- [x] T038 [US4] ARBORA-HARNESS-PLUGINS-R007 Command hook runner.
- [x] T039 [US4] ARBORA-HARNESS-PLUGINS-R008 The codex renderer.
- [x] T040 [US4] ARBORA-HARNESS-PLUGINS-R009 The grok renderer.
- [ ] T041 [US4] ARBORA-HARNESS-PLUGINS-R010.1 The hosted profile renders a connection sheet.
- [ ] T042 [US4] ARBORA-HARNESS-PLUGINS-R010.2 A hosted client with the vendor redirect.
- [ ] T043 [US4] ARBORA-HARNESS-PLUGINS-R010.3 The playbook attaches and checkpoints.
- [ ] T044 [US4] ARBORA-HARNESS-PLUGINS-R010 A hosted coding agent reaches the door (rollup).
