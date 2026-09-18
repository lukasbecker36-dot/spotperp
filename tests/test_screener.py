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


def test_dislocation_excludes_empty_books(monkeypatch):
    """Only outright-empty books are dropped now: the screen depth floor is
    deliberately tiny, because top-of-book depth is the wrong measure for names
    that are worked over time (STONK shows ~$8 yet fills $42-99 clips)."""
    monkeypatch.setattr(config, "SCREEN_DIFF_MIN_HOURS", 6.0)
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    rows = [
        _row("EMPTY", 5.0, -50.0, depth=0.0),
        _row("THINBUTREAL", 10.0, 5.0, depth=8.0),   # STONK-like, now kept
    ]
    ranked = screener.rank_rows_by_dislocation(rows)
    assert [r.symbol for r in ranked] == ["THINBUTREAL"]


def test_write_snapshot_carries_diff_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCREENER_SNAPSHOT_FILE", tmp_path / "snap.json")
    main = [_row("AAA", 10.0, 1.0)]
    diff = [_row("BBB", 5.0, -50.0)]
    screener.write_snapshot(main, diff)
    snap = screener.read_snapshot()
    assert [r["symbol"] for r in snap["rows"]] == ["AAA"]
    assert [r["symbol"] for r in snap["diff_rows"]] == ["BBB"]
    assert snap["diff_rows"][0]["entry_bps_avg_24h"] == -50.0


def _swing_row(symbol, entry, lo, hi, fund=1.0, hours=24.0, depth=1000.0):
    r = _row(symbol, entry, (lo + hi) / 2, hours=hours, depth=depth)
    r.basis_p10_24h = lo
    r.basis_p90_24h = hi
    r.funding_8h_bps = fund
    return r


def test_swing_ranks_by_round_trip_from_here(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_SWING_EXIT_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_SWING_MIN_FUNDING_BPS", 0.0)
    rows = [
        _swing_row("SMALL", 40.0, 0.0, 45.0),      # capt +40
        _swing_row("STONK", 150.0, -50.0, 160.0),  # capt +200
    ]
    assert [r.symbol for r in screener.rank_rows_by_swing(rows)] == ["STONK", "SMALL"]


def test_swing_excludes_permanently_rich_pairs(monkeypatch):
    """A pair pinned at +80..+120 has a range but never becomes closeable —
    that's carry, not a round trip."""
    monkeypatch.setattr(config, "SCREEN_SWING_EXIT_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_SWING_MIN_FUNDING_BPS", 0.0)
    rows = [
        _swing_row("PINNEDRICH", 120.0, 80.0, 125.0),   # low never reaches 5
        _swing_row("ROUNDTRIP", 60.0, 1.0, 65.0),
    ]
    assert [r.symbol for r in screener.rank_rows_by_swing(rows)] == ["ROUNDTRIP"]


def test_swing_excludes_negative_funding(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_SWING_EXIT_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_SWING_MIN_FUNDING_BPS", 0.0)
    rows = [
        _swing_row("PAYSYOU", 60.0, 1.0, 65.0, fund=2.0),
        _swing_row("COSTSYOU", 90.0, 1.0, 95.0, fund=-3.0),   # bigger capt, but pays
    ]
    assert [r.symbol for r in screener.rank_rows_by_swing(rows)] == ["PAYSYOU"]


def test_hourly_means_average_out_intra_hour_flicker():
    """The BULLA failure mode: a quote swinging +-160bps WITHIN each hour must
    not look like a swing, because each hourly mean lands near the centre."""
    import time as _time
    now = int(_time.time() * 1000)
    flicker = screener.DailyBasis()
    swinger = screener.DailyBasis()
    for h in range(24):
        ts = now - h * 3_600_000
        # flicker: violent within the hour, but centred on 0 every hour
        for k, v in enumerate((-160.0, 160.0, -160.0, 160.0)):
            flicker.add("FLICKER", ts + k * 60_000, v)
        # swinger: calm within the hour, but the LEVEL walks across the day
        swinger.add("SWING", ts, float(h) * 10.0)
    f_lo, f_hi = flicker.percentiles("FLICKER")
    s_lo, s_hi = swinger.percentiles("SWING")
    assert abs(f_hi - f_lo) < 5.0       # noise averages out -> tiny range
    assert (s_hi - s_lo) > 100.0        # real day-scale swing -> wide range


def _fill_row(symbol, entry, hours_tradeable, volume, depth=1000.0, trades=24_000):
    r = _row(symbol, entry, entry, hours=24.0, depth=depth)
    r.hours_tradeable_24h = hours_tradeable
    r.perp_volume_24h = volume
    r.perp_trades_24h = trades
    return r


def test_fillability_ranks_by_expected_taker_events(monkeypatch):
    """Dwell alone misleads. A name workable 23h at ~7 trades/h gives a resting
    order far fewer chances than one workable 19h at ~613 trades/h, even though
    dwell ranks the first higher. Rank by the product."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    rows = [
        _fill_row("LONGBUTQUIET", 23.2, 23, 50_000, trades=176),      # ~169
        _fill_row("SHORTBUTBUSY", 69.6, 19, 1_900_000, trades=14_700),  # ~11638
        _fill_row("MIDDLING", 25.2, 21, 95_000, trades=238),          # ~208
    ]
    ranked = screener.rank_rows_by_fillability(rows)
    assert [r.symbol for r in ranked] == [
        "SHORTBUTBUSY", "MIDDLING", "LONGBUTQUIET",
    ]


def test_fill_chances_is_dwell_times_trades_per_hour():
    r = _fill_row("X", 50.0, 19, 1_900_000, trades=14_700)
    assert screener._fill_chances(r) == pytest.approx(19 * 14_700 / 24)


def test_fillability_excludes_dead_books(monkeypatch):
    """A wide basis on a symbol nobody trades never fills, however good it
    looks — depth alone can't tell you that."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 250_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    rows = [
        _fill_row("NOFLOW", 200.0, 24, 1_000),      # deep book, no trading
        _fill_row("TRADED", 30.0, 10, 900_000),
    ]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["TRADED"]


def test_fillability_excludes_one_tick_spikes(monkeypatch):
    """A basis that was only workable for an hour can't be worked by a resting
    maker order."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 250_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    rows = [
        _fill_row("SPIKE", 300.0, 1, 5_000_000),
        _fill_row("SUSTAINED", 25.0, 12, 500_000),
    ]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["SUSTAINED"]


def test_hours_above_counts_workable_hours():
    import time as _time
    now = int(_time.time() * 1000)
    d = screener.DailyBasis()
    for h in range(24):
        # wide for 18 hours, flat for 6
        d.add("XUSDT", now - h * 3_600_000, 80.0 if h < 18 else 1.0)
    assert d.hours_above("XUSDT", 20.0) == 18
    assert d.hours_above("XUSDT", 200.0) == 0


def test_fillability_requires_current_enterability(monkeypatch):
    """A name that fills well but whose basis is BELOW the entry floor today is
    a watchlist entry, not a candidate — it can't be acted on."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    rows = [
        # huge flow, historically workable, but -2.4bps right now (the 龙虾 case)
        _fill_row("NOTNOW", -2.4, 6, 12_000_000),
        _fill_row("ENTERABLE", 40.0, 5, 200_000),
    ]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["ENTERABLE"]


def _jitter_row(symbol, entry, jitter, samples=20):
    r = _fill_row(symbol, entry, hours_tradeable=20, volume=1_000_000, trades=24_000)
    r.entry_bps_jitter = jitter
    r.samples = samples
    return r


def test_fillability_drops_flickering_basis(monkeypatch):
    """BULLA printed -0.3/+158/-160/+27 inside a minute. A resting order can't
    be worked against that — the fill price is a lottery."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_MAX_BASIS_JITTER_BPS", 25.0)
    rows = [
        _jitter_row("FLICKER", 90.0, jitter=180.0),
        _jitter_row("STEADY", 40.0, jitter=4.0),
    ]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["STEADY"]


def test_jitter_filter_fails_open_without_enough_samples(monkeypatch):
    """With under 3 samples we can't judge — don't hide a name for lack of
    data."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_MAX_BASIS_JITTER_BPS", 25.0)
    rows = [_jitter_row("NEW", 40.0, jitter=999.0, samples=2)]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["NEW"]


def test_jitter_measures_successive_change_not_spread():
    """A smooth drift must NOT be penalised: only tick-to-tick flicker."""
    import time as _time
    now = int(_time.time() * 1000)

    def jitter_of(series):
        rb = screener.RollingBasis(300.0)
        for i, v in enumerate(series):
            rb.add("XUSDT", now - (len(series) - i) * 15_000, v, v)
        row = screener.ScreenerRow(
            symbol="XUSDT", entry_bps=series[-1], close_bps=0.0,
            spread_cost_bps=0.0, fees_bps=0.0, funding_8h_bps=0.0,
            net_edge_bps=0.0, max_notional_usd=0.0, aster_ask="1",
            mexc_ask="1", ts_ms=0,
        )
        rb.annotate(row)
        return row.entry_bps_jitter

    drift = [float(v) for v in range(10, 90, 4)]          # smooth 10 -> 86
    flicker = [150.0 if i % 2 else -150.0 for i in range(20)]
    # The drift spans 76bps — a std would flag it — but each step is ~4bps.
    assert jitter_of(drift) < 10
    # The flicker spans the same kind of range but reverses every sample.
    assert jitter_of(flicker) > 100


def test_fillability_drops_a_round_trip_that_cannot_clear_costs(monkeypatch):
    """ZEREBRO: 44.9 entry but a 24h low of +32.3 — easily fillable, ranked top
    on flow, yet only 12.6bps gross and +0.6 after the round-trip cost. Being
    fillable is not the same as being worth doing."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_NET_SWING_BPS", 5.0)
    rows = [
        _fill_row("ZEREBRO", 44.9, 24, 1_000_000, trades=6_266),
        _fill_row("WORTHIT", 20.2, 7, 129_000, trades=408),
    ]
    rows[0].basis_p10_24h = 32.3     # net (44.9-32.3)-12 = +0.6 -> dropped
    rows[1].basis_p10_24h = 2.8      # net (20.2-2.8)-12  = +5.4 -> kept
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == ["WORTHIT"]


def _score_row(symbol, entry, lo, hours_tradeable, trades, jitter=0.0):
    r = _fill_row(symbol, entry, hours_tradeable, 1_000_000, trades=trades)
    r.basis_p10_24h = lo
    r.entry_bps_jitter = jitter
    return r


def test_fill_score_haircuts_the_quoted_basis_by_its_jitter(monkeypatch):
    """A resting order is adverse-selected, so the quoted basis overstates what
    it locks by roughly how far the basis travels between samples. Two rows with
    identical net edge and identical flow must not score the same when one
    flickers 18bps between samples and the other 1bps."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    steady = _score_row("STEADY", 60.0, 0.0, 20, 24_000, jitter=1.0)
    flicker = _score_row("FLICKER", 60.0, 0.0, 20, 24_000, jitter=18.0)
    assert screener.fill_score(steady) == pytest.approx(48.0 - 1.0)
    assert screener.fill_score(flicker) == pytest.approx(48.0 - 18.0)


def test_fill_score_scales_down_thin_flow_but_saturates(monkeypatch):
    """Below the target a resting order may simply never be lifted, so the edge
    is discounted. Above it, extra flow adds nothing to a SINGLE round trip —
    100x the taker events does not make the trip worth 100x."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    # 20h x (3000/24) = 2500 chances -> 5x the target, still factor 1.0.
    busy = _score_row("BUSY", 60.0, 0.0, 20, 3_000)
    torrent = _score_row("TORRENT", 60.0, 0.0, 20, 300_000)
    thin = _score_row("THIN", 60.0, 0.0, 20, 300)     # 250 chances -> 0.5
    assert screener.fill_score(busy) == pytest.approx(48.0)
    assert screener.fill_score(torrent) == pytest.approx(48.0)
    assert screener.fill_score(thin) == pytest.approx(24.0)


def test_fill_score_ignores_depth(monkeypatch):
    """Top-of-book depth understates exactly the names worth trading here —
    STONK shows ~$8 at the touch yet fills $42-99 clips. Weighting the score by
    depth would re-bury them, which is the bug the depth floor was lowered to
    fix. Size is a separate question ($clip), not a ranking input."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    thin_book = _score_row("STONKLIKE", 60.0, 0.0, 20, 24_000)
    deep_book = _score_row("DEEP", 60.0, 0.0, 20, 24_000)
    thin_book.max_notional_usd = 8.0
    deep_book.max_notional_usd = 50_000.0
    assert screener.fill_score(thin_book) == screener.fill_score(deep_book)


def test_fillability_ranking_prefers_the_rich_steady_name_over_pure_flow(
    monkeypatch,
):
    """The board this replaced ranked on flow alone, which put a +2.9 net name
    with huge volume above a +60 net name that fills a few hundred times a day.
    The composite has to reverse that."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_NET_SWING_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    rows = [
        _score_row("RICH", 90.0, 0.0, 22, 1_200, jitter=3.0),      # 1100 chances
        _score_row("BUSYTHIN", 22.0, 2.0, 24, 600_000, jitter=4.0),
    ]
    assert [r.symbol for r in screener.rank_rows_by_fillability(rows)] == [
        "RICH", "BUSYTHIN",
    ]
