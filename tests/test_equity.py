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
    def __init__(self, balances=None, fail=False, risk=None, risk_fail=False):
        self._balances = balances or [{"asset": "USDT", "balance": "1000"}]
        # Default: one short marked 25 in the red, matching the old fixture's
        # crossUnPnl of -25 but sourced the way the code now reads it.
        self._risk = risk if risk is not None else [
            {"symbol": "GUSDT", "positionAmt": "-1000",
             "unRealizedProfit": "-25"},
        ]
        self._fail = fail
        self._risk_fail = risk_fail

    async def balances(self):
        if self._fail:
            raise ExchangeError("aster", "down")
        return self._balances

    async def position_risk(self):
        if self._risk_fail:
            raise ExchangeError("aster", "down")
        return self._risk


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
    # Perp equity is the wallet plus the open positions' mark-to-market, not
    # the wallet alone — an unrealised loss is money you no longer have.
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
    eq = await equity.snapshot(_Aster(fail=True, risk=[]), _Mexc(), {})
    assert eq.aster_usd == 0 and eq.errors
    eq = await equity.snapshot(_Aster(risk_fail=True), _Mexc(), {})
    assert eq.errors and "positionRisk" in eq.errors[0]
    eq = await equity.snapshot(_Aster(), _Mexc(fail=True), {})
    assert eq.errors


async def test_perp_is_marked_from_position_risk_not_the_balance_endpoint():
    """crossUnPnl/marginBalance are Binance field names read nowhere else in
    this codebase. If Aster omits them the perp leg silently stops marking and
    account value swings by the full spot move with no hedge against it — the
    balance endpoint is ignored for P&L now."""
    eq = await equity.snapshot(
        _Aster(
            balances=[{"asset": "USDT", "balance": "1000",
                       "crossUnPnl": "-999", "marginBalance": "-999"}],
            risk=[{"symbol": "GUSDT", "positionAmt": "-1000",
                   "unRealizedProfit": "-25"}],
        ),
        _Mexc(), {},
    )
    assert eq.aster_upnl_usd == Decimal(-25)      # from positionRisk
    assert eq.aster_usd == Decimal(975)


async def test_perp_upnl_computed_when_the_venue_omits_it():
    """Without unRealizedProfit, (mark - entry) x signed size gets there: for
    a SHORT (negative size) a mark below entry is a gain."""
    eq = await equity.snapshot(
        _Aster(risk=[{"symbol": "GUSDT", "positionAmt": "-1000",
                      "entryPrice": "1.00", "markPrice": "0.98"}]),
        _Mexc(), {},
    )
    assert eq.aster_upnl_usd == Decimal(20)
    eq = await equity.snapshot(
        _Aster(risk=[{"symbol": "GUSDT", "positionAmt": "-1000",
                      "entryPrice": "1.00", "markPrice": "1.03"}]),
        _Mexc(), {},
    )
    assert eq.aster_upnl_usd == Decimal(-30)


async def test_unmarkable_position_is_reported():
    """A position we cannot mark must be said out loud, not counted as flat —
    that is how the perp leg went missing in the first place."""
    eq = await equity.snapshot(
        _Aster(risk=[{"symbol": "GUSDT", "positionAmt": "-1000"}]),
        _Mexc(), {},
    )
    assert eq.errors and "unmarked" in eq.errors[0]


async def test_flat_positions_are_skipped():
    eq = await equity.snapshot(
        _Aster(risk=[{"symbol": "X", "positionAmt": "0",
                      "unRealizedProfit": "-999"}]),
        _Mexc(), {},
    )
    assert eq.aster_upnl_usd == 0 and eq.aster_usd == Decimal(1000)


def test_daily_series_takes_the_last_mark_of_each_day(tmp_path):
    """A daily P&L table compares closing marks. Averaging the day's samples
    would blur a day that moved."""
    conn = database.init_db(tmp_path / "t.db")
    day = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000)
    for offset, total in ((0, "100"), (3_600_000, "150"), (7_200_000, "125")):
        database.record_equity(conn, day + offset, "0", "0", "0", total)
    database.record_equity(conn, day + 86_400_000, "0", "0", "0", "200")
    series = equity.daily_series(database.equity_history(conn))
    assert [m.total for m in series] == [Decimal(125), Decimal(200)]
    # The sample count travels with the mark so a day made of one reading can
    # be flagged: it is the moment sampling started, not a day's close.
    assert series[0].samples == 3 and series[1].samples == 1
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


def test_daily_series_carries_the_components(tmp_path):
    """The total alone cannot say WHY a day moved. A hedged book can change
    purely because the legs are marked differently (Aster mark vs MEXC bid),
    and that shows up as perp and coins moving in opposite directions."""
    conn = database.init_db(tmp_path / "t.db")
    day = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000)
    database.record_equity(conn, day, "600", "300", "100", "1000")
    database.record_equity(conn, day + 86_400_000, "560", "345", "100", "1005")
    a, b = equity.daily_series(database.equity_history(conn))
    assert b.total - a.total == Decimal(5)
    assert b.aster - a.aster == Decimal(-40)        # legs moved against each
    assert b.spot_coins - a.spot_coins == Decimal(45)   # other: a mark shift
    conn.close()
