Prepared installed-only paired restart bundle. NOT EXECUTED.
Source: Arbora#525 a5ecf6f8 + #520 0e89e4d9 + reviewed preserved refactor,
metadata rename and boot helper merge. Hashes in source-manifest.json.
Requires coherent installed candidate Arbora/Scion/Meristem4/Medulla2.27.10 and
matching medulla-stem2.27.10 candidate wheel; official candidate packaging is available.
The hosted runner binds new same-run builds separately from earlier evidence.
Do not use --no-deps, source overlays or old overlapping distributions.
Use actual qualified Tuig binary, not screenshot-only gallery binary.
Run in a capable Linux x86_64 executor with loopback process privileges.

uv pip check --python /absolute/candidate/env/bin/python
ARBORA_RUN_NATIVE_HISTORY_ACCEPTANCE=1 \
ARBORA_HISTORY_TUIG_BIN=/absolute/qualified/tuig \
/absolute/candidate/env/bin/python -I -m pytest -p no:cacheprovider -rA \
  --basetemp=/absolute/isolated/evidence --junitxml=native-history-restart.xml \
  tests/qualification/test_native_history_restart.py

Require two passed, zero skipped/failed/errors. Preserve exact package wheel,
source and native binary hashes plus model/effect/process evidence. Backend
Overstory-wire is not browser acceptance. Neither dependency installation nor
this bundle grants compatibility approval or bypasses the publication gate.
