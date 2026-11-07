// The arbora door for this harness (ARBORA-HARNESS-PLUGINS-R006).
//
// Generated at the point of use by `arbora plugin bundle --harness opencode`;
// never committed. The renderer writes bundle.json beside this module; this
// file holds no endpoint, host, path or credential.
//
// setup() registers the door MCP server (native client, fixed loopback
// callback), the bundle skills and the handoff/resume commands. The session
// hooks reach the workstream operations through the arbora hook runner, which
// signs in with the `arbora plugin login` credential. A failed call adds no
// context and never blocks the session.
import { Plugin } from "@opencode/plugin"
import { readFileSync } from "node:fs"
import { spawn } from "node:child_process"

type Bundle = {
  harness: string
  door: { name: string; url: string; clientId: string; redirectUri: string }
  skills: string[]
  commands: { name: string; description: string; template: string }[]
  hooks: Record<string, string>
  runner: string
}

const here = new URL(".", import.meta.url)
const bundle: Bundle = JSON.parse(readFileSync(new URL("bundle.json", here), "utf8"))
const RUNNER_TIMEOUT_MS = 5000

export function runHook(point: string, input: Record<string, unknown>): Promise<string> {
  return new Promise((resolve) => {
    const [cmd, ...args] = bundle.runner.split(" ")
    let out = ""
    let done = false
    const finish = (value: string) => {
      if (!done) {
        done = true
        resolve(value)
      }
    }
    try {
      const child = spawn(cmd, [...args, point, "--harness", bundle.harness], {
        stdio: ["pipe", "pipe", "ignore"],
      })
      const timer = setTimeout(() => {
        child.kill()
        finish("")
      }, RUNNER_TIMEOUT_MS)
      child.stdout.on("data", (chunk) => (out += chunk))
      child.on("error", () => finish(""))
      child.stdin.on("error", () => finish(""))
      child.on("close", () => {
        clearTimeout(timer)
        finish(out.trim())
      })
      child.stdin.end(JSON.stringify(input))
    } catch {
      finish("")
    }
  })
}

function hookContext(text: string): string {
  try {
    const parsed = JSON.parse(text)
    return String(parsed?.context ?? "")
  } catch {
    return text
  }
}

function skillText(name: string): string {
  return readFileSync(new URL(`skills/${name}/SKILL.md`, here), "utf8")
}

function skillDescription(text: string): string {
  const match = text.match(/^description:\s*["']?(.+?)["']?\s*$/m)
  return match ? match[1] : ""
}

export default Plugin.define({
  id: "arbora",
  async setup(ctx: any) {
    ctx.mcp.transform((editor: any) => {
      editor.set(bundle.door.name, {
        type: "remote",
        url: bundle.door.url,
        oauth: { clientId: bundle.door.clientId, redirectUri: bundle.door.redirectUri },
      })
    })
    ctx.skill.transform((editor: any) => {
      for (const name of bundle.skills) {
        const content = skillText(name)
        editor.add({
          id: name,
          name,
          description: skillDescription(content),
          path: new URL(`skills/${name}/SKILL.md`, here).pathname,
          content,
        })
      }
    })
    ctx.command.transform((editor: any) => {
      for (const command of bundle.commands) {
        editor.add({ name: command.name, description: command.description, template: command.template })
      }
    })
    const attached = new Set<string>()
    const directory = () => String(ctx.location?.directory ?? process.cwd())
    ctx.session.hook("context", async (event: any) => {
      const sessionID = String(event?.sessionID ?? event?.session?.id ?? "")
      if (!sessionID || attached.has(sessionID)) return
      attached.add(sessionID)
      const text = hookContext(
        await runHook(bundle.hooks["session.context"], { session_id: sessionID, cwd: directory() }),
      )
      if (text) event.system.push({ type: "text", text })
    })
    ctx.session.hook("compaction", async (event: any) => {
      const sessionID = String(event?.sessionID ?? event?.session?.id ?? "")
      if (sessionID) {
        await runHook(bundle.hooks["session.compaction"], { session_id: sessionID, trigger: "auto" })
      }
    })
    ctx.event.subscribe({
      types: ["session.deleted"],
      handler: async (event: any) => {
        const sessionID = String(event?.properties?.info?.id ?? event?.sessionID ?? "")
        if (sessionID && attached.delete(sessionID)) {
          await runHook(bundle.hooks["session.deleted"], { session_id: sessionID })
        }
      },
    })
  },
})
