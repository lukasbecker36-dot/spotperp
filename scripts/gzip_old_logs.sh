#!/usr/bin/env bash
# Gzip basis logs for every UTC day EXCEPT today's, which the engine is still
# appending to. Gzipping the live file would break the plain-CSV writer, so it
# is always skipped. Safe to run repeatedly (gzip -f overwrites a stale .gz).
#
# Run daily via deploy/basis-trade-logrotate.timer, or by hand:
#     scripts/gzip_old_logs.sh [OUTPUT_DIR]
# The backtest readers (backtest_divergence, backtest_carry, analyze_basis_log)
# read .csv and .csv.gz transparently — glob them with output/basis_log_*.csv*
set -euo pipefail

OUTPUT_DIR="${1:-${OUTPUT_DIR:-/opt/basis-trade/output}}"
today="basis_log_$(date -u +%Y%m%d).csv"

shopt -s nullglob
for f in "$OUTPUT_DIR"/basis_log_*.csv; do
    [ "$(basename "$f")" = "$today" ] && continue   # live file, leave it
    gzip -f "$f"
    echo "gzipped $f"
done
