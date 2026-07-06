"""Tests for the live recon P&L math (recon.py)."""
from decimal import Decimal

import config
import recon


def test_reconstruct_spot_entry_weighted_avg():
    # Two buys covering the holding: 60 @ 100, 40 @ 110 -> avg 104.
    trades = [
        {"isBuyer": True, "qty": "60", "price": "100", "time": 1000},
        {"isBuyer": True, "qty": "40", "price": "110", "time": 2000},
        {"isBuyer": False, "qty": "5", "price": "120", "time": 3000},  # a sell
    ]
    avg, earliest, covered = recon.reconstruct_spot_entry(trades, Decimal(100))
    assert avg == Decimal(104)
    assert covered is True
    assert earliest == 1000  # earliest of the two buys used


def test_reconstruct_uses_most_recent_first():
    # Held qty 40 < total buys: newest buy (40 @ 110) covers it.
    trades = [
        {"isBuyer": True, "qty": "60", "price": "100", "time": 1000},
        {"isBuyer": True, "qty": "40", "price": "110", "time": 2000},
    ]
    avg, earliest, covered = recon.reconstruct_spot_entry(trades, Decimal(40))
    assert avg == Decimal(110)
    assert earliest == 2000
    assert covered is True


def test_reconstruct_uncovered_when_history_short():
    trades = [{"isBuyer": True, "qty": "30", "price": "100", "time": 1000}]
    avg, earliest, covered = recon.reconstruct_spot_entry(trades, Decimal(100))
    assert avg == Decimal(100)
    assert covered is False  # only 30 of 100 visible


def test_reconstruct_no_buys_returns_none():
    assert recon.reconstruct_spot_entry(
        [{"isBuyer": False, "qty": "10", "price": "1", "time": 1}], Decimal(10)
    ) is None


def test_pair_recon_net_pnl(monkeypatch):
    monkeypatch.setattr(config, "ASTER_MAKER_FEE", Decimal("0.0"))
    monkeypatch.setattr(config, "MEXC_TAKER_FEE", Decimal("0.0005"))
    # Short 10 perp @ 100, buy back at 98 -> +20 perp.
    # Long 10 spot @ 99, sell at 99.5 -> +5 spot.
    # Funding +3. Maker fee 0, taker fee 5bps on (99+99.5)*10 = 0.9925.
    p = recon.PairRecon(
        symbol="BTCUSDT", base_asset="BTC",
        perp_qty=Decimal(10), perp_entry=Decimal(100), perp_exit=Decimal(98),
        spot_qty=Decimal(10), spot_entry=Decimal(99), spot_exit=Decimal("99.5"),
        spot_entry_est=False, funding_usd=Decimal(3), held_hours=Decimal(5),
        spot_balance=Decimal(10), perp_base=Decimal(10),
    )
    assert p.perp_pnl == Decimal(20)
    assert p.spot_pnl == Decimal(5)
    assert p.perp_fees == Decimal(0)
    assert p.spot_fees == Decimal("0.9925")
    assert p.net_pnl == Decimal("27.0075")  # 20 + 5 + 3 - 0.9925
    assert p.hedge_imbalance == Decimal(0)


def test_pair_recon_hedge_imbalance():
    p = recon.PairRecon(
        symbol="X", base_asset="X",
        perp_qty=Decimal(10), perp_entry=Decimal(1), perp_exit=Decimal(1),
        spot_qty=Decimal(8), spot_entry=Decimal(1), spot_exit=Decimal(1),
        spot_entry_est=False, funding_usd=Decimal(0), held_hours=None,
        spot_balance=Decimal(8), perp_base=Decimal(10),
    )
    assert p.hedge_imbalance == Decimal(-2)  # naked perp: less spot than perp


def test_format_report_totals(monkeypatch):
    monkeypatch.setattr(config, "ASTER_MAKER_FEE", Decimal("0.0"))
    monkeypatch.setattr(config, "MEXC_TAKER_FEE", Decimal("0.0"))
    p = recon.PairRecon(
        symbol="BTCUSDT", base_asset="BTC",
        perp_qty=Decimal(1), perp_entry=Decimal(100), perp_exit=Decimal(99),
        spot_qty=Decimal(1), spot_entry=Decimal(99), spot_exit=Decimal("99"),
        spot_entry_est=False, funding_usd=Decimal(1), held_hours=Decimal(2),
        spot_balance=Decimal(1), perp_base=Decimal(1),
    )
    out = recon.format_report([p], [])
    assert "BTCUSDT" in out
    assert "NET P&L $+2.00" in out  # +1 perp + 0 spot + 1 funding
    assert "notional" in out        # USD size shown


def _pair(**kw):
    base = dict(
        symbol="BTCUSDT", base_asset="BTC",
        perp_qty=Decimal(1), perp_entry=Decimal(100), perp_exit=Decimal(99),
        spot_qty=Decimal(1), spot_entry=Decimal(99), spot_exit=Decimal(99),
        spot_entry_est=False, funding_usd=Decimal(0), held_hours=Decimal(2),
        spot_balance=Decimal(1), perp_base=Decimal(1),
    )
    base.update(kw)
    return recon.PairRecon(**base)


def test_liq_distance_pct_for_short():
    # Short: liquidation is above the mark. mark 100, liq 150 -> +50% of room.
    p = _pair(perp_mark=Decimal(100), perp_liq=Decimal(150))
    assert p.liq_distance_pct == Decimal(50)


def test_liq_distance_none_without_prices():
    assert _pair(perp_mark=Decimal(100), perp_liq=Decimal(0)).liq_distance_pct is None
    assert _pair(perp_mark=Decimal(0), perp_liq=Decimal(150)).liq_distance_pct is None


def test_format_report_shows_mark_liq_and_warns_when_close(monkeypatch):
    monkeypatch.setattr(config, "ASTER_MAKER_FEE", Decimal("0.0"))
    monkeypatch.setattr(config, "MEXC_TAKER_FEE", Decimal("0.0"))
    safe = recon.format_report([_pair(perp_mark=Decimal(100), perp_liq=Decimal(190))], [])
    assert "liq 190" in safe and "+90.0% to liq" in safe and "⚠️" not in safe
    close = recon.format_report([_pair(perp_mark=Decimal(100), perp_liq=Decimal(108))], [])
    assert "+8.0% to liq" in close and "⚠️" in close  # <15% -> warn


def test_format_report_shows_funding_rates(monkeypatch):
    monkeypatch.setattr(config, "ASTER_MAKER_FEE", Decimal("0.0"))
    monkeypatch.setattr(config, "MEXC_TAKER_FEE", Decimal("0.0"))
    out = recon.format_report(
        [_pair(funding_now_8h_bps=5.2, funding_avg_8h_bps=4.8)], []
    )
    assert "fund rate now +5.2 / 24h +4.8 bps/8h" in out


def test_format_report_liq_na_when_missing(monkeypatch):
    monkeypatch.setattr(config, "ASTER_MAKER_FEE", Decimal("0.0"))
    monkeypatch.setattr(config, "MEXC_TAKER_FEE", Decimal("0.0"))
    out = recon.format_report([_pair(perp_mark=Decimal(100), perp_liq=Decimal(0))], [])
    assert "liq n/a" in out


def test_format_report_empty():
    assert "no matched" in recon.format_report([], [])
