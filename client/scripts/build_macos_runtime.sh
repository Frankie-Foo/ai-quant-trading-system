#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf build/runtime build/runtime-work build/macos-research-runtime.spec
mkdir -p build/runtime

python -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name macos-research-runtime \
  --paths .. \
  --distpath build/runtime \
  --workpath build/runtime-work \
  --specpath build \
  --collect-data pandas_market_calendars \
  --copy-metadata pandas-market-calendars \
  --add-data "../config.yaml:." \
  --add-data "../config:config" \
  --hidden-import data_plane.cli \
  --hidden-import scripts.build_catalyst_snapshot \
  --hidden-import scripts.build_cross_asset_sentiment_snapshot \
  --hidden-import scripts.build_daily_universe \
  --hidden-import scripts.build_factor_candidates \
  --hidden-import scripts.build_order_flow_snapshot \
  --hidden-import scripts.build_orb5_signals \
  --hidden-import scripts.build_postmarket_episode \
  --hidden-import scripts.build_premarket_rvol \
  --hidden-import scripts.build_selection_gates \
  --hidden-import scripts.build_unified_shadow_selection \
  --hidden-import scripts.review_postmarket_episode \
  --hidden-import scripts.run_multisignal_shadow_pipeline \
  --hidden-import scripts.run_postclose_missed_movers_review \
  --hidden-import scripts.run_structured_pdca \
  ../scripts/macos_research_entry.py

chmod 755 build/runtime/macos-research-runtime
