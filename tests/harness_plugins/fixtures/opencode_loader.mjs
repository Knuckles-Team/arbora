// Loader fixture for the generated opencode plugin (ARBORA-HARNESS-PLUGINS-R006.4).
// Usage: node --experimental-strip-types opencode_loader.mjs <plugin dir> <record.json>
// Stubs the harness plugin module, runs setup() with a recording context, then
// fires the session context, compaction and end hooks once each.
import { register } from "node:module"
import { writeFileSync } from "node:fs"
import { pathToFileURL } from "node:url"

const stub = "data:text/javascript,export const Plugin = { define: (d) => d }"
register(
  "data:text/javascript," +
    encodeURIComponent(
      `export async function resolve(s, c, n) { return s === "@opencode/plugin" ? { url: ${JSON.stringify(stub)}, shortCircuit: true } : n(s, c) }`,
    ),
)

const [dir, out] = process.argv.slice(2)
const record = { mcp: {}, skills: [], commands: [], hooks: [], events: [], system: [] }
const editor = (sink) => ({
  set: (name, value) => (record.mcp[name] = value),
  add: (value) => sink.push(value),
})
const hooks = {}
let subscriber = null
const ctx = {
  location: { directory: "/workspace/project" },
  mcp: { transform: (fn) => fn(editor(null)) },
  skill: { transform: (fn) => fn(editor(record.skills)) },
  command: { transform: (fn) => fn(editor(record.commands)) },
  session: { hook: (name, fn) => { record.hooks.push(name); hooks[name] = fn } },
  event: { subscribe: (opts) => { record.events.push(...opts.types); subscriber = opts.handler } },
}
const plugin = (await import(pathToFileURL(`${dir}/server.ts`).href)).default
await plugin.setup(ctx)
const event = { sessionID: "oc-1", system: [] }
await hooks.context(event)
await hooks.context(event)
await hooks.compaction({ sessionID: "oc-1" })
await subscriber({ properties: { info: { id: "oc-1" } } })
record.system = event.system
record.skills = record.skills.map((s) => ({ id: s.id, description: s.description, hasContent: s.content.length > 0 }))
writeFileSync(out, JSON.stringify(record))
