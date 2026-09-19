from decimal import Decimal

import time
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


def _quote_row(symbol, entry, close, depth, volume=1_000_000.0, jitter=0.0,
               samples=10):
    r = screener.ScreenerRow(
        symbol=symbol, entry_bps=entry, close_bps=close,
        spread_cost_bps=entry - close, fees_bps=0.0, funding_8h_bps=0.0,
        net_edge_bps=entry, max_notional_usd=depth, aster_ask="1",
        mexc_ask="1", ts_ms=0,
    )
    r.entry_bps_avg = entry
    r.entry_bps_jitter = jitter
    r.samples = samples
    r.perp_volume_24h = volume
    return r


def test_quote_reject_catches_untransactable_books(monkeypatch):
    """ARGUSUSDT printed a 250bps entry on a $4 book. The tell is not the size
    of the number — it is that the two books are nowhere near each other, so
    neither quote is a price anyone could trade."""
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    wide = _quote_row("ARGUS", 250.0, 60.0, depth=400.0)      # 190bps apart
    thin = _quote_row("PIEVERSE", -4.2, -10.0, depth=1.0)
    assert screener.quote_reject_reason(wide) == "spread"
    assert screener.quote_reject_reason(thin) == "depth"


def test_quote_reject_keeps_a_genuinely_rich_basis(monkeypatch):
    """The gate must not be a magnitude cap: 哈基米 quotes +123 and is real.
    Capping the number would throw away exactly what the screen is for."""
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    rich = _quote_row("HACHIMI", 123.5, 115.0, depth=200.0)
    assert screener.quote_reject_reason(rich) is None


def test_quote_reject_catches_flicker_and_dead_flow(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_MAX_BASIS_JITTER_BPS", 25.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    flicker = _quote_row("FLICKER", 40.0, 35.0, depth=200.0, jitter=60.0)
    quiet = _quote_row("QUIET", 40.0, 35.0, depth=200.0, volume=900.0)
    assert screener.quote_reject_reason(flicker) == "jitter"
    assert screener.quote_reject_reason(quiet) == "volume"


def test_quote_reject_fails_open_on_missing_data(monkeypatch):
    """Two fail-open cases that must not blank the board on a slow start: the
    24h ticker sweep not yet landed (volume 0), and too few samples to judge
    jitter."""
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_MAX_BASIS_JITTER_BPS", 25.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    no_vol = _quote_row("NEWSWEEP", 40.0, 35.0, depth=200.0, volume=0.0)
    one_sample = _quote_row("JUSTSEEN", 40.0, 35.0, depth=200.0,
                            jitter=99.0, samples=1)
    assert screener.quote_reject_reason(no_vol) is None
    assert screener.quote_reject_reason(one_sample) is None


def _carry_row(symbol, entry, lo, jitter=0.0, trades=100_000.0):
    r = _quote_row(symbol, entry, entry, depth=100.0)
    r.entry_bps_avg = entry
    r.basis_p10_24h = lo
    r.entry_bps_jitter = jitter
    r.perp_trades_24h = trades
    return r


def test_carry_score_adds_a_one_off_basis_to_a_funding_stream(monkeypatch):
    """The two halves of a carry trade are in different units. The basis is
    captured ONCE (entry down to the level an exit fills at); funding accrues
    per 8h. They are only comparable over a stated horizon."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "FUNDING_SCORE_HOLD_HOURS", 24.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    r = _carry_row("X", entry=32.6, lo=-47.8, jitter=10.9)
    # one-off (32.6 + 47.8 - 10.9 - 12) = 57.5, stream 11.3 x 3 = 33.9
    assert screener.carry_score(r, 11.3, 15.6) == pytest.approx(91.4)


def test_carry_score_takes_the_lower_of_average_and_latest_funding(monkeypatch):
    """CATEUSDT averaged 61.7bps/8h while its latest settlement was 22.2 — the
    carry had collapsed. Ranking on the average alone kept it top of the board
    on a number that no longer existed. The reverse case (one spiky settlement
    on a modest average) must not flatter a row either."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "FUNDING_SCORE_HOLD_HOURS", 24.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    r = _carry_row("FLAT", entry=12.0, lo=0.0)     # one-off 0, score is all carry
    decaying = screener.carry_score(r, 61.7, 22.2)
    spiking = screener.carry_score(r, 10.6, 34.5)
    assert decaying == pytest.approx(22.2 * 3)
    assert spiking == pytest.approx(10.6 * 3)


def test_carry_score_haircuts_the_basis_but_not_the_funding(monkeypatch):
    """A resting order fills on the bad side of the basis, but funding accrues
    at the same rate whatever price you got in at. Jitter must not scale the
    stream, or a steady book would look like it earned more funding."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "FUNDING_SCORE_HOLD_HOURS", 24.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    steady = _carry_row("STEADY", entry=40.0, lo=0.0, jitter=1.0)
    rough = _carry_row("ROUGH", entry=40.0, lo=0.0, jitter=11.0)
    assert (
        screener.carry_score(steady, 10.0, 10.0)
        - screener.carry_score(rough, 10.0, 10.0)
    ) == pytest.approx(10.0)


def test_carry_score_demotes_a_basis_below_its_own_24h_low(monkeypatch):
    """CATEUSDT quoted +104.5 with a 24h low of +120.9 — the basis had fallen
    out of its range, so there is no convergence left to capture and the range
    is describing a regime that ended. The one-off half must go NEGATIVE."""
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "FUNDING_SCORE_HOLD_HOURS", 24.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    cate = _carry_row("CATE", entry=104.5, lo=120.9, jitter=18.2)
    koma = _carry_row("KOMA", entry=32.6, lo=-47.8, jitter=10.9)
    # Ranked on raw 24h carry CATE (61.7) beat KOMA (11.3) five times over.
    assert screener.carry_score(cate, 61.7, 22.2) < screener.carry_score(
        koma, 11.3, 15.6
    )


def test_carry_score_scales_by_fill_chances_but_not_depth(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "FUNDING_SCORE_HOLD_HOURS", 24.0)
    monkeypatch.setattr(config, "SCREEN_FILL_TARGET_CHANCES", 500.0)
    busy = _carry_row("BUSY", entry=40.0, lo=0.0, trades=5_000.0)
    quiet = _carry_row("QUIET", entry=40.0, lo=0.0, trades=250.0)
    assert screener.carry_score(quiet, 10.0, 10.0) == pytest.approx(
        screener.carry_score(busy, 10.0, 10.0) / 2
    )
    # Depth is a sizing question, not a ranking input — same as fill_score.
    deep = _carry_row("DEEP", entry=40.0, lo=0.0)
    deep.max_notional_usd = 50_000.0
    thin = _carry_row("THIN", entry=40.0, lo=0.0)
    thin.max_notional_usd = 6.0
    assert screener.carry_score(deep, 10.0, 10.0) == screener.carry_score(
        thin, 10.0, 10.0
    )


def test_index_gate_catches_a_pair_the_other_gates_cannot(monkeypatch):
    """ONEUSDT printed a +2674bps entry that had sat above +1500 all day, on a
    tight book with a steady quote. Every other gate tests the two quotes
    against EACH OTHER, so a mis-mapped symbol or a wrong contract multiplier
    passes all of them — a consistently wrong price is still tight and steady.
    Aster's own index is the outside reference that catches it."""
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_MAX_INDEX_DIVERGENCE_BPS", 500.0)
    # Tight spread, deep book, steady: indistinguishable from a good row...
    bad = _quote_row("ONE", 2673.8, 2670.0, depth=500.0)
    assert screener.quote_reject_reason(bad) is None      # ...to the old gates
    bad.index_divergence_bps = -2100.0                    # index says 21% off
    assert screener.quote_reject_reason(bad) == "index"


def test_index_gate_fails_open_without_an_index(monkeypatch):
    """premiumIndex may not carry an index for every contract; a missing one
    must not blank the board."""
    monkeypatch.setattr(config, "SCREEN_MIN_DEPTH_USD", 5.0)
    monkeypatch.setattr(config, "SCREEN_MAX_SPREAD_COST_BPS", 100.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    r = _quote_row("NOINDEX", 40.0, 35.0, depth=200.0)
    assert r.index_divergence_bps is None
    assert screener.quote_reject_reason(r) is None


def test_compute_row_measures_index_divergence_through_the_multiplier():
    """The index is quoted per contract-unit like the book, so it has to be
    divided by qty_multiplier before comparing with MEXC — otherwise every
    1000X-style contract would look mis-mapped by 1000x."""
    pair = PairMap(
        aster_symbol="1000PEPEUSDT", mexc_symbol="PEPEUSDT",
        qty_multiplier=Decimal(1000),
    )
    now = int(time.time() * 1000)
    aster = BookTicker("1000PEPEUSDT", Decimal("10.0"), Decimal(1),
                       Decimal("10.1"), Decimal(1), now)
    mexc = BookTicker("PEPEUSDT", Decimal("0.0100"), Decimal(1),
                      Decimal("0.0101"), Decimal(1), now)
    row = screener.compute_row(
        pair, aster, mexc, None, now_ms=now, index_price=Decimal("10.05")
    )
    assert abs(row.index_divergence_bps) < 1.0      # same asset, same scale
    row = screener.compute_row(
        pair, aster, mexc, None, now_ms=now, index_price=Decimal("12.0")
    )
    assert row.index_divergence_bps > 1800          # ~19% apart -> mis-mapped


def test_fillability_drops_negative_funding(monkeypatch):
    """Shorting the perp is the premium trade and a positive rate means the
    short RECEIVES. A negative rate makes the wait a cost — you pay to hold
    while the basis converges — which is the opposite of the setup this screen
    selects for."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_NET_SWING_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_FUNDING_BPS", 0.0)
    paying = _fill_row("PAYSYOU", 60.0, 20, 1_000_000)
    costing = _fill_row("COSTSYOU", 90.0, 22, 5_000_000)
    paying.funding_8h_bps = 4.0
    costing.funding_8h_bps = -11.0        # a better basis, but time is against you
    assert [r.symbol for r in screener.rank_rows_by_fillability(
        [paying, costing]
    )] == ["PAYSYOU"]


def test_fillability_keeps_zero_funding(monkeypatch):
    """The floor is 'not a cost', not 'must pay' — a flat rate still qualifies,
    and the knob is there to demand more."""
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_VOLUME_USD", 50_000.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_HOURS", 4.0)
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_NET_SWING_BPS", 5.0)
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_FUNDING_BPS", 0.0)
    flat = _fill_row("FLAT", 60.0, 20, 1_000_000)
    flat.funding_8h_bps = 0.0
    assert [r.symbol for r in screener.rank_rows_by_fillability([flat])] == ["FLAT"]
    monkeypatch.setattr(config, "SCREEN_FILL_MIN_FUNDING_BPS", 5.0)
    assert screener.rank_rows_by_fillability([flat]) == []
