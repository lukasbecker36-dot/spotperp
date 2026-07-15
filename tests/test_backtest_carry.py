"""Smoke tests for scripts/backtest_carry.py."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backtest_carry as bc  # noqa: E402
import backtest_divergence as bd  # noqa: E402


def _write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "symbol", "entry_bps", "close_bps",
                    "funding_8h_bps", "max_notional_usd"])
        w.writerows(rows)


def test_carry_collects_funding_and_exits_on_decay(tmp_path):
    """Rich funding (30bps/8h) for ~6h, static basis, then funding drops to 0.
    The carry should enter, collect ~funding, exit on decay, net positive."""
    ts = 1_000_000_000_000
    rows = []
    # 8 samples @ 1h apart, funding 30bps/8h, basis flat (entry 20 / close 12).
    for i in range(8):
        rows.append([ts + i * 3_600_000, "CARRYUSDT", "20.0", "12.0", "30.0", "5000"])
    # funding collapses -> exit
    for i in range(8, 11):
        rows.append([ts + i * 3_600_000, "CARRYUSDT", "20.0", "12.0", "0.0", "5000"])
    p = tmp_path / "log.csv"
    _write(p, rows)
    series = bd.load_logs([str(p)])["CARRYUSDT"]

    trades = bc.simulate_carry(
        "CARRYUSDT", series, threshold=10, exit_funding=0.0,
        exit_basis=float("-inf"), confirm=3, min_depth=200,
        max_hold_hours=168, fees_bps=14.0, gap_reset_ms=10_800_000,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.reason == "funding_decay"
    # ~5 hours of funding at 30/8 per hour before decay ~= 18-20bps
    assert 12 < t.funding_bps < 25
    # basis P&L = entry(20) - exit close(12) = +8 (maker earns spread here)
    assert t.basis_pnl == 8.0
    # net = funding + 8 - 14 fees > 0
    assert t.net_bps > 0
    assert t.annualised_bps > 0


def test_carry_max_hold_caps_the_trade(tmp_path):
    ts = 1_000_000_000_000
    rows = [[ts + i * 3_600_000, "LONGUSDT", "10.0", "5.0", "20.0", "3000"]
            for i in range(200)]           # funding never decays
    p = tmp_path / "long.csv"
    _write(p, rows)
    series = bd.load_logs([str(p)])["LONGUSDT"]
    trades = bc.simulate_carry(
        "LONGUSDT", series, threshold=10, exit_funding=0.0,
        exit_basis=float("-inf"), confirm=3, min_depth=200,
        max_hold_hours=24, fees_bps=14.0, gap_reset_ms=10_800_000,
    )
    assert trades and all(t.reason == "max_hold" for t in trades)
    assert all(t.hold_hours <= 25 for t in trades)   # capped near 24h


def test_carry_depth_filter_blocks_thin_names(tmp_path):
    ts = 1_000_000_000_000
    rows = [[ts + i * 3_600_000, "THINUSDT", "50.0", "40.0", "40.0", "100"]
            for i in range(12)]
    p = tmp_path / "thin.csv"
    _write(p, rows)
    series = bd.load_logs([str(p)])["THINUSDT"]
    trades = bc.simulate_carry(
        "THINUSDT", series, threshold=10, exit_funding=0.0,
        exit_basis=float("-inf"), confirm=3, min_depth=1000,
        max_hold_hours=168, fees_bps=14.0, gap_reset_ms=10_800_000,
    )
    assert trades == []


def test_carry_net_formula():
    t = bc.Carry(
        symbol="X", entry_ts=0, exit_ts=3_600_000 * 24,
        basis_in=15.0, basis_out=25.0,    # premium WIDENED 10bps against us
        funding_bps=40.0, fees_bps=14.0, depth_usd=1000, reason="max_hold",
    )
    # net = 40 funding + (15 - 25) basis - 14 fees = 16
    assert t.basis_pnl == -10.0
    assert t.net_bps == 16.0
    assert t.hold_hours == 24.0
    assert round(t.annualised_bps) == round(16.0 * 8760 / 24)
