# ARBORA-HARNESS-PLUGINS — Research

This file records the extension points of each target harness, read on 2026-11-07. Each harness is an integration target, so this file names it by its profile identifier. The rows in requirements.md depend on these facts. A fact marked "to verify" needs a check on a live harness before its renderer row lands.

## Sources

| Profile | Source |
|---|---|
| `claude-code` | https://code.claude.com/docs/en/plugins-reference, https://code.claude.com/docs/en/plugins/marketplace-reference, https://code.claude.com/docs/en/hooks |
| `opencode` | https://opencode.ai/docs/plugins/, https://opencode.ai/docs/build/plugins/server, https://opencode.ai/docs/skills/ |
| `codex` | https://learn.chatgpt.com/docs/plugins, https://developers.openai.com/codex/hooks |
| `grok` | https://github.com/xai-org/plugin-marketplace, the skills, plugins and marketplaces page of the harness documentation |

## `claude-code`

- **Manifest.** `.claude-plugin/plugin.json`. Only `name` is required. A name must not start with the vendor prefix, so the plugin name is `arbora`. `version` pins the plugin; a command source derives the version from a hash of the copied files.
- **Parts.** `skills/<name>/SKILL.md`, `commands/`, `agents/*.md`, `hooks/hooks.json`, `.mcp.json`, `.lsp.json`, `output-styles/`, `monitors/monitors.json`, `bin/` and `settings.json`.
- **Settings.** Plugin `settings.json` applies only `agent` and `subagentStatusLine`. A plugin cannot set the main status line. Row R005.4 answers this gap with a command that the developer selects.
- **User config.** `userConfig` prompts at enable time. A `sensitive` value goes to the platform credential store. The bundle needs none, because the endpoint is known at generation time.
- **Path variables.** `${CLAUDE_PLUGIN_ROOT}` changes on update. `${CLAUDE_PLUGIN_DATA}` survives updates.
- **Hooks.** Handler types are `command`, `http`, `mcp_tool`, `prompt` and `agent`. An `mcp_tool` hook names server `plugin:<plugin>:<server>`, a tool and an `input` map with `${path}` substitution from the hook input. The tool text output parses like command stdout. The hook never starts an OAuth flow. Launch-time `SessionStart` and `Setup` hooks fire before MCP servers connect, so their `mcp_tool` hooks are skipped. `UserPromptSubmit` supports `mcp_tool` with a 30-second default timeout. Every hook input carries `session_id`, `transcript_path` and `cwd`.
- **Events.** `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`, `PostCompact`, `Stop`, `SubagentStart`, `SubagentStop`, `SessionEnd` and others.
- **Marketplace.** `.claude-plugin/marketplace.json` with `name`, `owner` and `plugins`. Plugin sources: relative path, `github`, `url`, `git-subdir`, `npm`, `archive` and `command`. A `command` source prints the plugin directory path. The harness reruns it once per session. This is the point-of-use update channel of R005.3. Marketplace sources: `github`, `git`, `url`, `file`, `directory` and `settings`. A team can preload a marketplace with `extraKnownMarketplaces`.
- **Validator.** `claude plugin validate --strict` checks the manifest, the paths and the MCP entries.
- **Native client callback.** The native client contract already lists the fixed loopback callback of this profile.

## `opencode`

- **Loading.** The `plugins` array of the config file takes package names, local directories and objects with options. The harness auto-discovers `.ts` and `.js` files and package directories in the global plugin directory and in `.opencode/plugins/`. The `opencode plugin add|list|check|update|remove` commands manage plugins.
- **API.** A plugin is `Plugin.define({ id, setup(ctx) })` from one `@opencode/plugin` dependency. A plugin package may hold a server part, a terminal part, a desktop part and an RPC contract.
- **Server context.** `ctx` holds `app`, `location`, `options`, `storage` and the namespaces `agent`, `command`, `mcp`, `skill`, `tool`, `session`, `permission`, `credential`, `event` and others.
- **Hooks.** `ctx.session.hook` takes `prompt`, `context`, `compaction`, `generate`, `title`, `model.request`, `http.request`, `http.response` and `retry`. `ctx.tool.hook` takes `execute.before` and `execute.after`. `ctx.permission.hook("evaluate")` and `ctx.shell.hook("create.before")` also exist. `ctx.event.subscribe` streams server events.
- **Registrations.** `ctx.mcp.transform` sets, updates and removes MCP servers. `ctx.skill.transform` adds skills. `ctx.command.transform` adds commands. `ctx.agent.transform` updates agents but has no add method. `ctx.tool.transform` adds tools.
- **Skills.** The harness reads skills from its own global and project skill directories. It also reads two shared compatibility directories. The `skills` config array adds directories and HTTP catalogs.
- **Native client callback.** The native client contract already lists the fixed remote MCP OAuth callback of this profile.
- **To verify.** Whether a session hook can call a tool of a connected MCP server through `ctx`. If it cannot, the generated module calls the served HTTP operations with the runner credential of R003.2.
- **To verify.** Whether an agent can be added. The renderer adds agents only after this check; R006.2 registers none.

## `codex`

- **Plugins.** A plugin bundles skills, MCP servers and hooks. The manifest path is `.codex-plugin/plugin.json`; to verify on a live harness. The `/plugins` command installs from a configured marketplace. A repository marketplace is one source type.
- **Hooks.** Hook files load from the user and project config directories and from plugins. Hooks accumulate across layers. The events include `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest` and `Stop`. `PreCompact` and subagent events are reported in later versions; to verify. Hook timeouts are in seconds. The developer must trust plugin hooks before they run.
- **MCP.** Servers use standard input and output or streamable HTTP. The config file holds per-server overrides.
- **Skills.** The harness loads skills by description match or by name.
- **Gaps.** No MCP-tool hook type is documented, so hooks run the R007 runner. No session end event is documented, so no detach is sent. No status line extension is documented.

## `grok`

- **Plugins.** Plugins load from the project and user plugin directories and from a `--plugin-dir` flag. A plugin bundles skills, commands, agents, hooks, MCP servers and LSP servers in the shared layout.
- **Marketplace.** `.grok-plugin/marketplace.json` lists entries with `name` and `source`. A remote source pins a 40-character commit SHA. A local source names a directory.
- **Compatibility.** The harness documentation states that it reads the plugin, marketplace, skill, MCP and hook files of the `claude-code` layout. This is to verify. R009 reuses the shared layout either way.
- **Gaps.** The hook handler types are not documented, so hooks run the R007 runner.

## Workspace survey

- Today the `claude-code` user config holds one hand-written `http` entry for the door. The user skill directory holds a large set of links to skill folders. The `opencode` workspace config holds one remote door entry with the native client ID and the fixed callback. R002.3 turns these into a `DUPLICATE_DOOR` refusal at install.
- `arbora/identity/client_contract.py` declares the native client and two loopback callbacks. R030.4.7 moves the callback list to the profile table.
- `arbora/console.py:main` already dispatches `identity reconcile` before server start. The `plugin` command follows the same form.
- No repository holds a plugin or marketplace file of the ecosystem today.
- The `universal-installer` skill of the catalog links skills into harness skill directories and patches harness MCP config. It holds a per-target frontmatter contract table. `US-CONTENT-R007.4` stops it from wiring the four bundle profiles, so one door exists per harness. `US-CONTENT-R007.1` makes every skill pass the strictest contract as written, so the renderers copy skills unchanged.

## Open owner decisions

| ID | Decision | Default in these rows |
|---|---|---|
| D1 | Where the public marketplace catalog lives, if any. Options: no public catalog, a catalog file in the arbora repository, or a separate catalog repository. | No public catalog. Install writes a local marketplace file. |
| D2 | Whether the `opencode` module ships as a template in the arbora wheel or as a package on the public registry. | Template in the wheel. |
| D3 | Whether hooks may write to arbora without a preview. | Yes, only the workstream operations, only on the caller's own session, with `Confirm.NONE`. |
| D4 | Whether the bundle skill set is a declared list or an engine selection per profile. | Declared list, empty by default, plus the door skills. |
| D5 | Whether the workspace digest uses the root path or the origin remote and branch. | Root path, hashed with the principal by arbora. Two worktrees are two workstreams. |
| D6 | Whether install may remove a hand-written door entry itself. | No. Install refuses and prints the step. |
| D7 | Whether the catalog installer keeps its other harness targets or is removed whole. | It keeps the other targets; only the four bundle profiles move. |
