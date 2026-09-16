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
    def row(symbol: str, net: float, depth: float, net_avg: float | None = None):
        return screener.ScreenerRow(
            symbol=symbol, entry_bps=net, close_bps=0, spread_cost_bps=0,
            fees_bps=0, funding_8h_bps=0, net_edge_bps=net,
            max_notional_usd=depth, aster_ask="1", mexc_ask="1", ts_ms=0,
            net_edge_bps_avg=net if net_avg is None else net_avg,
        )

    rows = [row("A", 10, 1e6), row("B", 30, 1e6), row("C", 99, 1.0)]
    ranked = rank_rows(rows)
    assert [r.symbol for r in ranked] == ["B", "A"]  # C dropped: depth too thin


def test_rank_rows_ranks_by_windowed_average_not_live():
    """A one-tick live spike must NOT outrank a persistently higher average."""
    def row(symbol: str, net_live: float, net_avg: float):
        return screener.ScreenerRow(
            symbol=symbol, entry_bps=net_live, close_bps=0, spread_cost_bps=0,
            fees_bps=0, funding_8h_bps=0, net_edge_bps=net_live,
            max_notional_usd=1e6, aster_ask="1", mexc_ask="1", ts_ms=0,
            net_edge_bps_avg=net_avg,
        )
    # SPIKE looks best live (80) but averages 5; STEADY averages 30.
    ranked = rank_rows([row("SPIKE", 80, 5), row("STEADY", 25, 30)])
    assert [r.symbol for r in ranked] == ["STEADY", "SPIKE"]


def test_rolling_basis_window_mean_and_pruning():
    rb = screener.RollingBasis(window_s=300)  # 5 min
    pair_row = lambda e, n: screener.ScreenerRow(
        symbol="X", entry_bps=e, close_bps=0, spread_cost_bps=0, fees_bps=0,
        funding_8h_bps=0, net_edge_bps=n, max_notional_usd=0, aster_ask="1",
        mexc_ask="1", ts_ms=0,
    )
    # First sample with no history -> avg equals the live value, 1 sample.
    rb.add("X", 0, 40.0, 20.0)
    r = rb.annotate(pair_row(40.0, 20.0))
    assert r.entry_bps_avg == 40.0 and r.net_edge_bps_avg == 20.0
    assert r.samples == 1

    # A later spike: the mean sits between the two, not at the spike.
    rb.add("X", 15_000, 100.0, 80.0)  # +15s
    r = rb.annotate(pair_row(100.0, 80.0))
    assert r.samples == 2
    assert r.entry_bps_avg == pytest.approx(70.0)   # (40+100)/2
    assert r.net_edge_bps_avg == pytest.approx(50.0)  # (20+80)/2
    assert r.window_s == pytest.approx(15.0)

    # A sample past the window drops the oldest (time-pruned).
    rb.add("X", 400_000, 10.0, 5.0)   # >300s after t=0 and t=15s
    r = rb.annotate(pair_row(10.0, 5.0))
    assert r.samples == 1             # only the newest remains
    assert r.net_edge_bps_avg == pytest.approx(5.0)


def _daily_now_ms():
    import time
    return int(time.time() * 1000)


def test_daily_basis_means_over_window():
    d = screener.DailyBasis()
    now = _daily_now_ms()
    for h in range(24):
        d.add("AAAUSDT", now - h * 3_600_000, 10.0)
    mean, hours = d.stats("AAAUSDT")
    assert mean == pytest.approx(10.0)
    assert hours == 24


def test_daily_basis_prunes_beyond_window_out_of_order():
    """The log seed replays historical rows, so an out-of-order (older) add
    must not widen the window past 24h."""
    d = screener.DailyBasis()
    now = _daily_now_ms()
    d.add("BBBUSDT", now - 30 * 3_600_000, 999.0)   # ancient, added first
    for h in range(24):
        d.add("BBBUSDT", now - h * 3_600_000, 5.0)
    mean, hours = d.stats("BBBUSDT")
    assert hours <= 24
    assert mean == pytest.approx(5.0)               # 999 dropped


def test_daily_basis_unknown_symbol_is_none():
    d = screener.DailyBasis()
    assert d.stats("NOPEUSDT") == (None, 0.0)


def test_daily_basis_annotate_falls_back_to_live():
    d = screener.DailyBasis()
    row = screener.ScreenerRow(
        symbol="ZZZUSDT", entry_bps=42.0, close_bps=0.0, spread_cost_bps=0.0,
        fees_bps=0.0, funding_8h_bps=0.0, net_edge_bps=0.0,
        max_notional_usd=0.0, aster_ask="1", mexc_ask="1", ts_ms=0,
    )
    d.annotate(row)
    assert row.entry_bps_avg_24h == 42.0    # no history -> live value
    assert row.hours_24h == 0.0


def test_seed_daily_from_logs_reads_recent_rows(tmp_path, monkeypatch):
    """Seeding warms the window from the engine's own basis logs so a restart
    doesn't reset it; rows older than the window are skipped."""
    import csv as _csv
    import time as _time
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    now = _daily_now_ms()
    day = _time.strftime("%Y%m%d", _time.gmtime(now / 1000))
    path = tmp_path / f"basis_log_{day}.csv"
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["ts_ms", "symbol", "entry_bps", "close_bps",
                    "funding_8h_bps", "max_notional_usd"])
        w.writerow([now - 2 * 3_600_000, "SEEDUSDT", "20.0", "1", "0", "100"])
        w.writerow([now - 1 * 3_600_000, "SEEDUSDT", "30.0", "1", "0", "100"])
        w.writerow([now - 48 * 3_600_000, "SEEDUSDT", "999.0", "1", "0", "100"])  # too old

    d = screener.DailyBasis()
    seeded = screener.seed_daily_from_logs(d, now)
    assert seeded == 2                      # the 48h-old row is skipped
    mean, hours = d.stats("SEEDUSDT")
    assert mean == pytest.approx(25.0)      # (20 + 30) / 2


def _row(symbol, entry_avg, avg24, hours=24.0, depth=1000.0, net_avg=10.0):
    r = screener.ScreenerRow(
        symbol=symbol, entry_bps=entry_avg, close_bps=0.0, spread_cost_bps=0.0,
        fees_bps=0.0, funding_8h_bps=0.0, net_edge_bps=net_avg,
        max_notional_usd=depth, aster_ask="1", mexc_ask="1", ts_ms=0,
    )
    r.entry_bps_avg = entry_avg
    r.net_edge_bps_avg = net_avg
    r.entry_bps_avg_24h = avg24
    r.hours_24h = hours
    return r


def test_dislocation_ranks_by_gap_not_absolute_basis(monkeypatch):
    """A +5 basis on a pair that normally sits at -50 outranks a +51 basis on a
    pair that always sits at +50 — the gap is the opportunity, not the level."""
    monkeypatch.setattr(config, "SCREEN_DIFF_MIN_HOURS", 6.0)
    rows = [
        _row("ALWAYSRICH", 51.0, 50.4),      # gap +0.6
        _row("REVERT", 5.0, -50.0),          # gap +55
        _row("MILD", 40.0, 10.0),            # gap +30
    ]
    ranked = screener.rank_rows_by_dislocation(rows)
    assert [r.symbol for r in ranked] == ["REVERT", "MILD", "ALWAYSRICH"]


def test_dislocation_excludes_thin_24h_history(monkeypatch):
    """A gap measured against a few minutes of history is noise."""
    monkeypatch.setattr(config, "SCREEN_DIFF_MIN_HOURS", 6.0)
    rows = [
        _row("FRESH", 5.0, -50.0, hours=1.0),    # huge gap, but no history
        _row("SEASONED", 10.0, 5.0, hours=12.0),
    ]
    ranked = screener.rank_rows_by_dislocation(rows)
    assert [r.symbol for r in ranked] == ["SEASONED"]


def test_dislocation_excludes_thin_depth(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_DIFF_MIN_HOURS", 6.0)
    monkeypatch.setattr(config, "MIN_DEPTH_NOTIONAL_USD", Decimal("200"))
    rows = [
        _row("THIN", 5.0, -50.0, depth=10.0),
        _row("DEEP", 10.0, 5.0, depth=1000.0),
    ]
    ranked = screener.rank_rows_by_dislocation(rows)
    assert [r.symbol for r in ranked] == ["DEEP"]


def test_write_snapshot_carries_diff_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCREENER_SNAPSHOT_FILE", tmp_path / "snap.json")
    main = [_row("AAA", 10.0, 1.0)]
    diff = [_row("BBB", 5.0, -50.0)]
    screener.write_snapshot(main, diff)
    snap = screener.read_snapshot()
    assert [r["symbol"] for r in snap["rows"]] == ["AAA"]
    assert [r["symbol"] for r in snap["diff_rows"]] == ["BBB"]
    assert snap["diff_rows"][0]["entry_bps_avg_24h"] == -50.0
