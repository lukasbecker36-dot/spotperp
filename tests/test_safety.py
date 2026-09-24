"""Safety-stop and position-reference tests for the engine loop.

Builds an Engine shell (no network) around the paper executor: open a
position through the normal paper entry path, then drive the books and
call _check_safety directly.
"""
import asyncio
from decimal import Decimal

import pytest

import config
import database
import live_monitor
import position_manager as pm
import screener
from exchange_client import BookTicker, SymbolInfo
from executor import Executor, MarketData, PaperTrader
from screener import PairMap


class DummyNotifier:
    def __init__(self):
        self.messages = []

    async def alert(self, message: str) -> None:
        self.messages.append(message)


def info(symbol: str) -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol, base_asset="BTC", quote_asset="USDT",
        tick_size=Decimal("0.1"), step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def set_books(md: MarketData, aster_bid: str, aster_ask: str,
              mexc_bid: str, mexc_ask: str) -> None:
    import time
    ts = int(time.time() * 1000)
    md.aster_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(aster_bid), Decimal(100), Decimal(aster_ask),
        Decimal(100), ts)
    md.mexc_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(mexc_bid), Decimal(100), Decimal(mexc_ask),
        Decimal(100), ts)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(config, "REPRICE_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "EXIT_REPRICE_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal("1"))
    # Convergence tests open a position and check the auto-close in the same
    # instant; default the min-hold off so they behave as before (its own test
    # sets it explicitly).
    monkeypatch.setattr(config, "CONVERGENCE_MIN_HOLD_MINUTES", 0.0)

    conn = database.init_db(tmp_path / "test.db")
    md = MarketData()
    md.pair_maps["BTCUSDT"] = PairMap("BTCUSDT", "BTCUSDT", Decimal(1))
    md.aster_info["BTCUSDT"] = info("BTCUSDT")
    md.mexc_info["BTCUSDT"] = info("BTCUSDT")
    positions = pm.PositionManager(conn)
    notifier = DummyNotifier()
    executor = Executor(
        md, PaperTrader(md), positions, notifier, conn, paper=True
    )
    eng = live_monitor.Engine.__new__(live_monitor.Engine)
    eng.paper = True
    eng.conn = conn
    eng.md = md
    eng.positions = positions
    eng.notifier = notifier
    eng.executor = executor
    eng._auto_passive = set()
    eng._position_risk = {}
    eng._liq_alerted = {}
    eng._stops_qty = {}
    eng._auto_stops_attempt = {}
    eng._tp_confirm = {}
    eng._liq_protect = set()
    eng._spot_check_at = {}
    eng._spot_deficit_since = {}
    eng._stops_orders = {}
    eng._stop_grace = {}
    eng._hedge_break = {}
    eng._position_risk_ts = 0.0
    eng._position_risk_wall_ms = 0
    eng._basis_24h = screener.DailyBasis()

    class _NoOrders:
        """Venue stub for command handlers that list resting orders."""
        async def open_orders(self, symbol):
            return []

    eng.aster = _NoOrders()
    eng.mexc = _NoOrders()
    yield eng
    conn.close()


async def open_position(eng, trade_kind: str = "convergence") -> int:
    """+50bps entry: perp ask 100.5 vs spot ask 100.0, paper instant fill."""
    set_books(eng.md, "100.4", "100.5", "99.9", "100.0")
    pos = eng.positions.create(
        "BTCUSDT", Decimal(1000), paper=True, trade_kind=trade_kind
    )
    eng.executor.start_entry(pos)
    for _ in range(200):
        if eng.positions.get(pos.id).state == pm.OPEN:
            return pos.id
        await asyncio.sleep(0.02)
    raise AssertionError("entry never opened")


async def test_adverse_stop_disabled_by_default(engine):
    """Off by default: even a huge adverse widen must NOT force-close."""
    pos_id = await open_position(engine)
    assert config.ADVERSE_WIDEN_STOP_BPS is None
    set_books(engine.md, "101.9", "102.0", "99.9", "100.0")  # widened ~+150bps
    await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode is None


async def test_adverse_widen_stop_fires_when_enabled(engine, monkeypatch):
    monkeypatch.setattr(config, "ADVERSE_WIDEN_STOP_BPS", Decimal("100"))
    pos_id = await open_position(engine)
    # close basis = (aster_bid - mexc_bid)/mexc_bid; entry was ~50bps.
    # Widen well past entry + 100bps: close ~200bps.
    set_books(engine.md, "101.9", "102.0", "99.9", "100.0")
    await engine._check_safety(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode == "now"
    assert any("adverse stop" in m for m in engine.notifier.messages)


async def test_no_stop_inside_widen_band(engine, monkeypatch):
    monkeypatch.setattr(config, "ADVERSE_WIDEN_STOP_BPS", Decimal("100"))
    pos_id = await open_position(engine)
    # close ~100bps, entry ~50bps: widened 50 < 100 threshold -> no action
    set_books(engine.md, "100.9", "101.0", "99.9", "100.0")
    await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode is None
    assert engine.notifier.messages[-1].startswith("✅")  # entry-open alert only


async def test_converged_tp_fires_when_profitable(engine):
    pos_id = await open_position(engine)
    # Basis inverted: aster 99.0/99.1 vs mexc 99.9/100.0 -> close ~-90bps.
    # Taker close: buy perp at 99.1 (entry 100.5), sell spot 99.9 (entry
    # 100.0) -> clearly net positive.
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")
    # Needs CONVERGED_TP_CONFIRM_TICKS consecutive sweeps: one flickering quote
    # must not cross both legs (BULLA #170).
    for _ in range(config.CONVERGED_TP_CONFIRM_TICKS - 1):
        await engine._check_safety(engine.positions.get(pos_id))
        assert engine.positions.get(pos_id).exit_mode is None   # still confirming
    await engine._check_safety(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode == "now"
    assert any("taking profit" in m for m in engine.notifier.messages)


async def test_converged_tp_not_fired_by_a_single_flicker(engine):
    """A one-tick inverted quote surrounded by normal ones must NOT cross: the
    BULLA case fired on a -160bps print and filled at +27bps for a real loss."""
    pos_id = await open_position(engine)
    for _ in range(5):
        set_books(engine.md, "99.0", "99.1", "99.9", "100.0")   # flicker
        await engine._check_safety(engine.positions.get(pos_id))
        set_books(engine.md, "100.4", "100.5", "99.9", "100.0")  # back to premium
        await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode is None
    assert not any("taking profit" in m for m in engine.notifier.messages)


async def test_converged_tp_waits_for_min_hold(engine, monkeypatch):
    """The convergence auto-close must not fire within the min-hold — right
    after entry a converged+profitable-looking basis is a spread artifact
    (CASHCAT). It fires once the position has been held long enough."""
    monkeypatch.setattr(config, "CONVERGENCE_MIN_HOLD_MINUTES", 15.0)
    pos_id = await open_position(engine)         # opened just now
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")  # converged + profitable
    await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode is None   # too soon — held

    # Backdate the open past the min-hold: now the same basis closes it.
    engine.conn.execute(
        "UPDATE positions SET opened_ms=? WHERE id=?",
        (int(_time.time() * 1000) - 20 * 60_000, pos_id),
    )
    engine.conn.commit()
    for _ in range(config.CONVERGED_TP_CONFIRM_TICKS):
        await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode == "now"


async def test_carry_trade_skips_converged_tp(engine):
    """A carry trade holds for funding: the same converged + profitable
    basis that auto-closes a convergence trade must NOT close a carry one."""
    pos_id = await open_position(engine, trade_kind="carry")
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")  # converged, profitable
    await engine._check_safety(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode is None
    assert pos.state == pm.OPEN


async def test_carry_trade_hits_adverse_stop_only_when_enabled(engine, monkeypatch):
    """When re-enabled, the adverse-widen stop applies to carry too."""
    monkeypatch.setattr(config, "ADVERSE_WIDEN_STOP_BPS", Decimal("100"))
    pos_id = await open_position(engine, trade_kind="carry")
    set_books(engine.md, "101.9", "102.0", "99.9", "100.0")  # widened ~+150bps
    await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode == "now"
    assert any("adverse stop" in m for m in engine.notifier.messages)


async def test_safety_skips_on_stale_book(engine):
    """A frozen book (old ts) must not trigger any auto-close."""
    import time as _t
    pos_id = await open_position(engine)
    # Deeply converged + profitable, but quotes are 30s old (> QUOTE_STALE).
    old = int(_t.time() * 1000) - 30_000
    from exchange_client import BookTicker
    engine.md.aster_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal("99.0"), Decimal(100), Decimal("99.1"), Decimal(100), old)
    engine.md.mexc_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal("99.9"), Decimal(100), Decimal("100.0"), Decimal(100), old)
    await engine._check_safety(engine.positions.get(pos_id))
    assert engine.positions.get(pos_id).exit_mode is None   # nothing fired


async def test_converged_starts_passive_when_taker_unprofitable(engine):
    """Converged (maker-taker basis <= 0) but a taker-taker close would lose
    (wide Aster ask, the ETHFI case): don't cross at a loss — work it passively
    at the convergence target instead."""
    pos_id = await open_position(engine)
    # close basis ~-90bps but the ask is still 101.5: a taker buy-back at the
    # ask loses 1.0/unit on the perp leg -> aggressive est pnl negative.
    set_books(engine.md, "99.0", "101.5", "99.9", "100.0")
    await engine._check_safety(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode == "passive"
    assert pos.exit_target_bps == config.CONVERGED_PASSIVE_BPS
    assert pos_id in engine._auto_passive
    assert any("passive maker close" in m for m in engine.notifier.messages)


async def test_auto_passive_escalates_when_taker_turns_profitable(engine):
    """A convergence-auto passive exit crosses to a taker close once the basis
    runs negative enough that taker-taker is net positive."""
    pos_id = await open_position(engine)
    engine.positions.set_exit_request(pos_id, "passive", config.CONVERGED_PASSIVE_BPS)
    engine.positions.set_state(pos_id, pm.EXITING)
    engine._auto_passive.add(pos_id)
    # Tight book, deeply inverted: taker buy-back at 99.1 (entry 100.5) wins.
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")
    for _ in range(config.CONVERGED_TP_CONFIRM_TICKS):
        await engine._check_auto_passive(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode == "now"
    assert pos_id not in engine._auto_passive
    assert any("crossing to lock" in m for m in engine.notifier.messages)


async def test_auto_passive_resets_to_open_on_basis_recovery(engine):
    """If the basis recovers back into premium, stand the passive close down and
    return the position to OPEN (keeps collecting funding, max-hold re-armed)."""
    pos_id = await open_position(engine)
    engine.positions.set_exit_request(pos_id, "passive", config.CONVERGED_PASSIVE_BPS)
    engine.positions.set_state(pos_id, pm.EXITING)
    engine._auto_passive.add(pos_id)
    # Basis back to ~+50bps, well above the reset band.
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    await engine._check_auto_passive(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.state == pm.OPEN
    assert pos.exit_mode is None
    assert pos_id not in engine._auto_passive
    assert any("stood down" in m for m in engine.notifier.messages)


async def test_funding_snapshot_survives_zero_priced_book(engine, monkeypatch, tmp_path):
    """Regression: compute_row returns None on a stale/zero book (thin microcap);
    the funding snapshot must skip it, not crash annotate(None) — which froze the
    funding file and heartbeat for days.

    The row is now HIDDEN rather than listed with a blank basis: a carry you
    have no price for is not a candidate. It still has to be counted, so
    /funding can say why the board is shorter than the funding universe."""
    import funding as funding_mod
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "fnd.json")
    engine._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
    # A funding stat for BTCUSDT so the row is built, but a zero-ask book so
    # compute_row returns None.
    engine.md.funding_stats["BTCUSDT"] = funding_mod.summarize(
        "BTCUSDT", [], None, now_ms=0
    )
    set_books(engine.md, "100.4", "100.5", "99.9", "0")   # MEXC ask = 0 -> None row
    engine._write_funding_snapshot()                       # must not raise
    import json
    snap = json.loads((tmp_path / "fnd.json").read_text())
    assert snap["rows"] == []                              # no price -> not a row
    assert snap["hidden"] == {"book": 1}                   # ...but accounted for


async def test_slow_scan_isolates_write_failures(engine, monkeypatch, tmp_path):
    """A throw in one snapshot write must NOT block the others — in particular
    the heartbeat must keep writing (regression: a broken funding snapshot
    froze the heartbeat and funding file for days while /screen stayed fresh)."""
    monkeypatch.setattr(config, "HEARTBEAT_FILE", tmp_path / "hb.json")
    monkeypatch.setattr(config, "SCREENER_SNAPSHOT_FILE", tmp_path / "scr.json")
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "fnd.json")
    engine._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
    engine._last_basis_log = 0.0

    async def noop():
        return None
    monkeypatch.setattr(engine, "_refresh_funding", noop)

    def boom():
        raise RuntimeError("funding snapshot kaboom")
    monkeypatch.setattr(engine, "_write_funding_snapshot", boom)

    await engine._slow_scan()   # must not raise

    assert (tmp_path / "hb.json").exists()    # heartbeat written despite failure
    assert (tmp_path / "scr.json").exists()   # screener too


async def test_position_marks_full_when_book_live(engine):
    pid = await open_position(engine)
    marks = engine._position_marks()
    assert "upnl_usd" in marks[str(pid)]      # full mark, not a skip


async def test_position_marks_include_liq_distance(engine):
    pid = await open_position(engine)
    # Cache a positionRisk row: mark 100, liq 150 -> +50% room for the short.
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "150"}
    }
    m = engine._position_marks()[str(pid)]
    assert m["liq_price"] == 150.0
    assert m["liq_dist_pct"] == pytest.approx(50.0)


async def _make_live_open(engine):
    """Open a position, then flip it to live so liq checks apply."""
    pid = await open_position(engine)
    engine.conn.execute("UPDATE positions SET paper=0 WHERE id=?", (pid,))
    engine.conn.commit()
    return pid


async def test_liq_alert_fires_when_close(engine):
    pid = await _make_live_open(engine)
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "108"}
    }  # +8% -> under the 15% threshold
    await engine._check_liquidation(engine.positions.get(pid))
    assert any("LIQUIDATION" in m for m in engine.notifier.messages)
    assert pid in engine._liq_alerted


async def test_liq_alert_throttled_then_rearms(engine):
    pid = await _make_live_open(engine)
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "108"}
    }
    await engine._check_liquidation(engine.positions.get(pid))
    await engine._check_liquidation(engine.positions.get(pid))
    assert sum("LIQUIDATION" in m for m in engine.notifier.messages) == 1  # throttled

    # Recover above threshold -> re-arm; next danger alerts again.
    engine._position_risk["BTCUSDT"]["liquidationPrice"] = "200"  # +100%
    await engine._check_liquidation(engine.positions.get(pid))
    assert pid not in engine._liq_alerted
    engine._position_risk["BTCUSDT"]["liquidationPrice"] = "108"  # danger again
    await engine._check_liquidation(engine.positions.get(pid))
    assert sum("LIQUIDATION" in m for m in engine.notifier.messages) == 2


async def test_liq_alert_silent_when_safe(engine):
    pid = await _make_live_open(engine)
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "200"}
    }  # +100% -> safe
    await engine._check_liquidation(engine.positions.get(pid))
    assert not any("LIQUIDATION" in m for m in engine.notifier.messages)


async def test_liq_alert_skips_paper(engine):
    pid = await open_position(engine)   # stays paper
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "101"}
    }
    await engine._check_liquidation(engine.positions.get(pid))
    assert not any("LIQUIDATION" in m for m in engine.notifier.messages)


async def test_position_marks_omit_liq_without_risk(engine):
    pid = await open_position(engine)
    engine._position_risk = {}                # no cached risk (e.g. paper / not fetched)
    m = engine._position_marks()[str(pid)]
    assert "upnl_usd" in m and "liq_dist_pct" not in m


async def test_position_marks_skips_with_reason_no_bid(engine):
    """A thin book (no bid on a venue) yields an explanatory skip, not silence."""
    pid = await open_position(engine)
    set_books(engine.md, "100.4", "100.5", "0", "100.0")  # MEXC bid = 0
    m = engine._position_marks()[str(pid)]
    assert "skip" in m and "MEXC spot bid" in m["skip"]


async def test_position_marks_skips_when_symbol_not_in_universe(engine):
    pid = await open_position(engine)
    engine.md.pair_maps.pop("BTCUSDT")        # e.g. delisted / stale universe
    m = engine._position_marks()[str(pid)]
    assert "skip" in m and "cross-listed" in m["skip"]


async def test_remove_marks_position_closed_no_trading(engine):
    pid = await open_position(engine)
    out = await engine._cmd_remove({"position_id": "BTC"})
    assert "removed" in out.lower()
    pos = engine.positions.get(pid)
    assert pos.state == pm.CLOSED
    assert pos.realized_pnl_usd is None      # unknown (closed off-book), not faked


async def test_remove_clears_auto_state(engine):
    pid = await open_position(engine)
    engine._auto_passive.add(pid)
    engine._liq_alerted[pid] = 1.0
    await engine._cmd_remove({"position_id": str(pid)})
    assert engine.positions.get(pid).state == pm.CLOSED
    assert pid not in engine._auto_passive and pid not in engine._liq_alerted


async def test_remove_unknown_symbol(engine):
    assert "no active position" in await engine._cmd_remove({"position_id": "ETH"})


async def test_remove_already_closed(engine):
    pid = await open_position(engine)
    await engine._cmd_remove({"position_id": str(pid)})
    assert "already CLOSED" in await engine._cmd_remove({"position_id": str(pid)})


class _StopClient:
    """Records place/cancel and returns ids, for /stops tests."""
    def __init__(self, open_orders=None):
        self.placed = []
        self.cancelled = []
        self._open = open_orders or []

    async def open_orders(self, symbol):
        return self._open

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)

    async def place_order(self, symbol, side, order_type, **kw):
        from types import SimpleNamespace
        self.placed.append({"symbol": symbol, "side": side, "type": order_type, **kw})
        return SimpleNamespace(order_id=f"oid-{order_type}")

    async def position_risk(self):
        return []


async def test_stops_places_reduce_only_perp_stop_and_spot_limit(engine):
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "150"}
    }
    out = await engine._cmd_stops({"symbol": "BTC"})

    # Perp: reduce-only buy STOP_MARKET at 1% below liq (148.5), full size.
    perp = engine.aster.placed[0]
    assert perp["side"] == "BUY" and perp["type"] == "STOP_MARKET"
    assert perp["reduce_only"] is True and perp["working_type"] == "MARK_PRICE"
    assert perp["stop_price"] == Decimal("148.5")
    assert perp["quantity"] == Decimal("9.95")
    # Spot: sell LIMIT at the same level, full size.
    spot = engine.mexc.placed[0]
    assert spot["side"] == "SELL" and spot["type"] == "LIMIT"
    assert spot["price"] == Decimal("148.5") and spot["quantity"] == Decimal("9.95")
    assert "below liq 150" in out


import time as _time
from types import SimpleNamespace


def _order(executed="0", avg="0", open_=True):
    return SimpleNamespace(
        executed_qty=Decimal(executed), avg_price=Decimal(avg), is_open=open_,
    )


class _GraceClient(_StopClient):
    """_StopClient plus get_order lookups for stop-fire tests."""
    def __init__(self, orders=None, **kw):
        super().__init__(**kw)
        self._orders = orders or {}

    async def get_order(self, symbol, order_id):
        return self._orders[order_id]


def _arm_adl(engine, position_amt: str):
    """Point the engine at a fresh venue snapshot showing `position_amt`, taken
    AFTER the position opened (so it isn't dismissed as post-entry cache lag)."""
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": position_amt,
                    "markPrice": "100", "liquidationPrice": "200"}
    }
    engine._position_risk_ts = _time.monotonic()
    engine._position_risk_wall_ms = int(_time.time() * 1000) + 60_000


async def _wait_state(engine, pid, state, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if engine.positions.get(pid).state == state:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"never reached {state}, is {engine.positions.get(pid).state}"
    )


async def test_hedge_break_warns_but_waits_for_confirmation(engine):
    """First sighting of a venue perp deficit alerts and TAKES OVER the position
    (returns True so basis auto-closes are skipped) but must NOT trade until the
    confirmation window has elapsed."""
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")                       # venue: perp gone
    took_over = await engine._check_hedge_integrity(engine.positions.get(pid))
    assert took_over is True                    # owns the position, skips basis checks
    assert pid in engine._hedge_break
    assert any("possible ADL" in m for m in engine.notifier.messages)
    pos = engine.positions.get(pid)
    assert pos.state == pm.OPEN and pos.perp_qty == Decimal("9.95")  # untraded
    assert pos.exit_mode is None
    # Immediately again (default 30s window not elapsed): still owns, still no trade.
    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True
    assert engine.positions.get(pid).exit_mode is None


async def test_hedge_break_suppresses_converged_tp(engine):
    """Regression (CASHCAT): during the confirmation window the convergence TP
    must NOT close the position — closing 'both legs' when the perp is already
    ADL'd prices a phantom perp buy-back and books a wrong P&L. The guard owns
    the position, so the safety loop skips the basis checks."""
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")                       # perp gone on venue
    # A basis at which the converged TP would otherwise fire (profitable taker).
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")
    engine._position_risk_ts = _time.monotonic()

    took_over = await engine._check_hedge_integrity(engine.positions.get(pid))
    assert took_over is True
    if not took_over:                            # mirror the safety-loop routing
        await engine._check_safety(engine.positions.get(pid))
    pos = engine.positions.get(pid)
    assert pos.exit_mode is None                 # converged-TP suppressed
    assert pos.state == pm.OPEN


async def test_hedge_break_ignores_pre_open_snapshot(engine):
    """Regression (CASHCAT): a just-entered perp lags the risk poll, so a
    snapshot taken BEFORE the position opened showing 0 must NOT read as an
    ADL — no warning, no timer."""
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "100"}
    }
    engine._position_risk_ts = _time.monotonic()
    # Snapshot predates the position opening (cache lag right after entry).
    engine._position_risk_wall_ms = engine.positions.get(pid).opened_ms - 5_000

    took_over = await engine._check_hedge_integrity(engine.positions.get(pid))
    assert took_over is False
    assert pid not in engine._hedge_break
    assert not any("possible ADL" in m for m in engine.notifier.messages)


async def test_hedge_break_ignores_stale_risk_data(engine):
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    engine._position_risk_ts = _time.monotonic() - 999   # stale snapshot
    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is False
    assert pid not in engine._hedge_break                # no timer started


async def test_hedge_break_timer_resets_when_venue_matches_again(engine):
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    await engine._check_hedge_integrity(engine.positions.get(pid))
    assert pid in engine._hedge_break
    _arm_adl(engine, "-9.95")                   # venue matches again
    await engine._check_hedge_integrity(engine.positions.get(pid))
    assert pid not in engine._hedge_break


async def test_full_adl_sells_spot_down_and_closes(engine, monkeypatch):
    """Perp fully ADL'd on venue: after confirmation the DB perp is reconciled
    (synthetic exit at mark) and the naked spot is sold off in tranches until
    the position is CLOSED with realized P&L booked."""
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(config, "ADL_SELL_INTERVAL_SECONDS", 0.01)
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    await engine._check_hedge_integrity(engine.positions.get(pid))  # warn+arm
    acted = await engine._check_hedge_integrity(engine.positions.get(pid))
    assert acted is True
    await _wait_state(engine, pid, pm.CLOSED)
    final = engine.positions.get(pid)
    assert final.perp_qty == 0 and final.spot_qty == 0
    assert final.realized_pnl_usd is not None       # ADL loss/gain is booked
    assert any("perp leg reduced on venue" in m for m in engine.notifier.messages)


async def test_stop_fire_prefers_resting_spot_limit(engine, monkeypatch):
    """When the perp deficit is OUR OWN /stops STOP_MARKET having fired, the
    perp is reconciled at the stop's REAL fill price and the resting MEXC sell
    LIMIT is kept working (grace) — NOT cancelled and market-dumped."""
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 0.0)
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")                       # venue: perp gone
    engine.aster = _GraceClient(
        orders={"A1": _order(executed="9.95", avg="148.5", open_=False)}
    )
    engine.mexc = _GraceClient(orders={"M1": _order(open_=True)})  # unfilled, resting
    engine._stops_orders[pid] = {"aster_id": "A1", "mexc_id": "M1"}

    await engine._check_hedge_integrity(engine.positions.get(pid))   # warn+arm
    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True

    final = engine.positions.get(pid)
    assert final.perp_qty == 0
    assert final.perp_exit_avg == Decimal("148.5")   # REAL stop fill, not mark
    assert engine.mexc.cancelled == []               # spot limit left working
    assert pid in engine._stop_grace
    assert final.state == pm.EXITING
    assert not engine.executor.has_task(pid)         # no market sell-down yet
    assert any("STOP FIRED" in m for m in engine.notifier.messages)


async def test_stop_grace_completes_when_limit_fills(engine):
    """During grace the MEXC limit fills at the stop price: fills are recorded
    and the position closes cleanly — no market selling at all."""
    pid = await _make_live_open(engine)
    engine.paper = False
    # Perp side already reconciled (stop fired) -> perp_qty 0.
    engine.positions.record_fill(
        pid, "aster", "exit", "BUY", Decimal("9.95"), Decimal("148.5"),
        Decimal(0), order_id="A1",
    )
    engine.positions.set_state(pid, pm.EXITING)
    engine.mexc = _GraceClient(
        orders={"M1": _order(executed="9.95", avg="148.5", open_=False)}
    )
    engine._stop_grace[pid] = {
        "until": _time.monotonic() + 60, "mexc_id": "M1",
        "recorded": Decimal(0), "floor": Decimal(0),
    }
    await engine._check_stop_grace(engine.positions.get(pid))
    await _wait_state(engine, pid, pm.CLOSED)
    final = engine.positions.get(pid)
    assert final.spot_qty == 0
    assert final.spot_exit_avg == Decimal("148.5")   # sold at the stop price
    assert pid not in engine._stop_grace
    assert final.realized_pnl_usd is not None


async def test_stop_grace_timeout_falls_back_to_tranches(engine, monkeypatch):
    """Grace expires with the limit unfilled: cancel it and tranche-sell the
    remainder at market."""
    monkeypatch.setattr(config, "ADL_SELL_INTERVAL_SECONDS", 0.01)
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.positions.record_fill(
        pid, "aster", "exit", "BUY", Decimal("9.95"), Decimal("148.5"),
        Decimal(0), order_id="A1",
    )
    engine.positions.set_state(pid, pm.EXITING)
    engine.aster = _GraceClient()
    engine.mexc = _GraceClient(orders={"M1": _order(open_=True)})  # never fills
    engine._stop_grace[pid] = {
        "until": _time.monotonic() - 1, "mexc_id": "M1",   # already expired
        "recorded": Decimal(0), "floor": Decimal(0),
    }
    await engine._check_stop_grace(engine.positions.get(pid))
    assert "M1" in engine.mexc.cancelled            # remnant limit pulled
    assert pid not in engine._stop_grace
    await _wait_state(engine, pid, pm.CLOSED)       # tranche sell-down finished
    assert engine.positions.get(pid).spot_qty == 0


async def test_partial_adl_rebalances_to_surviving_perp(engine, monkeypatch):
    """ADL reduced (not closed) the perp: spot is sold down to match the
    surviving perp and the position stays OPEN at the reduced size."""
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(config, "ADL_SELL_INTERVAL_SECONDS", 0.01)
    pid = await _make_live_open(engine)
    _arm_adl(engine, "-4.95")                   # 5.0 of 9.95 ADL'd away
    await engine._check_hedge_integrity(engine.positions.get(pid))
    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True
    # Wait for the sell-down to finish (position starts OPEN, so wait on the
    # actual rebalance outcome, not the state).
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        p = engine.positions.get(pid)
        if p.spot_qty == Decimal("4.95") and p.state == pm.OPEN and p.exit_mode is None:
            break
        await asyncio.sleep(0.02)
    final = engine.positions.get(pid)
    assert final.perp_qty == Decimal("4.95")    # reconciled to venue
    assert final.spot_qty == Decimal("4.95")    # hedge restored
    assert final.state == pm.OPEN
    assert final.exit_mode is None              # exit request cleared


class _RiskClient(_StopClient):
    """Stub venue client whose position_risk returns the given rows."""
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    async def position_risk(self):
        return self._rows


async def test_startup_hedge_check_arms_guard_for_overnight_adl(engine):
    """An ADL that happened while the engine was DOWN must be caught at boot:
    the startup check fetches fresh venue risk, warns immediately, and arms the
    confirmation timer (the safety loop then acts one window later)."""
    pid = await _make_live_open(engine)
    engine.paper = False
    # Venue reports the perp gone; engine has NO cached risk (fresh boot).
    engine.aster = _RiskClient(
        [{"symbol": "BTCUSDT", "positionAmt": "0", "markPrice": "100"}]
    )
    engine.mexc = _StopClient()
    engine._position_risk = {}
    engine._position_risk_ts = 0.0

    await engine._startup_hedge_check()

    assert pid in engine._hedge_break                     # timer armed at boot
    assert any("possible ADL" in m for m in engine.notifier.messages)
    pos = engine.positions.get(pid)
    assert pos.state == pm.OPEN and pos.perp_qty > 0      # nothing traded yet


async def test_startup_hedge_check_quiet_when_hedged(engine):
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _RiskClient(
        [{"symbol": "BTCUSDT", "positionAmt": "-9.95", "markPrice": "100"}]
    )
    engine.mexc = _StopClient()
    engine._position_risk = {}
    engine._position_risk_ts = 0.0

    await engine._startup_hedge_check()

    assert pid not in engine._hedge_break
    assert not any("possible ADL" in m for m in engine.notifier.messages)


async def test_stops_auto_refresh_on_size_up(engine):
    """After a position grows (size-up), the safety loop re-places its /stops at
    the new size so the added portion isn't left unprotected."""
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "150"}
    }
    await engine._place_stops(engine.positions.get(pid))
    assert engine._stops_qty[pid] == Decimal("9.95")
    before = len(engine.aster.placed)

    # Grow the position (as a size-up fill would).
    engine.positions.record_fill(pid, "aster", "entry", "SELL", Decimal(5), Decimal(100), Decimal(0))
    engine.positions.record_fill(pid, "mexc", "entry", "BUY", Decimal(5), Decimal(100), Decimal(0))

    await engine._ensure_stops(engine.positions.get(pid))
    assert engine._stops_qty[pid] == Decimal("14.95")           # tracks new size
    assert len(engine.aster.placed) > before                    # re-placed
    assert engine.aster.placed[-1]["quantity"] == Decimal("14.95")
    assert any("auto-refreshed" in m for m in engine.notifier.messages)


async def test_stops_resize_noop_when_size_unchanged(engine):
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "150"}
    }
    engine._stops_qty[pid] = Decimal("9.95")                     # matches current
    await engine._ensure_stops(engine.positions.get(pid))
    assert engine.aster.placed == []                            # nothing re-placed


async def test_stops_requires_live_mode(engine):
    await open_position(engine)              # engine.paper stays True
    out = await engine._cmd_stops({"symbol": "BTC"})
    assert "LIVE mode" in out


async def test_stops_needs_liquidation_price(engine):
    await _make_live_open(engine)
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "0"}
    }
    out = await engine._cmd_stops({"symbol": "BTC"})
    assert "no liquidation price" in out
    assert engine.aster.placed == []         # nothing placed without a liq price


class _InfoStub:
    """Stub exchange client exposing only async exchange_info."""
    def __init__(self, infos=None, error: Exception | None = None):
        self._infos = infos or {}
        self._error = error

    async def exchange_info(self):
        if self._error is not None:
            raise self._error
        return self._infos


async def test_refresh_picks_up_new_listing(engine):
    """A coin listed on both venues after startup becomes tradeable on refresh."""
    universe = {"BTCUSDT": object(), "NEWUSDT": object()}
    engine.aster = _InfoStub(universe)
    engine.mexc = _InfoStub(universe)
    assert "NEWUSDT" not in engine.md.pair_maps      # not in the startup universe
    added, removed = await engine._load_symbol_maps()
    assert "NEWUSDT" in engine.md.pair_maps
    assert added == ["NEWUSDT"] and removed == []


async def test_refresh_failure_keeps_existing_universe(engine):
    """A failed exchangeInfo fetch must not wipe the current universe."""
    before = dict(engine.md.pair_maps)
    engine.aster = _InfoStub(error=RuntimeError("aster 5xx"))
    engine.mexc = _InfoStub({"BTCUSDT": object(), "NEWUSDT": object()})
    added, removed = await engine._load_symbol_maps()
    assert added == [] and removed == []
    assert engine.md.pair_maps == before            # untouched


async def test_cmd_refresh_reports_added_symbols(engine):
    universe = {"BTCUSDT": object(), "NEWUSDT": object()}
    engine.aster = _InfoStub(universe)
    engine.mexc = _InfoStub(universe)
    msg = await engine._cmd_refresh()
    assert "NEW" in msg and "+1" in msg


async def test_auto_passive_releases_operator_override(engine):
    """If the operator switches an auto passive exit to a taker close, the auto
    manager releases ownership and stops touching it."""
    pos_id = await open_position(engine)
    engine.positions.set_exit_request(pos_id, "now", None)  # operator took over
    engine.positions.set_state(pos_id, pm.EXITING)
    engine._auto_passive.add(pos_id)
    await engine._check_auto_passive(engine.positions.get(pos_id))
    assert pos_id not in engine._auto_passive


async def test_resolve_position_by_symbol(engine):
    pos_id = await open_position(engine)
    for ref in ("BTC", "btc", "BTCUSDT", str(pos_id)):
        resolved = engine._resolve_position(ref)
        assert isinstance(resolved, pm.Position) and resolved.id == pos_id
    assert "no active position" in engine._resolve_position("ETH")
    assert "no position with id" in engine._resolve_position("999")


async def test_exit_command_accepts_symbol(engine):
    await open_position(engine)
    result = await engine._cmd_exit({"position_id": "btc", "mode": "now"})
    assert "exit started" in result


async def test_enter_on_open_position_sizes_up(engine):
    """/enter on a symbol that's already OPEN adds to it instead of rejecting."""
    pos_id = await open_position(engine)
    before = engine.positions.get(pos_id)
    result = engine._cmd_enter({"symbol": "BTC", "notional": "500"})
    assert "sizing up" in result and f"#{pos_id}" in result
    for _ in range(200):
        cur = engine.positions.get(pos_id)
        if cur.state == pm.OPEN and cur.perp_qty > before.perp_qty:
            break
        await asyncio.sleep(0.02)
    final = engine.positions.get(pos_id)
    assert final.perp_qty > before.perp_qty            # grew, same position
    # target_notional reflects the ACTUAL filled size, not a pre-bumped request.
    assert final.target_notional == final.perp_qty * final.perp_entry_avg
    assert len([p for p in engine.positions.active() if p.symbol == "BTCUSDT"]) == 1


async def test_enter_add_over_cap_rejected(engine, monkeypatch):
    """An add that would push the position past the per-leg cap is refused and
    leaves the position (and its target_notional) untouched."""
    monkeypatch.setattr(config, "MAX_NOTIONAL_PER_LEG_USD", Decimal(1200))
    pos_id = await open_position(engine)           # ~$1000 filled
    before_target = engine.positions.get(pos_id).target_notional
    result = engine._cmd_enter({"symbol": "BTC", "notional": "500"})  # -> ~1500 > 1200
    assert "over the" in result and "per-leg cap" in result
    assert engine.positions.get(pos_id).target_notional == before_target


class _AsterStub:
    def __init__(self, risk):
        self._risk = risk

    async def position_risk(self):
        return self._risk


class _MexcStub:
    def __init__(self, balances, trades):
        self._balances = balances
        self._trades = trades

    async def account(self):
        return {"balances": self._balances}

    async def my_trades(self, symbol, limit=200):
        return self._trades


async def test_cancel_stops_working_exit(engine):
    pos_id = await open_position(engine)
    # Passive exit gated (close ~50bps > target 5) -> sits EXITING, no fills.
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    engine.positions.set_exit_request(pos_id, "passive", Decimal(5))
    await engine.executor.start_exit(engine.positions.get(pos_id))
    for _ in range(50):
        if engine.positions.get(pos_id).state == pm.EXITING:
            break
        await asyncio.sleep(0.02)
    assert engine.positions.get(pos_id).state == pm.EXITING

    msg = await engine._cmd_cancel({"position_id": str(pos_id)})
    assert "cancelled" in msg
    await asyncio.sleep(0.05)
    p = engine.positions.get(pos_id)
    assert p.state == pm.OPEN
    assert p.exit_mode is None
    assert p.perp_qty == Decimal("9.95")  # nothing was closed


async def test_adopt_creates_managed_carry_position(engine):
    engine.paper = False
    engine.aster = _AsterStub(
        [{"symbol": "BTCUSDT", "positionAmt": "-10", "entryPrice": "100.0"}]
    )
    engine.mexc = _MexcStub(
        [{"asset": "BTC", "free": "10", "locked": "0"}],
        [{"isBuyer": True, "qty": "10", "price": "99.5", "time": 1000}],
    )
    msg = await engine._cmd_adopt({"symbol": "BTC"})
    assert "adopted" in msg
    active = engine.positions.active()
    assert len(active) == 1
    p = active[0]
    assert p.symbol == "BTCUSDT" and p.state == pm.OPEN
    assert p.trade_kind == "carry"
    assert p.perp_qty == Decimal(10) and p.spot_qty == Decimal(10)
    assert p.perp_entry_avg == Decimal("100.0")
    assert p.spot_entry_avg == Decimal("99.5")
    # (100 - 99.5)/99.5 * 1e4 = 50.25 bps
    assert p.entry_basis_bps == pytest.approx(Decimal("50.25"), abs=Decimal("0.1"))
    assert p.opened_ms == 1000  # from the earliest spot buy


async def test_adopt_refuses_without_spot_leg(engine):
    engine.paper = False
    engine.aster = _AsterStub(
        [{"symbol": "BTCUSDT", "positionAmt": "-10", "entryPrice": "100.0"}]
    )
    engine.mexc = _MexcStub([{"asset": "BTC", "free": "0", "locked": "0"}], [])
    msg = await engine._cmd_adopt({"symbol": "BTC"})
    assert "no BTC spot held" in msg
    assert engine.positions.active() == []


async def test_adopt_refuses_in_paper_mode(engine):
    engine.paper = True
    msg = await engine._cmd_adopt({"symbol": "BTC"})
    assert "LIVE" in msg
    assert engine.positions.active() == []


class _BalanceStub:
    def __init__(self, aster_bals, mexc_bals):
        self._aster = aster_bals
        self._mexc = mexc_bals

    async def balances(self):
        return self._aster

    async def account(self):
        return {"balances": self._mexc}


async def test_balance_sums_usdt_across_venues(engine):
    stub = _BalanceStub(
        [{"asset": "USDT", "balance": "3160.72", "availableBalance": "3000.00"},
         {"asset": "BTC", "balance": "1.0"}],
        [{"asset": "USDT", "free": "500.50", "locked": "10.00"},
         {"asset": "SKYAI", "free": "100"}],
    )
    engine.aster = stub
    engine.mexc = stub
    msg = await engine._cmd_balance()
    assert "Aster perp" in msg and "3,160.72" in msg
    assert "MEXC spot" in msg and "510.50" in msg  # free + locked
    assert "combined" in msg and "3,671.22" in msg  # 3160.72 + 510.50


async def test_balance_handles_venue_error(engine):
    class _Boom:
        async def balances(self):
            from exchange_client import ExchangeError
            raise ExchangeError("aster", "down")

        async def account(self):
            return {"balances": [{"asset": "USDT", "free": "5", "locked": "0"}]}
    engine.aster = _Boom()
    engine.mexc = _Boom()
    msg = await engine._cmd_balance()
    assert "Aster perp  error" in msg
    assert "MEXC spot" in msg and "5.00" in msg


class _IncomeStub:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def income_history(self, symbol, income_type, start_ms, end_ms):
        self.calls.append((symbol, income_type, start_ms, end_ms))
        return self._rows


async def test_refresh_position_funding_uses_real_income(engine):
    """funding_usd is set to the actual summed FUNDING_FEE income, not a
    current-rate extrapolation."""
    engine.paper = False
    pos = engine.positions.create("BTCUSDT", Decimal(1000), paper=False)
    engine.positions.set_state(pos.id, pm.OPEN)
    engine.conn.execute(
        "UPDATE positions SET opened_ms=? WHERE id=?", (1000, pos.id)
    )
    engine.conn.commit()
    engine.aster = _IncomeStub([{"income": "1.5"}, {"income": "0.7"}, {"income": "-0.1"}])
    await engine._refresh_position_funding()
    assert engine.positions.get(pos.id).funding_usd == Decimal("2.1")
    # queried over the trade's life (from opened_ms)
    assert engine.aster.calls[0][2] == 1000


async def test_refresh_position_funding_skips_paper_positions(engine):
    engine.paper = False
    pos = engine.positions.create("BTCUSDT", Decimal(1000), paper=True)
    engine.positions.set_state(pos.id, pm.OPEN)
    engine.aster = _IncomeStub([{"income": "9.9"}])
    await engine._refresh_position_funding()
    assert engine.positions.get(pos.id).funding_usd == Decimal(0)
    assert engine.aster.calls == []


async def test_exit_qty_accepts_dollar_notional(engine):
    """'$500' sizes the partial close from USD notional at the live perp mid
    (bid 100.4 / ask 100.5 -> mid 100.45), so $500 ~ 4.977 contracts."""
    pos_id = await open_position(engine)
    result = await engine._cmd_exit(
        {"position_id": str(pos_id), "mode": "now", "qty": "$500"}
    )
    assert "exit started" in result
    assert "~$500" in result       # echoed back as notional
    assert "4.977" in result       # round_qty(500 / 100.45)


async def test_exit_qty_bare_number_is_still_coins(engine):
    """A bare number keeps the old meaning: coins, as shown in /positions."""
    pos_id = await open_position(engine)
    result = await engine._cmd_exit(
        {"position_id": str(pos_id), "mode": "now", "qty": "4"}
    )
    assert "size 4 of 9.95" in result


async def test_exit_dollar_below_one_lot_rejected(engine):
    pos_id = await open_position(engine)
    result = await engine._cmd_exit(
        {"position_id": str(pos_id), "mode": "now", "qty": "$0.01"}
    )
    assert "below one lot" in result


def _live_stops_engine(engine):
    """Engine wired for real stop placement (live mode + stub venue clients)."""
    engine.paper = False
    engine.aster = _StopClient()
    engine.mexc = _StopClient()
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100", "liquidationPrice": "150"}
    }


async def test_stops_auto_placed_when_position_has_none(engine):
    """A brand-new OPEN position gets stops without the operator running
    /stops — the 'in case I forget' case."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    assert pid not in engine._stops_qty              # nothing placed yet

    await engine._ensure_stops(engine.positions.get(pid))

    assert engine._stops_qty[pid] == Decimal("9.95")
    assert engine.aster.placed and engine.mexc.placed
    assert any("auto-placed" in m for m in engine.notifier.messages)


async def test_stops_rearmed_after_partial_exit_cancelled_them(engine):
    """/exit cancels stops (the spot LIMIT locks balance the exit needs). Once
    the part-reduce finishes and the position is OPEN again at a smaller size,
    stops must be re-armed automatically."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    await engine._place_stops(engine.positions.get(pid))
    assert engine._stops_qty[pid] == Decimal("9.95")

    # Partial exit: stops cancelled + untracked, then the position shrinks and
    # returns to OPEN (what _complete_exit leaves behind).
    await engine._cancel_stops_for(engine.positions.get(pid))
    assert pid not in engine._stops_qty
    engine.positions.record_fill(pid, "aster", "exit", "BUY", Decimal(4), Decimal(100), Decimal(0))
    engine.positions.record_fill(pid, "mexc", "exit", "SELL", Decimal(4), Decimal(100), Decimal(0))
    engine._auto_stops_attempt.clear()

    await engine._ensure_stops(engine.positions.get(pid))

    assert engine._stops_qty[pid] == Decimal("5.95")          # re-armed at new size
    assert engine.aster.placed[-1]["quantity"] == Decimal("5.95")
    assert any("auto-placed" in m for m in engine.notifier.messages)


async def test_auto_stops_can_be_disabled(engine, monkeypatch):
    monkeypatch.setattr(config, "AUTO_STOPS", False)
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    await engine._ensure_stops(engine.positions.get(pid))
    assert pid not in engine._stops_qty
    assert engine.aster.placed == []


async def test_auto_stops_skipped_while_task_working(engine):
    """An entry/add/exit in flight means the size is still changing — don't
    place stops against a moving target."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    engine.executor.has_task = lambda _pid: True
    await engine._ensure_stops(engine.positions.get(pid))
    assert pid not in engine._stops_qty
    assert engine.aster.placed == []


async def test_auto_stops_retry_is_throttled(engine):
    """A failing placement (no liq price) must not retry — or alert — every
    sweep."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    engine._position_risk = {}                       # no liq price -> fails

    async def _no_risk():
        return []
    engine.aster.position_risk = _no_risk

    await engine._ensure_stops(engine.positions.get(pid))
    first = len(engine.notifier.messages)
    await engine._ensure_stops(engine.positions.get(pid))   # immediate retry
    assert len(engine.notifier.messages) == first           # throttled
    assert pid not in engine._stops_qty


async def test_auto_stops_first_attempt_never_throttled(engine, monkeypatch):
    """monotonic() is small early in a process's life, so defaulting the 'last
    attempt' to 0.0 would throttle the FIRST placement — precisely when a
    position needs stops after an /update restart. A huge retry window must
    still let the first attempt through."""
    monkeypatch.setattr(config, "AUTO_STOPS_RETRY_SECONDS", 1e9)
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)

    await engine._ensure_stops(engine.positions.get(pid))

    assert engine._stops_qty[pid] == Decimal("9.95")
    assert engine.aster.placed                       # really placed


async def _near_liq(engine, pid, dist_pct="108"):
    """Put the position within the liq-alert threshold (mark 100, liq 108)."""
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100",
                    "liquidationPrice": dist_pct}
    }


async def test_passive_exit_stood_down_near_liquidation(engine):
    """A passive exit waits with NO deadline and with stops cancelled. Near
    liquidation that is an unprotected overnight position — stand it down and
    arm stops instead."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    await _near_liq(engine, pid)
    engine.positions.set_exit_request(pid, "passive", Decimal(0))
    engine.positions.set_state(pid, pm.EXITING)
    await engine.executor.start_exit(engine.positions.get(pid))

    await engine._check_liquidation(engine.positions.get(pid))

    pos = engine.positions.get(pid)
    assert pos.state == pm.OPEN               # exit stood down
    assert pos.exit_mode is None
    assert pid in engine._liq_protect
    assert engine._stops_qty.get(pid) is not None    # stops armed
    assert any("stops armed" in m for m in engine.notifier.messages)


async def test_aggressive_exit_is_left_running_near_liquidation(engine):
    """A taker close is actively removing the risk — cancelling it would be
    counterproductive."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    await _near_liq(engine, pid)
    engine.positions.set_exit_request(pid, "now", None)
    engine.positions.set_state(pid, pm.EXITING)
    engine.executor.has_task = lambda _p: True     # pretend it's still working

    await engine._check_liquidation(engine.positions.get(pid))

    assert pid not in engine._liq_protect
    assert engine.positions.get(pid).exit_mode == "now"   # untouched


async def test_manual_exit_after_stand_down_is_honoured(engine):
    """The latch means a /exit issued AFTER the stand-down is not cancelled
    again on the next sweep."""
    pid = await _make_live_open(engine)
    _live_stops_engine(engine)
    await _near_liq(engine, pid)
    engine._liq_protect.add(pid)                  # already stood down
    engine.positions.set_exit_request(pid, "passive", Decimal(0))
    engine.positions.set_state(pid, pm.EXITING)
    engine.executor.has_task = lambda _p: True

    await engine._check_liquidation(engine.positions.get(pid))

    pos = engine.positions.get(pid)
    assert pos.state == pm.EXITING                # left alone
    assert pos.exit_mode == "passive"


async def test_converged_tp_does_not_restart_exit_while_stood_down(engine):
    """Without this the convergence auto-close would re-open a passive exit on
    the next sweep, cancelling the stops again and undoing the protection."""
    pid = await _make_live_open(engine)
    engine._liq_protect.add(pid)
    set_books(engine.md, "99.0", "99.1", "99.9", "100.0")   # converged

    await engine._check_safety(engine.positions.get(pid))

    assert engine.positions.get(pid).exit_mode is None


async def test_stand_down_latch_rearms_after_recovery(engine):
    pid = await _make_live_open(engine)
    engine._liq_protect.add(pid)
    engine._position_risk = {
        "BTCUSDT": {"symbol": "BTCUSDT", "markPrice": "100",
                    "liquidationPrice": "300"}          # far away again
    }
    await engine._check_liquidation(engine.positions.get(pid))
    assert pid not in engine._liq_protect


class _BalanceClient(_StopClient):
    """MEXC stub returning a fixed base-asset balance."""

    def __init__(self, qty):
        super().__init__()
        self._qty = str(qty)

    async def account(self):
        return {"balances": [{"asset": "BTC", "free": self._qty, "locked": "0"}]}


async def test_spot_deficit_reconciled_to_venue(engine, monkeypatch):
    """STONK #189: DB held 366 spot, MEXC had 4. An ambiguous sale that DID
    fill is never recorded, so the DB believes it holds coins that aren't
    there and the exit wedges trying to sell them."""
    monkeypatch.setattr(config, "SPOT_CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 0.0)
    pid = await _make_live_open(engine)
    engine.paper = False
    before = engine.positions.get(pid)
    assert before.spot_qty == Decimal("9.95")
    engine.mexc = _BalanceClient("2.0")          # venue has far less

    await engine._check_spot_integrity(engine.positions.get(pid))   # confirm
    await engine._check_spot_integrity(engine.positions.get(pid))   # act

    after = engine.positions.get(pid)
    assert after.spot_qty == Decimal("2.0")      # reconciled down to venue
    assert any("MEXC shows" in m for m in engine.notifier.messages)


async def test_spot_surplus_is_left_alone(engine, monkeypatch):
    """A balance ABOVE the DB is the operator's own coin, not our business."""
    monkeypatch.setattr(config, "SPOT_CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 0.0)
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.mexc = _BalanceClient("500")          # they hold extra separately

    await engine._check_spot_integrity(engine.positions.get(pid))
    await engine._check_spot_integrity(engine.positions.get(pid))

    assert engine.positions.get(pid).spot_qty == Decimal("9.95")   # untouched


async def test_spot_deficit_needs_confirmation(engine, monkeypatch):
    """One reading must not act — a transient API blip shouldn't book a sale."""
    monkeypatch.setattr(config, "SPOT_CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "HEDGE_BREAK_CONFIRM_SECONDS", 999.0)
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.mexc = _BalanceClient("2.0")

    await engine._check_spot_integrity(engine.positions.get(pid))
    await engine._check_spot_integrity(engine.positions.get(pid))

    assert engine.positions.get(pid).spot_qty == Decimal("9.95")   # not yet


async def test_funding_snapshot_carries_the_24h_basis_range(
    engine, monkeypatch, tmp_path
):
    """/funding needs the pair's own 24h range to answer "is now a good time to
    enter" — an entry near the 24h high is the rich end, near the low means you
    are paying for the carry."""
    import funding as funding_mod
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "fnd.json")
    engine._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
    engine._basis_24h = screener.DailyBasis()
    engine.md.funding_stats["BTCUSDT"] = funding_mod.summarize(
        "BTCUSDT", [], None, now_ms=0
    )
    import time as _time
    now = int(_time.time() * 1000)
    # A day of hourly samples swinging between +10 and +90bps.
    for h in range(24):
        engine._basis_24h.add(
            "BTCUSDT", now - h * 3_600_000, 90.0 if h % 2 else 10.0
        )
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    engine._write_funding_snapshot()
    import json
    row = json.loads((tmp_path / "fnd.json").read_text())["rows"][0]
    assert row["basis_p10_24h"] < row["basis_p90_24h"]
    assert row["hours_24h"] == 24.0


async def test_orders_reports_nothing_when_no_order_is_working(engine):
    out = await engine._cmd_orders({})
    assert "no working orders" in out


async def test_orders_shows_each_side_against_its_own_basis_and_range(engine):
    """An entry waits on the ask/ask basis and an exit on the bid/bid one, and
    they are different numbers. Each working order has to be shown against its
    own side, and against that side's own 24h range."""
    import time as _time
    pos_id = await open_position(engine)
    now = int(_time.time() * 1000)
    for h in range(24):
        # entry basis swings 0..60, close basis 10..70 — deliberately different
        # so a test that mixed the two up would read the wrong bounds.
        engine._basis_24h.add("BTCUSDT", now - h * 3_600_000,
                              60.0 if h % 2 else 0.0, 70.0 if h % 2 else 10.0)
    engine.positions.set_exit_request(pos_id, "passive", Decimal(5))
    engine.positions.set_state(pos_id, pm.EXITING)
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    out = await engine._cmd_orders({})
    assert f"#{pos_id} BTCUSDT" in out and "EXIT passive" in out
    assert "fires at <= +5.0bps" in out
    assert "+50.1" in out                 # bid/bid basis, not the ask/ask +50.0
    assert "24h +10.0 to +70.0" in out    # the CLOSE range, not the entry range


async def test_orders_warns_when_the_level_waited_for_is_out_of_range(engine):
    """An exit target below the pair's 24h low is a level it has not reached
    all day — the order will sit there indefinitely. That is the single most
    useful thing this view can say."""
    import time as _time
    pos_id = await open_position(engine)
    now = int(_time.time() * 1000)
    for h in range(24):
        engine._basis_24h.add("BTCUSDT", now - h * 3_600_000, 50.0, 50.0)
    engine.positions.set_exit_request(pos_id, "passive", Decimal(-200))
    engine.positions.set_state(pos_id, pm.EXITING)
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    out = await engine._cmd_orders({})
    assert "may never fill" in out


async def test_orders_excludes_stops_by_client_id_not_order_type(engine, monkeypatch):
    """The MEXC half of a stop is a plain sell LIMIT — identical in KIND to a
    passive exit — so stops can only be told apart by their client-id prefix.
    Filtering on order type would either hide real exits or show every stop."""
    from exchange_client import OrderResult
    pos_id = await open_position(engine)
    engine.positions.set_exit_request(pos_id, "passive", Decimal(5))
    engine.positions.set_state(pos_id, pm.EXITING)
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    monkeypatch.setattr(engine, "paper", False)

    def _order(cid, side):
        return OrderResult(
            venue="mexc", symbol="BTCUSDT", order_id="1", client_order_id=cid,
            side=side, status="NEW", price=Decimal("100"),
            orig_qty=Decimal(5), executed_qty=Decimal(0),
            avg_price=Decimal(0),
        )

    async def fake_open_orders(symbol):
        return [
            _order("sp_pext_1_1700000000", "BUY"),    # the working exit
            _order("sp_stop_1_1700000000", "SELL"),   # protection — same TYPE
        ]
    monkeypatch.setattr(type(engine.aster), "open_orders",
                        staticmethod(fake_open_orders))
    out = await engine._cmd_orders({})
    assert "BUY" in out
    assert "sp_stop" not in out
    assert "2 stop order(s) hidden" in out


async def test_orders_does_not_warn_when_the_level_is_already_met(engine):
    """A floor the basis ALREADY clears fills on the next tick. Comparing the
    target with the 24h range alone would call that 'may never fill'."""
    import time as _time
    now = int(_time.time() * 1000)
    for h in range(24):
        engine._basis_24h.add("BTCUSDT", now - h * 3_600_000, -100.0, -100.0)
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")   # +50bps live
    pos = engine.positions.create(
        "BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(30)
    )
    engine.positions.set_state(pos.id, pm.ENTERING)
    out = await engine._cmd_orders({})
    assert "ready — the level is met right now" in out
    assert "may never fill" not in out


class _StopOrderClient(_StopClient):
    """_StopClient that can also answer get_order, for hedge-guard tests."""
    def __init__(self, orders=None, open_orders=None):
        super().__init__(open_orders=open_orders)
        self._orders = orders or {}

    async def get_order(self, symbol, order_id):
        from exchange_client import ExchangeError
        if order_id not in self._orders:
            raise ExchangeError("test", "no such order")
        return self._orders[order_id]


def _stop_stub(oid, cid, side, status="NEW", executed="0", price="100"):
    """A /stops order as the venue would report it. Named distinctly from the
    _order helper above, which builds grace-window orders."""
    from exchange_client import OrderResult
    return OrderResult(
        venue="v", symbol="BTCUSDT", order_id=oid, client_order_id=cid,
        side=side, status=status, price=Decimal(price),
        orig_qty=Decimal("9.95"), executed_qty=Decimal(executed),
        avg_price=Decimal(price),
    )


async def test_stops_order_ids_survive_a_restart(engine):
    """Position 191: /stops recorded its order ids IN MEMORY only, the engine
    restarted on an /update, and the hedge guard then could not tell its own
    stop firing from an ADL — booking the perp at mark and market-dumping the
    spot while the sell LIMIT rested at the stop price."""
    pid = await _make_live_open(engine)
    database.save_stop_orders(engine.conn, pid, "aster-1", "mexc-1", Decimal(5))
    # Simulate the restart: fresh in-memory state, same DB.
    engine._stops_orders = {}
    engine._stops_qty = {}
    loaded = database.load_stop_orders(engine.conn)
    assert loaded[pid]["aster_id"] == "aster-1"
    assert loaded[pid]["mexc_id"] == "mexc-1"
    assert Decimal(loaded[pid]["perp_qty"]) == Decimal(5)


async def test_hedge_guard_waits_on_a_resting_spot_limit_after_a_restart(engine):
    """With the ids recovered, a fired stop whose spot twin is still resting
    gets the grace window instead of the tranche market sell."""
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    fired = _stop_stub("aster-1", f"sp_stop_{pid}_1", "BUY", "FILLED", "9.95", "100")
    resting = _stop_stub("mexc-1", f"sp_stop_{pid}_1", "SELL")
    engine.aster = _StopOrderClient(orders={"aster-1": fired})
    engine.mexc = _StopOrderClient(orders={"mexc-1": resting})
    engine._stops_orders[pid] = {"aster_id": "aster-1", "mexc_id": "mexc-1"}
    engine._hedge_break[pid] = _time.monotonic() - 999   # confirmation elapsed

    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True
    assert pid in engine._stop_grace
    assert any("resting at the stop price" in m for m in engine.notifier.messages)
    assert not any("tranches" in m for m in engine.notifier.messages)


async def test_hedge_guard_recovers_the_spot_twin_by_client_id(engine):
    """Belt and braces for a lost DB record: the spot twin is a RESTING sell
    LIMIT, so it is still open and findable by its sp_stop_<id>_ prefix. Without
    this the guard cancels a good limit at the stop price and dumps at market."""
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    resting = _stop_stub("mexc-1", f"sp_stop_{pid}_1", "SELL")
    engine.aster = _StopOrderClient()
    engine.mexc = _StopOrderClient(
        orders={"mexc-1": resting}, open_orders=[resting]
    )
    engine._stops_orders = {}                    # ids lost entirely
    engine._hedge_break[pid] = _time.monotonic() - 999

    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True
    assert pid in engine._stop_grace
    assert not any("tranches" in m for m in engine.notifier.messages)


async def test_hedge_guard_still_tranche_sells_a_real_adl(engine):
    """No resting spot limit = nothing better to do than tranche-sell. The
    change above must not turn every ADL into an indefinite wait."""
    pid = await _make_live_open(engine)
    _arm_adl(engine, "0")
    engine.aster = _StopOrderClient()
    engine.mexc = _StopOrderClient()              # no open orders at all
    engine._stops_orders = {}
    engine._hedge_break[pid] = _time.monotonic() - 999

    assert await engine._check_hedge_integrity(engine.positions.get(pid)) is True
    assert pid not in engine._stop_grace
    assert any("tranches" in m for m in engine.notifier.messages)


class _TradesClient(_StopOrderClient):
    """Adds a userTrades record, for /truefill."""
    def __init__(self, trades=None, **kw):
        super().__init__(**kw)
        self._trades = trades or []

    async def user_trades(self, symbol, start_ms, end_ms, limit=500):
        return [t for t in self._trades
                if start_ms <= int(t["time"]) <= end_ms]


async def test_truefill_replaces_a_mark_price_with_the_venue_vwap(engine):
    """The hedge guard books an unidentified perp close at MARK — a guess. The
    real price lives in Aster's trade record, and until it is pulled back in
    the position's P&L is wrong by the gap between the two."""
    import time as _time
    pid = await _make_live_open(engine)
    engine.paper = False
    # The guard's synthetic close: 9.95 booked at mark 100, order_id 'ADL'.
    engine.positions.record_fill(
        pid, "aster", "exit", "BUY", Decimal("9.95"), Decimal("100"),
        Decimal(0), order_id="ADL",
    )
    now = int(_time.time() * 1000)
    engine.aster = _TradesClient(trades=[
        # Two real partials averaging 104 — the stop filled well above mark.
        {"id": "t1", "side": "BUY", "qty": "5", "price": "103",
         "commission": "0.01", "time": now - 60_000},
        {"id": "t2", "side": "BUY", "qty": "4.95", "price": "105.010101",
         "commission": "0.01", "time": now - 50_000},
        # A SELL in the window must be ignored: it is not a short close.
        {"id": "t3", "side": "SELL", "qty": "9.95", "price": "1",
         "commission": "0", "time": now - 55_000},
    ])
    entry_fees = engine.positions.get(pid).fees_usd    # the close booked 0
    out = await engine._cmd_truefill({"position_id": str(pid)})
    assert "(mark) ->" in out
    pos = engine.positions.get(pid)
    assert pos.perp_exit_avg > Decimal("103.9")     # venue VWAP, not 100
    assert pos.perp_exit_avg < Decimal("104.1")
    # The guard books zero commission on a synthetic close; the venue's real
    # commission comes back with the price.
    assert pos.fees_usd == entry_fees + Decimal("0.02")
    assert pos.realized_pnl_usd is not None


async def test_truefill_is_a_no_op_without_synthetic_fills(engine):
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.aster = _TradesClient()
    out = await engine._cmd_truefill({"position_id": str(pid)})
    assert "no mark-priced fills" in out


async def test_truefill_leaves_the_fill_alone_when_no_trade_matches(engine):
    """A wrong or empty window must leave the book as it was, not zero it."""
    pid = await _make_live_open(engine)
    engine.paper = False
    engine.positions.record_fill(
        pid, "aster", "exit", "BUY", Decimal("9.95"), Decimal("100"),
        Decimal(0), order_id="ADL",
    )
    engine.aster = _TradesClient(trades=[])
    out = await engine._cmd_truefill({"position_id": str(pid)})
    assert "no Aster BUY trades in the window" in out
    assert engine.positions.get(pid).perp_exit_avg == Decimal("100")


def test_recompute_from_fills_rebuilds_averages_after_a_correction(engine):
    """record_fill folds each fill in incrementally, so a later correction
    would otherwise leave the stored average carrying the old price."""
    pos = engine.positions.create("BTCUSDT", Decimal(1000), paper=True)
    engine.positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(10), Decimal(100),
        Decimal("0.1"),
    )
    engine.positions.record_fill(
        pos.id, "aster", "exit", "BUY", Decimal(10), Decimal(90), Decimal("0.1"),
    )
    assert engine.positions.get(pos.id).perp_exit_avg == Decimal(90)
    fill = engine.conn.execute(
        "SELECT id FROM fills WHERE phase='exit'"
    ).fetchone()
    engine.positions.update_fill(
        fill["id"], Decimal(95), Decimal("0.2"), "venue:t1"
    )
    engine.positions.recompute_from_fills(pos.id)
    after = engine.positions.get(pos.id)
    assert after.perp_exit_avg == Decimal(95)
    assert after.fees_usd == Decimal("0.3")     # 0.1 entry + corrected 0.2
    assert after.perp_qty == 0


async def test_recompute_repairs_a_stored_average(engine):
    """Averages are folded in as fills arrive, so a position written by an
    older, wrong derivation keeps that answer until the next fill lands.
    /recompute replays the fills over it."""
    pos = engine.positions.create("BTCUSDT", Decimal(1000), paper=True)
    engine.positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(100), Decimal("100.30"),
        Decimal(0), "a",
    )
    engine.positions.record_fill(
        pos.id, "mexc", "entry", "BUY", Decimal(100), Decimal("100.00"),
        Decimal(0), "m",
    )
    # Corrupt the stored average the way the old derivation would have.
    engine.conn.execute(
        "UPDATE positions SET perp_entry_avg='95.0' WHERE id=?", (pos.id,)
    )
    engine.conn.commit()
    out = await engine._cmd_recompute({"position_id": str(pos.id)})
    assert engine.positions.get(pos.id).perp_entry_avg == Decimal("100.30")
    assert "entry basis" in out and "+30.0bps" in out
    # The corrected basis is written back, so /positions agrees with it.
    row = engine.conn.execute(
        "SELECT entry_basis_bps FROM positions WHERE id=?", (pos.id,)
    ).fetchone()
    assert abs(Decimal(row["entry_basis_bps"]) - Decimal(30)) < Decimal("0.01")


async def test_recompute_does_not_book_pnl_on_an_open_position(engine):
    """Realised P&L on a trade that has not finished would be a fiction."""
    pos = engine.positions.create("BTCUSDT", Decimal(1000), paper=True)
    engine.positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(100), Decimal("100.30"),
        Decimal(0), "a",
    )
    engine.positions.record_fill(
        pos.id, "mexc", "entry", "BUY", Decimal(100), Decimal("100.00"),
        Decimal(0), "m",
    )
    engine.positions.set_state(pos.id, pm.OPEN)
    await engine._cmd_recompute({"position_id": str(pos.id)})
    assert engine.positions.get(pos.id).realized_pnl_usd is None


class _EquityClient:
    """Venue stub returning fixed balances, for the equity sampler."""
    def __init__(self, aster=True):
        self._aster = aster

    async def balances(self):
        return [{"asset": "USDT", "balance": "1000"}]

    async def position_risk(self):
        return []

    async def account(self):
        return {"balances": [{"asset": "USDT", "free": "500", "locked": "0"}]}


async def test_equity_sampler_skips_paper(engine):
    """Paper has no venue balances; a zero row would poison the history with a
    cliff on the day the mode was switched."""
    engine.paper = True
    await engine._sample_equity()
    assert database.equity_history(engine.conn) == []


async def test_equity_sampler_records_and_then_throttles(engine, monkeypatch):
    monkeypatch.setattr(config, "EQUITY_SNAPSHOT_MINUTES", 30.0)
    engine.paper = False
    engine.aster = _EquityClient()
    engine.mexc = _EquityClient(aster=False)
    engine._last_equity = float("-inf")
    await engine._sample_equity()
    assert len(database.equity_history(engine.conn)) == 1
    await engine._sample_equity()          # within the interval
    assert len(database.equity_history(engine.conn)) == 1


async def test_equity_sampler_does_not_store_a_partial_snapshot(engine):
    """A venue that failed contributes 0. Storing that would put a fake crash
    in the history that no later reading can undo."""
    from exchange_client import ExchangeError

    class _Broken:
        async def balances(self):
            raise ExchangeError("aster", "down")

        async def position_risk(self):
            return []

        async def account(self):
            return {"balances": []}

    engine.paper = False
    engine.aster = _Broken()
    engine.mexc = _Broken()
    engine._last_equity = float("-inf")
    await engine._sample_equity()
    assert database.equity_history(engine.conn) == []


def test_basis_log_keeps_an_existing_file_at_its_own_width(engine, tmp_path, monkeypatch):
    """perp_trades_24h was appended to the log format later. A file started
    before the upgrade keeps its header for the rest of the day — appending a
    wider row under a narrower header leaves a ragged CSV."""
    import csv as _csv
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "BASIS_LOG_SECONDS", 0.0)
    day = _time.strftime("%Y%m%d", _time.gmtime())
    old = tmp_path / f"basis_log_{day}.csv"
    old.write_text(
        "ts_ms,symbol,entry_bps,close_bps,funding_8h_bps,max_notional_usd\n"
        "1,BTCUSDT,10.00,5.00,1.00,100\n"
    )
    row = screener.ScreenerRow(
        symbol="BTCUSDT", entry_bps=12.0, close_bps=6.0, spread_cost_bps=6.0,
        fees_bps=0.0, funding_8h_bps=1.0, net_edge_bps=12.0,
        max_notional_usd=100.0, aster_ask="1", mexc_ask="1", ts_ms=2,
    )
    row.perp_trades_24h = 2500.0
    engine._last_basis_log = 0.0
    engine._log_basis_rows([row])
    widths = {len(r) for r in _csv.reader(old.open()) if r}
    assert widths == {6}                       # stayed narrow, no ragged rows

    # A fresh day gets the full format.
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "next")
    engine._last_basis_log = 0.0
    engine._log_basis_rows([row])
    new = next((tmp_path / "next").glob("basis_log_*.csv"))
    rows = list(_csv.reader(new.open()))
    assert "perp_trades_24h" in rows[0] and len(rows[1]) == 7


async def test_funding_hides_discount_rows(engine, monkeypatch, tmp_path):
    """/funding lists premium candidates — short the perp, buy the spot. A
    negative entry basis means the perp is CHEAPER than spot, so the trade
    starts underwater. BANKUSDT scored +147 at an entry of -82 purely because
    its 24h low was -312 and the score read that as convergence to come."""
    import funding as funding_mod
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "fnd.json")
    monkeypatch.setattr(config, "FUNDING_MIN_ENTRY_BPS", 0.0)
    engine._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
    engine._basis_24h = screener.DailyBasis()
    engine.md.funding_stats["BTCUSDT"] = funding_mod.summarize(
        "BTCUSDT", [], None, now_ms=0
    )
    # Perp below spot on both sides -> a discount.
    set_books(engine.md, "99.4", "99.5", "99.9", "100.0")
    engine._write_funding_snapshot()
    import json
    snap = json.loads((tmp_path / "fnd.json").read_text())
    assert snap["rows"] == []
    assert snap["hidden"] == {"discount": 1}

    # The same pair at a premium is listed.
    set_books(engine.md, "100.4", "100.5", "99.9", "100.0")
    engine._write_funding_snapshot()
    snap = json.loads((tmp_path / "fnd.json").read_text())
    assert [r["symbol"] for r in snap["rows"]] == ["BTCUSDT"]


async def test_funding_discount_gate_is_tunable(engine, monkeypatch, tmp_path):
    """The knob is about what gets SURFACED. /enter still takes a negative
    target by hand, so the gate must not be hard-wired to zero."""
    import funding as funding_mod
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "fnd.json")
    monkeypatch.setattr(config, "FUNDING_MIN_ENTRY_BPS", -200.0)
    engine._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
    engine._basis_24h = screener.DailyBasis()
    engine.md.funding_stats["BTCUSDT"] = funding_mod.summarize(
        "BTCUSDT", [], None, now_ms=0
    )
    set_books(engine.md, "99.4", "99.5", "99.9", "100.0")
    engine._write_funding_snapshot()
    import json
    snap = json.loads((tmp_path / "fnd.json").read_text())
    assert [r["symbol"] for r in snap["rows"]] == ["BTCUSDT"]


async def test_cancel_stops_uses_the_recorded_ids_not_just_the_sweep(engine):
    """Position 226: the exit's pre-close cancel deleted the recorded stop ids
    and then relied on a client-id prefix sweep alone. When that sweep found
    nothing, the MEXC stop-limit kept resting — locking the whole spot balance
    — and the exit could not sell for 35 minutes. The recorded ids have to be
    used, and only cleared once dealt with."""
    pid = await _make_live_open(engine)
    engine.paper = False
    database.save_stop_orders(engine.conn, pid, "aster-7", "mexc-7", Decimal(5))
    engine._stops_orders[pid] = {"aster_id": "aster-7", "mexc_id": "mexc-7"}
    # The sweep finds nothing: a venue that does not echo our client ids.
    engine.aster = _StopClient(open_orders=[])
    engine.mexc = _StopClient(open_orders=[])

    n = await engine._cancel_stops_for(engine.positions.get(pid))
    assert engine.aster.cancelled == ["aster-7"]
    assert engine.mexc.cancelled == ["mexc-7"]
    assert n == 2
    # ...and the record is gone only now that it has been used.
    assert database.load_stop_orders(engine.conn) == {}


async def test_cancel_stops_survives_an_engine_restart(engine):
    """The ids live in the DB precisely so a restart cannot lose them — an
    empty in-memory map must still cancel what was recorded."""
    pid = await _make_live_open(engine)
    engine.paper = False
    database.save_stop_orders(engine.conn, pid, "aster-8", "mexc-8", Decimal(5))
    engine._stops_orders = {}                     # fresh process
    engine.aster = _StopClient(open_orders=[])
    engine.mexc = _StopClient(open_orders=[])
    await engine._cancel_stops_for(engine.positions.get(pid))
    assert engine.mexc.cancelled == ["mexc-8"]
