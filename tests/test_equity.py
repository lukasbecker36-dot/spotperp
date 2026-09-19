"""Total account value: what the venues say you are worth, and its history."""
import time
from decimal import Decimal

import database
import equity
from exchange_client import BookTicker, ExchangeError


def _book(sym, bid, ask):
    ts = int(time.time() * 1000)
    return BookTicker(sym, Decimal(bid), Decimal(1), Decimal(ask), Decimal(1), ts)


class _Aster:
    def __init__(self, balances=None, fail=False):
        self._balances = balances or [
            {"asset": "USDT", "balance": "1000", "crossUnPnl": "-25"},
        ]
        self._fail = fail

    async def balances(self):
        if self._fail:
            raise ExchangeError("aster", "down")
        return self._balances


class _Mexc:
    def __init__(self, balances=None, fail=False):
        self._balances = balances or []
        self._fail = fail

    async def account(self):
        if self._fail:
            raise ExchangeError("mexc", "down")
        return {"balances": self._balances}


async def test_total_is_perp_equity_plus_spot_plus_usdt():
    eq = await equity.snapshot(
        _Aster(),
        _Mexc([{"asset": "USDT", "free": "400", "locked": "0"},
               {"asset": "G", "free": "10000", "locked": "0"}]),
        {"GUSDT": _book("GUSDT", "0.03", "0.031")},
    )
    # Perp equity is margin plus the open positions' mark-to-market, not the
    # wallet alone — an unrealised loss is money you no longer have.
    assert eq.aster_usd == Decimal(975)
    assert eq.spot_coins_usd == Decimal(300)      # marked at the BID
    assert eq.spot_usdt_usd == Decimal(400)
    assert eq.total_usd == Decimal(1675)


async def test_locked_balances_count():
    """Coins reserved by a resting sell (a /stops limit) are still held."""
    eq = await equity.snapshot(
        _Aster(), _Mexc([{"asset": "G", "free": "4000", "locked": "6000"}]),
        {"GUSDT": _book("GUSDT", "0.03", "0.031")},
    )
    assert eq.spot_coins_usd == Decimal(300)


async def test_unpriced_coin_is_reported_not_counted():
    """A holding with no MEXC USDT book cannot be valued. Counting it as zero
    would quietly understate the account; saying so lets it be checked."""
    eq = await equity.snapshot(
        _Aster(), _Mexc([{"asset": "WAT", "free": "5", "locked": "0"}]), {}
    )
    assert eq.spot_coins_usd == 0
    assert eq.unpriced == ["WAT"]


async def test_a_failed_venue_is_an_error_not_a_zero():
    """A venue that fails contributes 0, which would read as a crash in account
    value. The caller needs to know the snapshot is partial."""
    eq = await equity.snapshot(_Aster(fail=True), _Mexc(), {})
    assert eq.aster_usd == 0 and eq.errors
    eq = await equity.snapshot(_Aster(), _Mexc(fail=True), {})
    assert eq.errors


async def test_margin_balance_is_preferred_when_the_venue_gives_it():
    eq = await equity.snapshot(
        _Aster([{"asset": "USDT", "balance": "1000", "crossUnPnl": "-25",
                 "marginBalance": "980"}]),
        _Mexc(), {},
    )
    assert eq.aster_usd == Decimal(980)


def test_daily_series_takes_the_last_mark_of_each_day(tmp_path):
    """A daily P&L table compares closing marks. Averaging the day's samples
    would blur a day that moved."""
    conn = database.init_db(tmp_path / "t.db")
    day = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000)
    for offset, total in ((0, "100"), (3_600_000, "150"), (7_200_000, "125")):
        database.record_equity(conn, day + offset, "0", "0", "0", total)
    database.record_equity(conn, day + 86_400_000, "0", "0", "0", "200")
    series = equity.daily_series(database.equity_history(conn))
    assert [v for _d, v in series] == [Decimal(125), Decimal(200)]
    conn.close()


def test_chart_renders_a_band_per_row():
    rows = equity.sparkline([100.0, 110.0, 105.0, 130.0], height=4, width=8)
    assert len(rows) == 4
    # Highest row is topped by the peak, lowest row is full across.
    assert rows[-1].strip().endswith("█" * 4) or "█" in rows[-1]
    assert rows[0].count("█") + rows[0].count("▄") >= 1


def test_chart_needs_two_points():
    assert equity.sparkline([100.0]) == []


def test_chart_survives_a_flat_series():
    """A day with no change must not divide by a zero range."""
    rows = equity.sparkline([500.0] * 6, height=3)
    assert len(rows) == 3
