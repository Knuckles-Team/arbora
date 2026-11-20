#!/usr/bin/env bash
# Informational ARBORA-ACCEPTANCE-R005 worklist report.
#
# The standalone checker deliberately returns nonzero for a missing acceptance
# receipt. The repository hook reports that gap but must not turn a worklist
# into a pre-push failure; an ACCEPTED state is still enforced by
# check_public_specs.py.
set -u

cd "$(git rev-parse --show-toplevel)"

python3 scripts/check_acceptance_evidence.py "$@"
status=$?
if [ "$status" -eq 0 ] || [ "$status" -eq 1 ]; then
  exit 0
fi
exit "$status"
