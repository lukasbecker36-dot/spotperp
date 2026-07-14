"""Smoke tests for scripts/backtest_divergence.py simulation logic."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backtest_divergence as bd  # noqa: E402


def _write_log(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "symbol", "entry_bps", "close_bps",
                    "funding_8h_bps", "max_notional_usd"])
        for r in rows:
            w.writerow(r)


def _series(tmp_path):
    """Wide divergence (entry 120 / close 100, spread 20) for 6 samples, then
    converged (entry -30 / close -50) for 6 samples. 60s cadence."""
    rows = []
    ts = 1_000_000_000_000
    for i in range(6):
        rows.append([ts + i * 60_000, "TESTUSDT", "120.00", "100.00", "1.00", "1000"])
    for i in range(6, 12):
        rows.append([ts + i * 60_000, "TESTUSDT", "-30.00", "-50.00", "1.00", "1000"])
    path = tmp_path / "basis_log_test.csv"
    _write_log(path, rows)
    return bd.load_logs([str(path)])["TESTUSDT"]


def test_maker_taker_captures_convergence(tmp_path):
    series = _series(tmp_path)
    model = bd.Model("maker-taker", fees_bps=14.0, mexc_frac=None)
    trades = bd.simulate(
        "TESTUSDT", series, model, threshold=100, exit_bps=0.0,
        confirm=3, min_depth=200, max_hold_hours=48,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.outcome == "converged"
    assert t.entry_edge == 120.0          # logged ask/ask edge
    assert t.exit_edge == -50.0           # logged bid/bid close
    # gross 170 - fees 14 + funding > 0
    assert t.net_bps > 150


def test_taker_taker_pays_both_spreads(tmp_path):
    series = _series(tmp_path)
    model = bd.Model("taker-taker", fees_bps=32.0, mexc_frac=0.5)
    # Taker entry edge = close - 0.5*spread = 100 - 10 = 90 -> below a 100
    # threshold (no trade), above a 50 threshold (trade).
    assert bd.simulate("TESTUSDT", series, model, threshold=100, exit_bps=0.0,
                       confirm=3, min_depth=200, max_hold_hours=48) == []
    trades = bd.simulate("TESTUSDT", series, model, threshold=50, exit_bps=0.0,
                         confirm=3, min_depth=200, max_hold_hours=48)
    assert len(trades) == 1
    t = trades[0]
    assert t.outcome == "converged"
    assert t.entry_edge == 90.0           # close - f*spread
    assert t.exit_edge == -20.0           # entry + f*spread
    # gross 110 - fees 32 + funding: strictly worse than maker-taker
    assert 70 < t.net_bps < 90


def test_depth_filter_blocks_thin_signals(tmp_path):
    rows = []
    ts = 1_000_000_000_000
    for i in range(12):
        rows.append([ts + i * 60_000, "THINUSDT", "200.00", "180.00", "0.00", "50"])
    path = tmp_path / "thin.csv"
    _write_log(path, rows)
    series = bd.load_logs([str(path)])["THINUSDT"]
    model = bd.Model("maker-taker", fees_bps=14.0, mexc_frac=None)
    assert bd.simulate("THINUSDT", series, model, threshold=100, exit_bps=0.0,
                       confirm=3, min_depth=200, max_hold_hours=48) == []


def test_gap_resets_confirmation(tmp_path):
    """A restart gap between qualifying samples must reset the streak — two
    samples before the gap + one after is NOT three consecutive confirmations."""
    ts = 1_000_000_000_000
    rows = [
        [ts, "GAPUSDT", "120.00", "100.00", "0.00", "1000"],
        [ts + 60_000, "GAPUSDT", "120.00", "100.00", "0.00", "1000"],
        [ts + 3_600_000, "GAPUSDT", "120.00", "100.00", "0.00", "1000"],  # 1h gap
        [ts + 3_660_000, "GAPUSDT", "5.00", "0.00", "0.00", "1000"],
    ]
    path = tmp_path / "gap.csv"
    _write_log(path, rows)
    series = bd.load_logs([str(path)])["GAPUSDT"]
    model = bd.Model("maker-taker", fees_bps=14.0, mexc_frac=None)
    assert bd.simulate("GAPUSDT", series, model, threshold=100, exit_bps=0.0,
                       confirm=3, min_depth=200, max_hold_hours=48) == []
