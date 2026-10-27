/** Negative-only real browser transport; production fetch adds missing CSRF. */
export async function invokeWithoutCsrfWrapper(
  page,
  origin,
  operation,
  params,
  csrf,
) {
  const endpoint = `/api/v1/ops/${operation}`;
  const target = new URL(endpoint, origin).href;
  // Observe the actual browser request, not merely the requested XHR options.
  const observed = page
    .waitForRequest(
      (request) => request.url() === target && request.method() === "POST",
    )
    .then(
      async (request) => {
        try {
          const header = await request.headerValue("x-csrf-token");
          return {
            request_observed: true,
            csrf_header_present: header !== null,
            csrf_header_expected:
              csrf === undefined ? header === null : header === csrf,
          };
        } catch {
          return {
            request_observed: false,
            csrf_header_present: false,
            csrf_header_expected: false,
          };
        }
      },
      () => ({
        request_observed: false,
        csrf_header_present: false,
        csrf_header_expected: false,
      }),
    );
  const reply = await page.evaluate(
    ({ endpoint, params, csrf }) =>
      new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open("POST", endpoint);
        request.withCredentials = true;
        request.responseType = "json";
        request.timeout = 20000;
        request.setRequestHeader("Content-Type", "application/json");
        if (csrf !== undefined) request.setRequestHeader("X-CSRF-Token", csrf);
        request.onload = () =>
          resolve({ status: request.status, value: request.response });
        request.onerror =
          request.ontimeout =
          request.onabort =
            () => reject(new Error("Negative browser request failed"));
        // Browser supplies same-origin cookies and Origin; neither is fabricated.
        request.send(JSON.stringify(params));
      }),
    { endpoint, params, csrf },
  );
  return { ...reply, ...(await observed) };
}
