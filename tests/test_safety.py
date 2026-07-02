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
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal("1"))

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
    await engine._check_safety(engine.positions.get(pos_id))
    pos = engine.positions.get(pos_id)
    assert pos.exit_mode == "now"
    assert any("taking profit" in m for m in engine.notifier.messages)


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
    result = engine._cmd_exit({"position_id": "btc", "mode": "now"})
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
    assert final.target_notional == Decimal(1500)
    assert final.perp_qty > before.perp_qty            # grew, same position
    assert len([p for p in engine.positions.active() if p.symbol == "BTCUSDT"]) == 1


async def test_enter_add_over_cap_rejected(engine, monkeypatch):
    """An add that would push the position past the per-leg cap is refused and
    leaves the position untouched."""
    monkeypatch.setattr(config, "MAX_NOTIONAL_PER_LEG_USD", Decimal(1200))
    pos_id = await open_position(engine)           # $1000 target
    result = engine._cmd_enter({"symbol": "BTC", "notional": "500"})  # -> 1500 > 1200
    assert "over the" in result and "per-leg cap" in result
    assert engine.positions.get(pos_id).target_notional == Decimal(1000)


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
    engine.executor.start_exit(engine.positions.get(pos_id))
    for _ in range(50):
        if engine.positions.get(pos_id).state == pm.EXITING:
            break
        await asyncio.sleep(0.02)
    assert engine.positions.get(pos_id).state == pm.EXITING

    msg = engine._cmd_cancel({"position_id": str(pos_id)})
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
