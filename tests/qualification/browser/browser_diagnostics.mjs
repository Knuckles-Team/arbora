/** Fixed-category diagnostics only; never accepts bodies, URLs or error messages. */
const stages = new Set([
  "replay-operation-refusal",
  "preflight",
  "login-navigation",
  "signed-out-session",
  "username-field",
  "password-field",
  "login-submit",
  "login-observer-setup",
  "login-response",
  "login-response-body",
  "login-outcome",
  "login-form-clearing",
  "authenticated-session",
  "authenticated-role-binding",
  "canonical-task-list",
  "canonical-task-get",
  "canonical-current-plan",
  "canonical-plan-freshness",
  "work-navigation",
  "mounted-task-heading",
  "mounted-plan-text",
  "mounted-plan-status",
  "replay-context",
  "replay-navigation",
  "replay-cookie-install",
  "replay-session-refusal",
  "chromium-launch",
  "browser-context",
  "plan-screenshot",
  "logout-cookie-properties",
  "logout-menu-open",
  "logout-submit",
  "logout-response",
  "mounted-plan-clearing",
  "revocation-plan-freshness",
  "revocation-cookie-freshness",
  "revocation-document-identity",
  "native-revocation-command",
  "native-revocation-acknowledgement",
  "revocation-focus-refresh",
  "revoked-session-refusal",
  "revoked-output-clearing",
  "revoked-heading-clearing",
  "same-document-assertion",
  "denied-canonical-reads",
  "browser-context-close",
  "missing-csrf-refusal",
  "wrong-csrf-refusal",
  "csrf-retained-identity",
]);
const journeys = new Set([
  "preflight",
  "genuine-local-login-and-canonical-reads",
  "same-origin-missing-and-wrong-csrf-refusal",
  "real-logout-and-cookie-replay-refusal",
  "native-revocation-with-same-mounted-stale-clearing",
  "distinct-no-scope-human-refusal",
]);
const statuses = new Set(["navigation", "session", "login", "operation"]);
const flags = new Set([
  "signed_out_local",
  "csrf_request_observed",
  "csrf_header_present",
  "csrf_header_expected",
  "login_body_read",
  "login_outcome_ok",
  "authenticated",
  "page_error",
  "asset_request_failed",
  "asset_http_error",
]);
const errors = new Set([
  "Error",
  "TimeoutError",
  "AssertionError",
  "TypeError",
]);

export function createDiagnostics() {
  let stage = "preflight";
  let observations = {};
  return {
    reset() {
      stage = "preflight";
      observations = {};
    },
    stage(value) {
      if (stages.has(value)) stage = value;
    },
    status(key, value) {
      if (
        statuses.has(key) &&
        Number.isInteger(value) &&
        value >= 100 &&
        value <= 599
      )
        observations[key] = value;
    },
    flag(key, value) {
      if (flags.has(key) && typeof value === "boolean")
        observations[key] = value;
    },
    loginBody(result) {
      let failure, type, length;
      try {
        failure = result.body_failure;
        type = result.content_type;
        length = result.body_length;
      } catch {
        return;
      }
      if (
        [
          "unobserved",
          "metadata",
          "redirect",
          "network",
          "timeout",
          "retrieval",
          "oversize",
          "utf8",
          "json",
          "none",
        ].includes(failure)
      )
        observations.login_body_failure = failure;
      if (["missing", "json", "other"].includes(type))
        observations.login_content_type = type;
      if (Number.isInteger(length) && length >= 0 && length <= 4096)
        observations.login_body_length = length;
    },
    snapshot(error, journey = "preflight") {
      let name;
      try {
        name = error?.name;
      } catch {
        name = "Error";
      }
      return {
        stage,
        journey: journeys.has(journey) ? journey : "preflight",
        error_class: errors.has(name) ? name : "Error",
        observations: { ...observations },
      };
    },
  };
}
