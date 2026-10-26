/** Observe the real login reply before a successful form reload retires it. */
export function observeLoginResponse(page) {
  return page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/auth/login" &&
        response.request().method() === "POST",
    )
    .then(
      async (response) => {
        const status = response.status();
        try {
          // Consume immediately in the response callback, not after click() has
          // awaited the application's reload. Retain no body or credential data.
          const body = await response.json();
          return {
            observed: true,
            status,
            body_read: true,
            outcome_ok: body?.outcome === "ok",
          };
        } catch {
          return {
            observed: true,
            status,
            body_read: false,
            outcome_ok: false,
          };
        }
      },
      () => ({
        observed: false,
        status: null,
        body_read: false,
        outcome_ok: false,
      }),
    );
}
