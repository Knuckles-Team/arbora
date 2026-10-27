/** Observe the real login reply; export only bounded metadata, never content. */
export async function observeLoginResponse(page) {
  try {
    return await page
      .waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/auth/login" &&
          response.request().method() === "POST",
      )
      .then(readLoginResponse, () => ({
        observed: false,
        status: null,
        body_read: false,
        outcome_ok: false,
        body_failure: "unobserved",
        body_length: null,
        content_type: "missing",
      }));
  } catch {
    return {
      observed: false,
      status: null,
      body_read: false,
      outcome_ok: false,
      body_failure: "unobserved",
      body_length: null,
      content_type: "missing",
    };
  }
}

export async function readLoginResponse(response) {
  const result = {
    observed: true,
    status: null,
    body_read: false,
    outcome_ok: false,
    body_failure: "metadata",
    body_length: null,
    content_type: "missing",
  };
  try {
    const status = response.status();
    if (Number.isInteger(status) && status >= 100 && status <= 599)
      result.status = status;
    // Reading headers is synchronous and cannot delay the body request.
    const type = response.headers()["content-type"];
    result.content_type =
      typeof type !== "string" || !type
        ? "missing"
        : type.split(";", 1)[0].trim().toLowerCase() === "application/json"
          ? "json"
          : "other";
    result.body_failure = "retrieval";
    let bytes;
    try {
      bytes = await response.body();
    } catch {
      return result;
    }
    const length = bytes.length;
    if (!Number.isInteger(length) || length < 0) return result;
    if (length > 4096) {
      result.body_failure = "oversize";
      return result;
    }
    result.body_length = length;
    let text;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      result.body_failure = "utf8";
      return result;
    }
    try {
      const body = JSON.parse(text);
      result.body_read = true;
      result.outcome_ok = body?.outcome === "ok";
      result.body_failure = "none";
    } catch {
      result.body_failure = "json";
    }
  } catch {
    // Metadata/accessor failures must not reject while click is pending.
  }
  return result;
}
