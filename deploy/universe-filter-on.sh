#!/bin/bash
# Turn the options-universe filter ON (settings.yaml options_universe_filter.enabled: true) and restart.
# Preview first: .venv/bin/python scripts/options_universe_preview.py
cd "$(dirname "$0")/.." || exit 1
if grep -q "^options_universe_filter:" config/settings.yaml; then
  sed -i '/^options_universe_filter:/,/^[^ ]/ s/^  enabled:.*/  enabled: true/' config/settings.yaml
else
  printf '\n# Rain 2026-11-11: options-side read filter over the screener'"'"'s list (see src/strategy/universe_filter.py)\noptions_universe_filter:\n  enabled: true\n' >> config/settings.yaml
fi
grep -n -A1 "^options_universe_filter:" config/settings.yaml
exec deploy/restart-trader.sh
