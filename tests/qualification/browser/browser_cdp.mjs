/** Observer-only CDP buffer for an actual form response across renderer navigation.
 * Pinned Playwright v1.64.1 protocol: Network.configureDurableMessages.
 * No interception, replay, cookie installation, page injection or body artifacts.
 */
import { readLoginResponse } from "./browser_login.mjs";

const buffers = {
  maxTotalBufferSize: 4 * 1024 * 1024,
  maxResourceBufferSize: 65536,
};

export async function captureLoginResponse(
  page,
  origin,
  click,
  timeout = 60000,
) {
  const target = new URL("/auth/login", origin).href;
  if (new URL(origin).protocol !== "https:" || timeout < 1 || timeout > 60000)
    throw new Error("Invalid login observation configuration");
  let client, timer;
  let requestId, status, contentType;
  let settled = false;
  let started = false;
  let resolveResult;
  const result = new Promise((resolve) => {
    resolveResult = resolve;
  });
  const finish = (value) => {
    if (!settled) {
      settled = true;
      resolveResult(value);
    }
  };
  const failure = (category) =>
    finish({
      observed: requestId !== undefined,
      status: status ?? null,
      body_read: false,
      outcome_ok: false,
      body_failure: category,
      body_length: null,
      content_type: "missing",
    });
  const guard = (callback) => (event) => {
    try {
      callback(event);
    } catch {
      failure("metadata");
    }
  };
  const bounded = async (operation, limit = Math.min(timeout, 5000)) => {
    let deadline;
    try {
      return await Promise.race([
        operation,
        new Promise((_, reject) => {
          deadline = setTimeout(
            () => reject(new Error("Observation command timed out")),
            limit,
          );
        }),
      ]);
    } finally {
      clearTimeout(deadline);
    }
  };
  const listeners = [];
  try {
    client = await page.context().newCDPSession(page);
    const on = (name, callback) => {
      const guarded = guard(callback);
      listeners.push([name, guarded]);
      client.on(name, guarded);
    };
    on("Network.requestWillBeSent", (event) => {
      if (settled) return;
      if (event.requestId === requestId && event.redirectResponse) {
        failure("redirect");
        return;
      }
      if (event.request.method !== "POST" || event.request.url !== target)
        return;
      if (requestId !== undefined || event.redirectResponse) {
        failure("redirect");
        return;
      }
      if (typeof event.requestId !== "string") {
        failure("metadata");
        return;
      }
      requestId = event.requestId;
    });
    on("Network.responseReceived", (event) => {
      if (settled || event.requestId !== requestId) return;
      if (
        event.response.url !== target ||
        (event.response.status >= 300 && event.response.status < 400)
      ) {
        failure("redirect");
        return;
      }
      const value = event.response.status;
      if (!Number.isInteger(value) || value < 100 || value > 599) {
        failure("metadata");
        return;
      }
      status = value;
      // Only this single header is read; cookies and other headers are not retained.
      for (const name of Object.keys(event.response.headers))
        if (name.toLowerCase() === "content-type") {
          const value = event.response.headers[name];
          contentType =
            typeof value !== "string"
              ? undefined
              : value.split(";", 1)[0].trim().toLowerCase() ===
                  "application/json"
                ? "application/json"
                : "application/octet-stream";
        }
    });
    on("Network.loadingFailed", (event) => {
      if (event.requestId === requestId) failure("network");
    });
    on("Network.loadingFinished", (event) => {
      if (settled || started || event.requestId !== requestId) return;
      if (status === undefined) {
        failure("metadata");
        return;
      }
      started = true;
      // Retrieval and durable storage belong to this SAME DevTools session.
      void readLoginResponse({
        status: () => status,
        headers: () => ({ "content-type": contentType }),
        body: async () => {
          const reply = await bounded(
            client.send("Network.getResponseBody", {
              requestId,
            }),
          );
          if (
            typeof reply.body !== "string" ||
            typeof reply.base64Encoded !== "boolean"
          )
            throw new Error("Invalid response transport");
          // Buffering is separately bounded in Chromium; parser acceptance stays4KiB.
          return Buffer.from(
            reply.body,
            reply.base64Encoded ? "base64" : "utf8",
          );
        },
      }).then(finish, () => failure("retrieval"));
    });
    on("close", () => failure("network"));
    // Dedicated command is supported by the pinned protocol; no fallback if absent.
    await bounded(
      client.send("Network.enable", { ...buffers, maxPostDataSize: 0 }),
    );
    await bounded(client.send("Network.configureDurableMessages", buffers));
    timer = setTimeout(() => failure("timeout"), timeout);
    await click(); // The genuine UI click and its navigation are not suppressed.
    return await result;
  } catch {
    throw new Error("Login observation failed");
  } finally {
    clearTimeout(timer);
    settled = true;
    requestId = status = contentType = undefined;
    if (client) {
      for (const [name, callback] of listeners) client.off(name, callback);
      try {
        await bounded(
          client.send("Network.configureDurableMessages", {}),
          1000,
        );
      } catch {
        /* Target closure still requires detachment. */
      }
      try {
        await bounded(client.send("Network.disable"), 1000);
      } catch {
        /* Detach even when the target has closed. */
      }
      try {
        await bounded(client.detach(), 1000);
      } catch {
        /* Browser/context final cleanup remains authoritative. */
      }
    }
  }
}
