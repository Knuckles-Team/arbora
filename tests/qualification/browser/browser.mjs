/**
 * Isolated CI: real installed-candidate browser reads.
 * No mocked routes, operations, registry, identities, TLS bypass, traces or HAR.
 *
 * Usage: node browser.mjs RUNTIME_DIRECTORY CREDENTIALS_FILE OUTPUT_DIRECTORY DENIED_CREDENTIALS_FILE
 * ready.json: { origin, task_id, run_id, context_id, principal_id, denied_principal_id }.
 * Both principal IDs are native-returned public identifiers; never fabricated.
 * The denied credential belongs to a separate no-scope human, not a mutation
 * of the positive principal. Cross-origin login is not qualified here: a
 * second trusted HTTPS origin would be required; no Origin-header spoofing.
 * Credentials: regular non-symlink mode-0600 JSON { username, password }.
 * The fixture provisions the test identity/keys and Chromium's trusted CA in
 * $HOME/.pki/nssdb/cert9.db; QUALIFICATION_BROWSER_HOME must equal this temp HOME.
 * The fixture observes control.json { action:'revoke_qualification_sessions',
 * request_id }, revokes through its real native owner, then atomically writes
 * control-result.json { request_id, completed:true }. No credentials in control.
 * The provider keeps the admitted task/current plan valid until this finishes.
 */
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { chromium, expect } from "@playwright/test";

let phase = "preflight";
let browser;
const completed = [];
const [runtimeArg, credentialsArg, outputArg, deniedCredentialsArg] =
  process.argv.slice(2);
const timeout = 60_000;

function requireCondition(condition) {
  // Never put credential/session/cookie values in assertion error messages.
  assert.ok(condition, `Qualification condition failed in ${phase}`);
}

async function readJson(file) {
  return JSON.parse(await fs.readFile(file, "utf8"));
}

async function waitForRevocation(runtime, requestId) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const ack = await readJson(path.join(runtime, "control-result.json"));
      if (ack.request_id === requestId) {
        requireCondition(ack.completed === true);
        return;
      }
    } catch (error) {
      if (error.code !== "ENOENT" && !(error instanceof SyntaxError))
        throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Native revocation acknowledgement timed out");
}

async function session(page) {
  return page.evaluate(async () => {
    const response = await fetch("/auth/session", {
      credentials: "same-origin",
    });
    return { status: response.status, value: await response.json() };
  });
}

async function invoke(page, operation, params, csrf) {
  return page.evaluate(
    async ({ operation, params, csrf }) => {
      const response = await fetch(`/api/v1/ops/${operation}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          ...(csrf === undefined ? {} : { "X-CSRF-Token": csrf }),
        },
        body: JSON.stringify(params),
      });
      return { status: response.status, value: await response.json() };
    },
    { operation, params, csrf },
  );
}

function unwrap(reply) {
  requireCondition(reply.status === 200 && reply.value.ok === true);
  requireCondition(typeof reply.value.meta?.registry_digest === "string");
  requireCondition(reply.value.meta.registry_digest.length > 0);
  return reply.value.result.value;
}

async function login(page, expected, credentials, denied = false) {
  await page.goto(expected.origin, { waitUntil: "domcontentloaded" });
  const signedOut = await session(page);
  requireCondition(
    signedOut.status === 200 && signedOut.value.authenticated === false,
  );
  requireCondition(signedOut.value.mode === "local");
  await page.getByLabel("Username", { exact: true }).fill(credentials.username);
  await page.getByLabel("Password", { exact: true }).fill(credentials.password);
  const loggedIn = page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/auth/login" &&
        response.request().method() === "POST",
    )
    .catch(() => null);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await loggedIn;
  requireCondition(response !== null);
  requireCondition(
    response.status() === 200 && (await response.json()).outcome === "ok",
  );
  await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0, {
    timeout,
  });
  const current = await session(page);
  requireCondition(
    current.status === 200 && current.value.authenticated === true,
  );
  requireCondition(current.value.mode === "local");
  requireCondition(
    current.value.subject ===
      (denied ? expected.denied_principal_id : expected.principal_id),
  );
  if (!denied) {
    requireCondition(current.value.webui_role === "user");
    requireCondition(
      Array.isArray(current.value.roles) &&
        current.value.roles.includes("webui:user"),
    );
  }
  requireCondition(
    typeof current.value.csrf_token === "string" &&
      current.value.csrf_token.length > 0,
  );
  return current.value;
}

async function readCredentials(file) {
  const credentialInfo = await fs.lstat(file);
  requireCondition(credentialInfo.isFile() && !credentialInfo.isSymbolicLink());
  requireCondition((credentialInfo.mode & 0o777) === 0o600);
  const credentials = await readJson(file);
  requireCondition(
    Object.keys(credentials).sort().join(",") === "password,username",
  );
  requireCondition(
    typeof credentials.username === "string" && credentials.username.length > 0,
  );
  requireCondition(
    typeof credentials.password === "string" && credentials.password.length > 0,
  );
  return credentials;
}

function assertForbidden(reply) {
  const forbidden = new Set([
    "UNAUTHENTICATED",
    "ACCESS_DENIED",
    "SUBJECT_ACCESS_DENIED",
    "PRINCIPAL_NOT_ALLOWED",
    "SCOPE_REQUIRED",
    "POLICY_DENIED",
    "PERMISSION_DENIED",
  ]);
  requireCondition(
    reply.value.ok !== true && reply.value.result?.value === undefined,
  );
  requireCondition(!JSON.stringify(reply.value).includes('"text":"inspect"'));
  requireCondition(
    [401, 403].includes(reply.status) ||
      (reply.status === 200 &&
        reply.value.ok === false &&
        forbidden.has(reply.value.error?.code)),
  );
}

async function assertCsrfRefused(page, expected) {
  for (const csrf of [undefined, randomUUID()]) {
    const refused = await invoke(
      page,
      "agents.tasks.get",
      { task_id: expected.task_id },
      csrf,
    );
    requireCondition([401, 403].includes(refused.status));
    assertForbidden(refused);
    const retained = await session(page);
    requireCondition(
      retained.status === 200 && retained.value.authenticated === true,
    );
    requireCondition(retained.value.subject === expected.principal_id);
  }
}

async function assertReads(page, expected, identity) {
  const listed = unwrap(
    await invoke(page, "agents.tasks.list", { limit: 50 }, identity.csrf_token),
  );
  requireCondition(
    Array.isArray(listed.tasks) &&
      listed.tasks.some((task) => task.id === expected.task_id),
  );
  requireCondition(
    listed.nextCursor === null || typeof listed.nextCursor === "string",
  );
  const task = unwrap(
    await invoke(
      page,
      "agents.tasks.get",
      { task_id: expected.task_id },
      identity.csrf_token,
    ),
  );
  requireCondition(
    task.id === expected.task_id && task.contextId === expected.context_id,
  );
  requireCondition(task.metadata?.arbora?.runId === expected.run_id);
  const read = unwrap(
    await invoke(
      page,
      "runs.current_plan",
      { run_id: task.metadata.arbora.runId },
      identity.csrf_token,
    ),
  );
  requireCondition(
    read.availability === "available" &&
      read.snapshot.run_id === expected.run_id,
  );
  requireCondition(
    read.snapshot.items.some(
      (item) => item.text === "inspect" && item.status === "in_progress",
    ),
  );
  const expires = Date.parse(read.snapshot.expires_at);
  requireCondition(expires > Date.now());
  return expires;
}

async function mountedPlan(page, expected) {
  await page.goto(
    `${expected.origin}/workspace/work?task=${encodeURIComponent(expected.task_id)}`,
    {
      waitUntil: "domcontentloaded",
    },
  );
  await expect(
    page.getByRole("heading", {
      name: `Task ${expected.task_id}`,
      exact: true,
    }),
  ).toBeVisible({ timeout });
  const plan = page.getByRole("region", { name: "Current plan", exact: true });
  await expect(plan.getByText("inspect", { exact: true })).toBeVisible({
    timeout,
  });
  await expect(plan.getByText("(In progress)", { exact: true })).toBeVisible();
  return plan;
}

async function replayRefused(browser, origin, cookies, csrf, taskId) {
  const replay = await browser.newContext({ serviceWorkers: "block" });
  try {
    const page = await replay.newPage();
    // Public health page establishes the trusted same origin without mounting
    // a new app which might consume/retire the cookie before this check.
    await page.goto(`${origin}/health`, { waitUntil: "domcontentloaded" });
    await replay.addCookies(cookies);
    const refused = await invoke(
      page,
      "agents.tasks.get",
      { task_id: taskId },
      csrf,
    );
    requireCondition(refused.status === 401 && refused.value.ok !== true);
    await replay.addCookies(cookies);
    const identity = await session(page);
    requireCondition(
      identity.status === 401 && identity.value.authenticated !== true,
    );
  } finally {
    await replay.close();
  }
}

try {
  requireCondition(
    Boolean(runtimeArg && credentialsArg && outputArg && deniedCredentialsArg),
  );
  const runtime = path.resolve(runtimeArg);
  const output = path.resolve(outputArg);
  const expected = await readJson(path.join(runtime, "ready.json"));
  requireCondition(
    Object.keys(expected).sort().join(",") ===
      "context_id,denied_principal_id,origin,principal_id,run_id,task_id",
  );
  const origin = new URL(expected.origin);
  requireCondition(
    origin.protocol === "https:" && !origin.username && !origin.password,
  );
  requireCondition(origin.origin === expected.origin);
  requireCondition(
    ["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname) ||
      origin.hostname.endsWith(".test"),
  );
  requireCondition(/^(?:a2a-|work-item:)[0-9a-f]{64}$/.test(expected.task_id));
  requireCondition(/^a2a-context-[0-9a-f]{64}$/.test(expected.context_id));
  requireCondition(
    typeof expected.run_id === "string" && expected.run_id.length > 0,
  );
  requireCondition(
    expected.task_id !== expected.run_id &&
      expected.context_id !== expected.run_id,
  );
  requireCondition(
    typeof expected.principal_id === "string" &&
      expected.principal_id.length > 0,
  );
  requireCondition(
    typeof expected.denied_principal_id === "string" &&
      expected.denied_principal_id.length > 0,
  );
  requireCondition(expected.principal_id !== expected.denied_principal_id);
  const credentials = await readCredentials(credentialsArg);
  const deniedCredentials = await readCredentials(deniedCredentialsArg);
  requireCondition(credentials.username !== deniedCredentials.username);
  requireCondition(!process.env.DEBUG && !process.env.PWDEBUG);
  requireCondition(Boolean(process.env.QUALIFICATION_BROWSER_HOME));
  requireCondition(
    path.resolve(process.env.HOME ?? "") ===
      path.resolve(process.env.QUALIFICATION_BROWSER_HOME),
  );
  await fs.access(path.join(process.env.HOME, ".pki/nssdb/cert9.db"));
  await fs.mkdir(output, { recursive: true, mode: 0o700 });
  // No ignoreHTTPSErrors, --ignore-certificate-errors, route handler, tracing,
  // video recording or HAR recording is permitted in this probe.
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    serviceWorkers: "block",
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);

  phase = "genuine-local-login-and-canonical-reads";
  const identity = await login(page, expected, credentials);
  await assertReads(page, expected, identity);
  const plan = await mountedPlan(page, expected);
  // This cropped, already-signed-in read-only plan has no username/password,
  // cookie or CSRF fields. Never capture the sign-in screen or failure page.
  await plan.screenshot({
    path: path.join(output, "installed-current-plan.png"),
  });
  completed.push(phase);

  phase = "same-origin-missing-and-wrong-csrf-refusal";
  await assertCsrfRefused(page, expected);
  completed.push(phase);

  phase = "real-logout-and-cookie-replay-refusal";
  const cookies = (await context.cookies(expected.origin)).filter(
    (cookie) => cookie.httpOnly,
  );
  requireCondition(
    cookies.length > 0 && cookies.every((cookie) => cookie.secure),
  );
  const displayName = identity.name ?? identity.username ?? identity.subject;
  requireCondition(typeof displayName === "string" && displayName.length > 0);
  const escaped = displayName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  await page.getByRole("button", { name: new RegExp(escaped) }).click();
  const loggedOut = page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/auth/logout" &&
        response.request().method() === "POST",
    )
    .catch(() => null);
  await page.getByRole("menuitem", { name: "Log out", exact: true }).click();
  requireCondition((await loggedOut)?.ok() === true);
  await expect(
    page.getByRole("region", { name: "Current plan", exact: true }),
  ).toHaveCount(0);
  await replayRefused(
    browser,
    expected.origin,
    cookies,
    identity.csrf_token,
    expected.task_id,
  );
  completed.push(phase);

  phase = "native-revocation-with-same-mounted-stale-clearing";
  const renewedIdentity = await login(page, expected, credentials);
  await mountedPlan(page, expected);
  const planExpires = await assertReads(page, expected, renewedIdentity);
  requireCondition(planExpires > Date.now() + 60_000);
  const liveCookies = (await context.cookies(expected.origin)).filter(cookie => cookie.httpOnly);
  requireCondition(liveCookies.length > 0 && liveCookies.every(cookie => cookie.expires * 1000 > Date.now() + 60_000));
  const cookieExpires = Math.min(...liveCookies.map(cookie => cookie.expires * 1000));
  const documentOrigin = await page.evaluate(() => performance.timeOrigin);
  const refusedSession = page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/auth/session" &&
        response.status() === 401,
      { timeout: timeout * 2 },
    )
    .catch(() => null);
  const requestId = randomUUID();
  const command = path.join(runtime, `control-${requestId}.tmp`);
  await fs.writeFile(
    command,
    JSON.stringify({
      action: "revoke_qualification_sessions",
      request_id: requestId,
    }),
    { mode: 0o600 },
  );
  await fs.rename(command, path.join(runtime, "control.json"));
  await waitForRevocation(runtime, requestId);
  // Existing mounted identity lifecycle performs the real session request.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  requireCondition((await refusedSession) !== null);
  requireCondition(Date.now() < planExpires && Date.now() < cookieExpires);
  await expect(
    page.getByRole("region", { name: "Current plan", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", {
      name: "Task output and references",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: `Task ${expected.task_id}`,
      exact: true,
    }),
  ).toHaveCount(0);
  requireCondition(
    (await page.evaluate(() => performance.timeOrigin)) === documentOrigin,
  );
  completed.push(phase);
  phase = "distinct-no-scope-human-refusal";
  const denied = await login(page, expected, deniedCredentials, true);
  for (const [operation, params] of [
    ["agents.tasks.get", { task_id: expected.task_id }],
    ["runs.current_plan", { run_id: expected.run_id }],
  ]) {
    assertForbidden(await invoke(page, operation, params, denied.csrf_token));
  }
  await expect(
    page.getByRole("region", { name: "Current plan", exact: true }),
  ).toHaveCount(0);
  completed.push(phase);
  await context.close();
  await fs.writeFile(
    path.join(output, "browser-result.json"),
    JSON.stringify(
      {
        status: "passed",
        completed,
        unqualified: [
          "cross-origin login: second trusted HTTPS origin not supplied",
        ],
        claims: {
          real_http_reads: true,
          native_principal_binding: true,
          missing_and_wrong_csrf_refusal: true,
          distinct_no_scope_human_refusal: true,
          cross_origin_login: false,
          same_document_revocation_clearing: true,
          live_agent_producer: false,
        },
      },
      null,
      2,
    ) + "\n",
    { mode: 0o600 },
  );
  console.log(
    "PASS installed canonical Work reads, logout replay refusal and same-document native revocation clearing",
  );
} catch (error) {
  // Browser errors may embed DOM values or request data. Only a fixed phase and
  // error class leave this process; credentials/cookies/bodies are never logged.
  console.error(
    `FAIL installed browser qualification: ${phase} (${error?.name ?? "Error"})`,
  );
  process.exitCode = 1;
} finally {
  await browser?.close();
}
