#!/usr/bin/env bash
# Repository-local pre-commit hooks that need prerequisites a fresh clone may
# lack (the synced test environment that scripts/bootstrap.sh provisions; every
# dependency installs from PyPI, so no sibling source checkout is needed).
#
#   scripts/local_hook.sh pytest [pytest args...]   full test suite
#   scripts/local_hook.sh mypy-env                  mypy against the locked environment
#   scripts/local_hook.sh dependency-readiness      release-tag index readiness
#
# A missing prerequisite prints "SKIPPED (<gate>): <reason>" and exits 0
# locally; with $CI set it prints "<gate>: CANNOT RUN: <reason>" and exits 2,
# so hosted CI never passes a gate that did not run.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

gate="${1:-}"
shift || true

unavailable() {
  case "${CI:-}" in
    "" | 0 | false | no)
      echo "SKIPPED ($gate): $1; run ${2:-scripts/bootstrap.sh}" >&2
      exit 0
      ;;
    *)
      echo "$gate: CANNOT RUN: $1" >&2
      exit 2
      ;;
  esac
}

require_synced_env() {
  [ -x .venv/bin/python ] || unavailable "the locked test environment (.venv) is not synced"
}

case "$gate" in
  mypy-env)
    require_synced_env
    exec uv run --no-sync mypy arbora tests
    ;;
  pytest)
    require_synced_env
    # The published epistemic-graph wheel carries the numeric kernel.
    .venv/bin/python -c "import epistemic_graph.numeric" 2>/dev/null \
      || unavailable "the locked epistemic-graph wheel is not installed"
    exec uv run --no-sync python -m pytest "$@"
    ;;
  dependency-readiness)
    source=""
    for candidate in .uv-workspace-siblings/repository-manager ../repository-manager; do
      if [ -e "$candidate/pyproject.toml" ]; then
        source="$candidate"
        break
      fi
    done
    [ -n "$source" ] || unavailable "no repository-manager checkout at .uv-workspace-siblings/repository-manager or ../repository-manager"
    # --no-sources: the readiness tool's own agent-utilities requirement
    # resolves from PyPI, never from a sibling source path it declares.
    exec uv run --no-project --no-sources --python 3.14 --prerelease=allow --with "$source" \
      python -m repository_manager.dependency_readiness .
    ;;
  *)
    echo "usage: scripts/local_hook.sh {mypy-env|pytest|dependency-readiness} [args...]" >&2
    exit 2
    ;;
esac
