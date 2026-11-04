#!/bin/bash
# Take the XSP index-put sleeve out of dry-run (it starts placing real orders) and restart the trader.
# Usage (from the Claude prompt):  ! deploy/xsp-live.sh
cd "$(dirname "$0")/.." || exit 1
python3 - <<'PY'
import re
p = "config/settings.yaml"
s = open(p).read()
m = re.search(r"^index_puts:\n((?:  .*\n)+)", s, re.M)
if not m:
    raise SystemExit("index_puts: section not found in config/settings.yaml")
block = m.group(1)
block = re.sub(r"^  enabled: .*$", "  enabled: true", block, flags=re.M)
block = re.sub(r"^  dry_run: .*$", "  dry_run: false   # LIVE since $(date)", block, flags=re.M)
s = s[:m.start(1)] + block + s[m.end(1):]
open(p, "w").write(s)
PY
sed -i "s/# LIVE since \$(date)/# LIVE since $(date -u +%Y-%m-%d)/" config/settings.yaml
grep -n -A3 "^index_puts:" config/settings.yaml
exec deploy/restart-trader.sh
