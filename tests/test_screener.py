from decimal import Decimal

import pytest

import config
import screener
from exchange_client import BookTicker
from screener import PairMap, build_pair_maps, compute_row, rank_rows


def book(symbol: str, bid: str, ask: str, qty: str = "100", ts: int = 1000) -> BookTicker:
    return BookTicker(
        symbol=symbol,
        bid=Decimal(bid),
        bid_qty=Decimal(qty),
        ask=Decimal(ask),
        ask_qty=Decimal(qty),
        ts_ms=ts,
    )


def test_build_pair_maps_intersects_usdt_pairs():
    maps = build_pair_maps(
        {"BTCUSDT", "ETHUSDT", "SOLUSDC", "ONLYASTERUSDT"},
        {"BTCUSDT", "ETHUSDT", "ONLYMEXCUSDT"},
    )
    assert set(maps) == {"BTCUSDT", "ETHUSDT"}
    assert maps["BTCUSDT"].qty_multiplier == 1


def test_build_pair_maps_applies_alias(monkeypatch):
    monkeypatch.setitem(
        screener.ASTER_TO_MEXC_ALIASES,
        "1000PEPEUSDT",
        ("PEPEUSDT", Decimal(1000)),
    )
    maps = build_pair_maps({"1000PEPEUSDT"}, {"PEPEUSDT"})
    assert maps["1000PEPEUSDT"].mexc_symbol == "PEPEUSDT"
    assert maps["1000PEPEUSDT"].qty_multiplier == 1000


def test_compute_row_basis_math():
    pair = PairMap("BTCUSDT", "BTCUSDT", Decimal(1))
    # perp ask 100.50 vs spot ask 100.00 -> entry basis 50bps
    aster = book("BTCUSDT", "100.40", "100.50")
    mexc = book("BTCUSDT", "99.90", "100.00")
    row = compute_row(pair, aster, mexc, Decimal("0.0001"), now_ms=1000)
    assert row is not None
    assert row.entry_bps == pytest.approx(50.0, abs=0.01)
    # close basis: (100.40 - 99.90) / 99.90 ~ 50.05bps
    assert row.close_bps == pytest.approx(50.05, abs=0.01)
    assert row.funding_8h_bps == pytest.approx(1.0)
    # net = entry - exit target - fees - slippage buffer
    fees = float((config.ENTRY_FEE + config.EXIT_FEE_PASSIVE) * 10000)
    expected_net = 50.0 - float(config.EXIT_BASIS_BPS) - fees - float(
        config.SLIPPAGE_BUFFER_BPS
    )
    assert row.net_edge_bps == pytest.approx(expected_net, abs=0.01)


def test_compute_row_normalises_alias_multiplier():
    pair = PairMap("1000PEPEUSDT", "PEPEUSDT", Decimal(1000))
    aster = book("1000PEPEUSDT", "10.04", "10.05")  # 1000x the spot price
    mexc = book("PEPEUSDT", "0.0100", "0.010")
    row = compute_row(pair, aster, mexc, None, now_ms=1000)
    assert row is not None
    # 10.05/1000 = 0.01005 vs 0.010 -> +50bps
    assert row.entry_bps == pytest.approx(50.0, abs=0.5)


def test_compute_row_rejects_stale_quotes():
    pair = PairMap("BTCUSDT", "BTCUSDT", Decimal(1))
    stale = book("BTCUSDT", "100", "100.1", ts=0)
    fresh = book("BTCUSDT", "100", "100.1", ts=99_000)
    assert compute_row(pair, stale, fresh, None, now_ms=100_000) is None


def test_rank_rows_filters_depth_and_sorts():
    def row(symbol: str, net: float, depth: float):
        return screener.ScreenerRow(
            symbol=symbol, entry_bps=net, close_bps=0, spread_cost_bps=0,
            fees_bps=0, funding_8h_bps=0, net_edge_bps=net,
            max_notional_usd=depth, aster_ask="1", mexc_ask="1", ts_ms=0,
        )

    rows = [row("A", 10, 1e6), row("B", 30, 1e6), row("C", 99, 1.0)]
    ranked = rank_rows(rows)
    assert [r.symbol for r in ranked] == ["B", "A"]  # C dropped: depth too thin
