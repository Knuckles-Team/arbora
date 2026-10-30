#!/usr/bin/env bash
# Local proposal only; source-to-wheel attestations must be reviewed by hosted workflow.
set -euo pipefail
: "${QUALIFICATION_ROOT:?absolute fresh qualification root}"
: "${WHEELHOUSE:?absolute reviewed wheel directory}"
: "${BINDINGS:?absolute reviewed candidate-bindings.json}"
: "${RESTART_BUNDLE:?absolute reviewed restart bundle}"
: "${TUIG_BIN:?absolute verified actual Tuig binary}"
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Boot subprocesses inherit PYTHON*; prevent source overlays, assertion disabling,
# user startup modules and caller-supplied pytest options/plugins.
for name in $(compgen -e); do
  case "$name" in PYTHON*|PYTEST*|UV_*|PIP_*) unset "$name" ;; esac
done
export PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python3 -I "$HERE/qualification_guards.py" preflight \
 "$BINDINGS" "$WHEELHOUSE" "$RESTART_BUNDLE" "$TUIG_BIN" \
 "$HERE/restart-bundle-manifest.json" "$QUALIFICATION_ROOT"
# Every dependency other than the reviewed candidate wheels resolves from
# Arbora's own uv.lock; a fresh resolve could pull an unreviewed release.
uv --no-config export --frozen --project "$HERE/../.." --all-extras --no-hashes \
 --no-emit-project --no-emit-package medulla --no-emit-package medulla-stem \
 --no-emit-package scion-sdk --no-emit-package meristem \
 -o "$QUALIFICATION_ROOT/evidence/arbora-lock-constraints.txt"
uv --no-config pip compile --python-version 3.12 --generate-hashes \
 -c "$QUALIFICATION_ROOT/evidence/arbora-lock-constraints.txt" \
 "$QUALIFICATION_ROOT/requirements.in" -o "$QUALIFICATION_ROOT/evidence/requirements.lock"
uv --no-config venv --python 3.12 "$QUALIFICATION_ROOT/env"
uv --no-config pip install --python "$QUALIFICATION_ROOT/env/bin/python" --require-hashes -r "$QUALIFICATION_ROOT/evidence/requirements.lock"
uv --no-config pip check --python "$QUALIFICATION_ROOT/env/bin/python" | tee "$QUALIFICATION_ROOT/evidence/dependency-check.txt"
"$QUALIFICATION_ROOT/env/bin/python" -I "$HERE/qualification_guards.py" installed "$BINDINGS" > "$QUALIFICATION_ROOT/evidence/installed-origins.json"
cp -a "$RESTART_BUNDLE" "$QUALIFICATION_ROOT/restart"
cd "$QUALIFICATION_ROOT/restart"
export ARBORA_RUN_NATIVE_HISTORY_ACCEPTANCE=1 ARBORA_HISTORY_TUIG_BIN="$TUIG_BIN"
# Raw assertion/model/connector traces stay private on the ephemeral runner.
if ! "$QUALIFICATION_ROOT/env/bin/python" -I -m pytest -p no:cacheprovider -rA \
 --basetemp="$QUALIFICATION_ROOT/evidence/native-processes" \
 --junitxml="$QUALIFICATION_ROOT/evidence/native-history-restart.xml" \
 tests/qualification/test_native_history_restart.py \
 > "$QUALIFICATION_ROOT/evidence/private-native-history.log" 2>&1; then
  printf '%s\n' 'Installed restart qualification failed; raw synthetic-runtime logs withheld.' >&2
  printf '%s\n' '{"status":"failed","scope":"two-installed-restart-journeys"}' > "$QUALIFICATION_ROOT/evidence/gate-result.json"
  exit 1
fi
python3 -I "$HERE/qualification_guards.py" junit "$QUALIFICATION_ROOT/evidence/native-history-restart.xml"
printf '%s\n' '{"status":"passed","tests":2,"skips":0,"scope":"two-installed-restart-journeys"}' > "$QUALIFICATION_ROOT/evidence/gate-result.json"
