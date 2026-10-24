#!/usr/bin/env bash
# Gzip basis and scam-screen logs for every UTC day EXCEPT today's, which the engine is still
# appending to. Gzipping the live file would break the plain-CSV writer, so it
# is always skipped. Safe to run repeatedly (gzip -f overwrites a stale .gz).
#
# Run daily via deploy/basis-trade-logrotate.timer, or by hand:
#     scripts/gzip_old_logs.sh [OUTPUT_DIR]
# The backtest readers (backtest_divergence, backtest_carry, analyze_basis_log)
# read .csv and .csv.gz transparently — glob them with output/basis_log_*.csv*
set -euo pipefail

OUTPUT_DIR="${1:-${OUTPUT_DIR:-/opt/basis-trade/output}}"
day="$(date -u +%Y%m%d)"

shopt -s nullglob
for f in "$OUTPUT_DIR"/basis_log_*.csv "$OUTPUT_DIR"/scam_log_*.csv; do
    case "$(basename "$f")" in
        "basis_log_${day}.csv"|"scam_log_${day}.csv") continue ;;   # live files, leave them
    esac
    gzip -f "$f"
    echo "gzipped $f"
done
