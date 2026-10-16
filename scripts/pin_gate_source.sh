#!/usr/bin/env bash
# Write a copy of .pre-commit-config.yaml whose shared-gate repository
# (Knuckles-Team/pipelines) is at one exact commit, and print its path.
#
# The tracked config keeps `rev: main`. A CI run resolves `main` ONCE (the
# freeze-gate-source job) and every gate in every attempt of that run uses
# the resulting commit, so re-running a job at the same commit cannot pick
# up a newer gate and answer differently.
set -euo pipefail
sha="${1:?usage: pin_gate_source.sh <40-hex pipelines commit> <output path>}"
out="${2:?usage: pin_gate_source.sh <40-hex pipelines commit> <output path>}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "pin_gate_source: not a full commit SHA: $sha" >&2; exit 2; }
config="$(git rev-parse --show-toplevel)/.pre-commit-config.yaml"
python3 - "$config" "$sha" "$out" <<'PY'
import re, sys
config, sha, out = sys.argv[1:4]
lines = open(config, encoding="utf-8").read().split("\n")
hits = 0
for i, line in enumerate(lines):
    if re.match(r"- repo: https://github\.com/Knuckles-Team/pipelines\s*$", line):
        j = i + 1
        if re.match(r"\s+rev: main(\s|$)", lines[j]):
            lines[j] = re.sub(r"rev: main.*", f"rev: {sha}", lines[j])
            hits += 1
if hits != 1:
    sys.exit(f"pin_gate_source: expected one pipelines `rev: main`, found {hits}")
open(out, "w", encoding="utf-8").write("\n".join(lines))
PY
echo "$out"
