#!/usr/bin/env bash
# One-command contributor setup from a fresh clone (local or Claude Code on the web).
#
#   scripts/bootstrap.sh              uv, pinned Python, locked PyPI deps, git hooks
#   scripts/bootstrap.sh --scanners   also the pinned native scanners (cargo/npm, ~15 min cold)
#   scripts/bootstrap.sh --siblings-only
#                                     `uv sync` only; skips git hooks and scanners
#                                     (the name is kept for existing lane scripts).
#                                     Fresh worktree: `scripts/bootstrap.sh
#                                     --siblings-only && uv run pytest tests/<file>`.
#   scripts/bootstrap.sh --kernel     accepted and ignored: the published
#                                     epistemic-graph wheel carries the kernel.
#
# Idempotent and non-interactive. Every dependency installs from PyPI; no
# sibling source checkout is linked or cloned.
# Afterwards:
#   uvx pre-commit run --all-files
#   uvx pre-commit run pytest --hook-stage manual --all-files
set -euo pipefail

cd "$(dirname "$0")/.."

scanners=0
siblings_only=0
for arg in "$@"; do
  case "$arg" in
    --kernel) ;;
    --scanners) scanners=1 ;;
    --siblings-only) siblings_only=1 ;;
    -h | --help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done
if [[ "$siblings_only" == 1 ]]; then
  scanners=0
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# uv: releases before 0.9 cannot download current CPython patch releases.
uv_minor() { uv --version 2>/dev/null | awk '{split($2, v, "."); print v[1] * 1000 + v[2]}'; }
if ! command -v uv >/dev/null 2>&1 || [[ "$(uv_minor)" -lt 9 ]]; then
  echo "bootstrap: installing a current uv"
  # Fallbacks: an externally-managed (PEP 668) interpreter, then Astral's own
  # self-updater for a standalone uv. Never pipe a downloaded installer to sh.
  python3 -m pip install --quiet --user --upgrade "uv>=0.9" \
    || python3 -m pip install --quiet --user --upgrade --break-system-packages "uv>=0.9" \
    || uv self update
fi

# The one Python pin: the release workflow's setup-python version.
python_version="$(sed -n "s/^ *python-version: '\(.*\)'$/\1/p" .github/workflows/release.yml | head -n 1)"
echo "bootstrap: Python $python_version"
uv python install "$python_version"

# Every dependency, the epistemic-graph engine and its numeric kernel
# included, installs from PyPI at the version uv.lock pins.
echo "bootstrap: locked dependencies (PyPI)"
uv sync --frozen --python "$python_version" --extra test --extra webui

if [[ "$siblings_only" == 1 ]]; then
  echo "bootstrap: --siblings-only, skipping git hooks (shared .git/hooks across worktrees)"
else
  echo "bootstrap: git hooks"
  uvx pre-commit install --hook-type pre-commit --hook-type pre-push
fi

if [[ "$scanners" == 1 ]]; then
  echo "bootstrap: native scanners (cargo/npm builds; ~15 min cold)"
  scanner_root="$HOME/.local/share/graph-os-scanners"
  bins="$(bash scripts/install_scanners.sh "$scanner_root")"
  echo "bootstrap: add the scanners to PATH:"
  echo "$bins" | sed 's/^/  export PATH="/; s/$/:$PATH"/'
fi

echo "bootstrap: done"
