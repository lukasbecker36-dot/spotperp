"""End-to-end paper-mode executor tests.

Paper fills are instant (maker orders fill immediately at their resting
price, spot takers fill at the touch), so entries go ENTERING -> OPEN in
a single tick and exits close within one poll cycle.
"""
import asyncio
from decimal import Decimal

import pytest

import config
import database
import position_manager as pm
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
              mexc_bid: str, mexc_ask: str, mexc_ask_qty: str = "100",
              mexc_bid_qty: str = "100") -> None:
    import time
    ts = int(time.time() * 1000)
    md.aster_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(aster_bid), Decimal(100), Decimal(aster_ask),
        Decimal(100), ts)
    md.mexc_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(mexc_bid), Decimal(mexc_bid_qty), Decimal(mexc_ask),
        Decimal(mexc_ask_qty), ts)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(config, "REPRICE_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal("1"))
    monkeypatch.setattr(config, "ENTRY_TIMEOUT_MINUTES", 1)
    monkeypatch.setattr(config, "EXIT_TIMEOUT_MINUTES", 1)

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
    yield md, positions, executor, notifier, conn
    conn.close()


async def open_position(md, positions, executor) -> int:
    """Drive a position to OPEN: paper fills instantly at the ask/ask."""
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)
    return pos.id


async def wait_for_state(positions, position_id, state, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if positions.get(position_id).state == state:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"position {position_id} never reached {state},"
        f" is {positions.get(position_id).state}"
    )


async def test_entry_fills_and_hedges(env):
    md, positions, executor, notifier, conn = env
    # +50bps entry basis: perp ask 100.5 vs spot ask 100.0
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)

    # Paper fills instantly: maker SELL at 100.5, spot BUY at 100.0
    await wait_for_state(positions, pos.id, pm.OPEN)

    final = positions.get(pos.id)
    assert final.perp_qty == Decimal("9.95")        # 1000 / 100.5 rounded to step
    assert final.spot_qty == final.perp_qty         # fully hedged
    assert final.perp_entry_avg == Decimal("100.5") # paper maker at the ask
    assert final.spot_entry_avg == Decimal("100.0") # paper taker at the ask
    assert final.entry_basis_bps == pytest.approx(Decimal(50), abs=Decimal("0.5"))


async def test_entry_cancel_on_already_filled(env):
    """Cancel request on an instant-filled paper entry: position is already
    OPEN (fully hedged), so cancel has no effect."""
    md, positions, executor, notifier, conn = env
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)

    # Cancel after the fact — position is already OPEN and fully hedged
    result = executor.request_cancel(pos.id)
    assert not result  # task already done
    final = positions.get(pos.id)
    assert final.state == pm.OPEN


async def test_start_add_grows_open_position(env):
    """Sizing up an OPEN position: start_add works another maker entry for the
    incremental notional, folding fills into the same position with a blended
    average — not a second position."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    before = positions.get(pos_id)
    assert before.perp_qty == Decimal("9.95")          # $1000 / 100.5

    # Same +50bps book; add $500 -> ~4.975 more contracts at 100.5.
    executor.start_add(positions.get(pos_id), Decimal(500))
    await wait_for_message(notifier, "added")
    await wait_for_state(positions, pos_id, pm.OPEN)

    final = positions.get(pos_id)
    assert final.perp_qty == Decimal("14.925")
    assert final.state == pm.OPEN
    # target_notional snaps to the actual filled size (perp qty x entry price),
    # so cancelled/partial adds can't inflate it.
    assert final.target_notional == final.perp_qty * final.perp_entry_avg
    assert final.spot_qty == final.perp_qty            # still fully hedged
    assert final.perp_entry_avg == Decimal("100.5")    # blended (same price)
    assert final.entry_basis_bps == pytest.approx(Decimal(50), abs=Decimal("0.5"))
    # still one position in the symbol
    assert len([p for p in positions.active() if p.symbol == "BTCUSDT"]) == 1
    assert any("added" in m for m in notifier.messages)


async def test_add_no_fill_leaves_position_open(env):
    """An add that can't fill (basis below its floor) must leave the prior
    exposure OPEN and unchanged — never CANCEL the position."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    before = positions.get(pos_id)

    # Floor above the live 50bps basis so the add never places.
    positions.set_min_entry_bps(pos_id, Decimal(80))
    # Short entry timeout so the no-fill add returns quickly.
    old = config.ENTRY_TIMEOUT_MINUTES
    config.ENTRY_TIMEOUT_MINUTES = 0
    try:
        executor.start_add(positions.get(pos_id), Decimal(500))
        await wait_for_message(notifier, "filled nothing")
    finally:
        config.ENTRY_TIMEOUT_MINUTES = old

    final = positions.get(pos_id)
    assert final.state == pm.OPEN
    assert final.perp_qty == before.perp_qty           # unchanged
    # A no-fill add must NOT inflate target_notional (the reported bug: repeated
    # cancelled size-ups kept growing 'target now $X').
    assert final.target_notional == final.perp_qty * final.perp_entry_avg


async def test_entry_hedge_ambiguous_does_not_unwind(env):
    """An ambiguous spot hedge (maybe-filled) must NOT unwind the perp (could go
    naked spot) nor retry (could double) — keep the perp fill and alert."""
    from exchange_client import AmbiguousOrderError
    md, positions, executor, notifier, conn = env
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    async def ambiguous(symbol, side, qty, cap):
        raise AmbiguousOrderError("mexc", "timeout after place")
    executor._trader.spot_taker = ambiguous

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_message(notifier, "AMBIGUOUS")
    await wait_for_state(positions, pos.id, pm.OPEN)

    final = positions.get(pos.id)
    assert final.perp_qty > 0        # perp fill kept, not unwound
    assert final.spot_qty == 0       # spot never confirmed


async def test_cancel_task_awaits_cleanup(env):
    """start_exit/_cancel_task must AWAIT the replaced task's CancelledError
    cleanup before returning, so a replacement can't trade concurrently with
    the dying task's order-cancel / spot-sell cleanup."""
    md, positions, executor, notifier, conn = env
    cleanup_done = {"v": False}

    async def slow_task():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)      # simulate cleanup work
            cleanup_done["v"] = True
            raise
    executor._spawn(999, slow_task())
    await asyncio.sleep(0.01)              # let it start

    await executor._cancel_task(999)
    assert cleanup_done["v"] is True       # cleanup finished before we returned


async def test_passive_exit_closes_with_pnl(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    # basis converged: perp 100.0/100.1 vs spot 99.9/100.0 -> close ~10bps
    # passive exit with target 15bps: close basis is ~10bps < 15 -> not gated
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))

    # Paper maker BUY fills instantly at the bid (100.0); spot sells at bid (99.9)
    await wait_for_state(positions, pos_id, pm.CLOSED)

    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0
    # short perp 100.5 -> buy back 100.0 (+0.5/unit), spot 100.0 -> sell 99.9 (-0.1/unit)
    assert final.realized_pnl_usd is not None
    gross = (Decimal("0.5") - Decimal("0.1")) * Decimal("9.95")
    assert final.realized_pnl_usd <= gross
    assert final.realized_pnl_usd > gross - Decimal("2")


async def test_entry_clip_cap_bounds_each_maker_order(env, monkeypatch):
    """With a per-clip notional cap, no single resting maker order exceeds the
    cap (limiting adverse-selection blast radius), yet the entry still reaches
    the full target by chunking."""
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal(100))
    md, positions, executor, notifier, conn = env
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    placed: list[Decimal] = []
    orig = executor._trader.place_perp_maker

    async def spy(symbol, side, qty, price, client_id):
        placed.append(qty)
        return await orig(symbol, side, qty, price, client_id)
    executor._trader.place_perp_maker = spy

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)

    final = positions.get(pos.id)
    assert final.perp_qty == Decimal("9.95")          # full target still reached
    assert len(placed) > 1                             # chunked, not one big order
    clip_qty = Decimal("0.995")                        # round_qty(100 / 100.5)
    assert all(q <= clip_qty for q in placed)          # no clip over the cap


async def test_passive_exit_completes_when_bid_thin(env):
    """A thin MEXC bid caps each buy-back chunk to the closeable size; the
    exit must still fully close, chunking through (and the depth cap must not
    be mistaken for 'position closed')."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    # converged: close basis ~10bps, target 15 -> not gated; only 2 units bid.
    set_books(md, "100.0", "100.1", "99.9", "100.0", mexc_bid_qty="2")
    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0


async def test_passive_exit_respects_target_gate(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    # close basis is ~50bps (same as entry), target 5bps -> gated, no order
    positions.set_exit_request(pos_id, "passive", Decimal(5))
    await executor.start_exit(positions.get(pos_id))
    await asyncio.sleep(0.2)
    final = positions.get(pos_id)
    assert final.state == pm.EXITING
    assert final.perp_qty == Decimal("9.95")  # nothing closed


async def test_entry_floor_blocks_thin_basis(env):
    """Entry with a min_bps floor above the current basis must not place
    the maker order — no fills, no exposure."""
    md, positions, executor, notifier, conn = env
    # +50bps entry basis, but floor demands 80bps
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    pos = positions.create(
        "BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(80)
    )
    executor.start_entry(pos)
    await asyncio.sleep(0.2)
    current = positions.get(pos.id)
    assert current.state == pm.ENTERING
    assert current.perp_qty == 0 and current.spot_qty == 0

    # basis widens past the floor -> order placed and instant-filled
    set_books(md, "100.4", "100.9", "99.9", "100.0")
    await wait_for_state(positions, pos.id, pm.OPEN)
    assert positions.get(pos.id).perp_entry_avg == Decimal("100.9")


async def test_entry_completes_when_topofbook_thin(env):
    """A thin MEXC top of book caps each maker chunk to the hedgeable size;
    the entry should still reach OPEN, fully hedged, by chunking."""
    md, positions, executor, notifier, conn = env
    # +50bps basis but only 2 base units offered on the MEXC ask.
    set_books(md, "100.4", "100.5", "99.9", "100.0", mexc_ask_qty="2")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)
    final = positions.get(pos.id)
    assert final.perp_qty == Decimal("9.95")     # full target reached
    assert final.spot_qty == final.perp_qty       # fully hedged


async def test_hedge_cap_crosses_covering_depth(env, monkeypatch):
    """The hedge IOC is priced to cross live depth up to the needed size,
    plus the buffer — not the cached touch."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "HEDGE_SLIPPAGE_BPS", Decimal("25"))

    async def fake_depth(symbol, side, limit=20):
        return [(Decimal("100.0"), Decimal(5)),
                (Decimal("100.5"), Decimal(5)),   # cumulative 10 covers qty 8
                (Decimal("101.0"), Decimal(50))]
    executor._trader.spot_depth = fake_depth

    cap = await executor._hedge_cap("BTCUSDT", "BUY", Decimal(8), info("BTCUSDT"))
    # crosses to the 100.5 level (completes the fill), +25bps, round up to tick
    # 100.5 * 1.0025 = 100.75125 -> 100.8
    assert cap == Decimal("100.8")


async def test_hedge_cap_falls_back_to_touch_without_depth(env, monkeypatch):
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "HEDGE_SLIPPAGE_BPS", Decimal("25"))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    async def empty_depth(symbol, side, limit=20):
        return []
    executor._trader.spot_depth = empty_depth

    cap = await executor._hedge_cap("BTCUSDT", "BUY", Decimal(8), info("BTCUSDT"))
    # falls back to MEXC ask 100.0 * 1.0025 = 100.25 -> round up to 100.3
    assert cap == Decimal("100.3")


class _MarginStub:
    def __init__(self, err=None):
        self.calls = []
        self.err = err

    async def ensure_perp_margin(self, symbol, leverage, margin_type):
        self.calls.append((symbol, leverage, margin_type))
        return self.err


async def test_ensure_margin_sets_once_and_caches(env):
    md, positions, executor, notifier, conn = env
    executor._paper = False  # exercise the live-path margin setup
    stub = _MarginStub()
    executor._trader = stub
    pos = positions.create("BTCUSDT", Decimal(1000), paper=False)
    await executor._ensure_margin(pos, "BTCUSDT")
    await executor._ensure_margin(pos, "BTCUSDT")  # cached -> no second call
    assert stub.calls == [
        ("BTCUSDT", config.ASTER_LEVERAGE, config.ASTER_MARGIN_TYPE)
    ]
    assert "BTCUSDT" in executor._margin_configured


async def test_ensure_margin_alerts_and_does_not_cache_on_failure(env):
    md, positions, executor, notifier, conn = env
    executor._paper = False
    executor._trader = _MarginStub(err="marginType: aster: position exists (code=-3000)")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=False)
    await executor._ensure_margin(pos, "BTCUSDT")
    assert any("could NOT set" in m for m in notifier.messages)
    assert "BTCUSDT" not in executor._margin_configured  # will retry next entry


async def test_ensure_margin_skipped_in_paper(env):
    md, positions, executor, notifier, conn = env
    stub = _MarginStub()
    executor._trader = stub  # paper stays True
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    await executor._ensure_margin(pos, "BTCUSDT")
    assert stub.calls == []


async def wait_for_message(notifier, needle, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if any(needle in m for m in notifier.messages):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"no alert containing {needle!r}; messages={notifier.messages}"
    )


async def wait_for_perp_qty(positions, position_id, qty, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if positions.get(position_id).perp_qty == qty:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"perp_qty never reached {qty}, is {positions.get(position_id).perp_qty}"
    )


async def test_partial_aggressive_exit_leaves_residual_open(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    full = positions.get(pos_id).perp_qty            # 9.95
    floor = full - Decimal("4.0")                     # stop at 5.95
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    positions.set_exit_request(pos_id, "now", None, floor)
    await executor.start_exit(positions.get(pos_id))

    await wait_for_perp_qty(positions, pos_id, floor)  # closed 4, residual remains
    p = positions.get(pos_id)
    assert p.state == pm.OPEN                          # not CLOSED
    assert p.spot_qty == floor                         # mult 1
    assert p.exit_mode is None                         # request cleared
    assert p.realized_pnl_usd is None                  # P&L booked only at full close


async def test_partial_then_full_exit_closes(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    full = positions.get(pos_id).perp_qty
    floor = full - Decimal("4.0")
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    positions.set_exit_request(pos_id, "now", None, floor)
    await executor.start_exit(positions.get(pos_id))
    await wait_for_perp_qty(positions, pos_id, floor)
    assert positions.get(pos_id).state == pm.OPEN
    # now close the rest (no qty -> full)
    positions.set_exit_request(pos_id, "now", None, None)
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0
    assert final.realized_pnl_usd is not None


async def test_passive_exit_holds_when_depth_missing(env):
    """Regression (BANK -0.43): an empty spot_depth reply must NOT bypass the
    close-size cap and dump the whole position. With a target set and no live
    depth, the protected exit places nothing and stays EXITING."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    # Gate OPEN for target 0: aster bid == mexc bid -> close basis 0, not gated.
    set_books(md, "99.9", "100.0", "99.9", "100.0")

    async def empty_depth(symbol, side, limit=20):
        return []
    executor._trader.spot_depth = empty_depth

    positions.set_exit_request(pos_id, "passive", Decimal(0))
    await executor.start_exit(positions.get(pos_id))
    await asyncio.sleep(0.3)
    p = positions.get(pos_id)
    assert p.state == pm.EXITING            # still working, NOT closed
    assert p.perp_qty == Decimal("9.95")    # nothing dumped at market


async def test_passive_exit_alerts_when_target_below_min_notional(env):
    """When the spot bid depth at the target only supports a sub-min-notional
    buy-back, don't spam rejects/dump — alert that the target is unreachable
    and keep the position open."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    # converged (close ~10bps < target 15, not gated) but only 0.01 on the bid:
    # closeable size is well below the $5 min order.
    set_books(md, "100.0", "100.1", "99.9", "100.0", mexc_bid_qty="0.01")
    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await wait_for_message(notifier, "not reachable")
    p = positions.get(pos_id)
    assert p.state == pm.EXITING
    assert p.perp_qty == Decimal("9.95")    # nothing closed


async def test_entry_aborts_when_hedge_basis_collapses(env):
    """Adverse-selection guard: the resting maker passes the floor at
    placement, but by hedge time the spot has rallied and the executable
    basis has collapsed below floor - abort band. The engine must unwind the
    perp fill instead of locking a bad entry."""
    md, positions, executor, notifier, conn = env
    config.ENTRY_HEDGE_ABORT_BPS = Decimal(20)
    # Gate sees +50bps (perp ask 100.5 vs spot ask 100.0) -> places at floor 30.
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        # First call (placement-time hedgeable cap) sees good depth; by the
        # hedge-time re-check the ask has rallied to ~100.49 (basis ~1bps).
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        return [(Decimal("100.49"), Decimal(100))]
    executor._trader.spot_depth = flip_depth

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_message(notifier, "collapsed")
    await wait_for_state(positions, pos.id, pm.CANCELLED)
    final = positions.get(pos.id)
    assert final.spot_qty == 0           # never hedged into the bad basis
    assert final.perp_qty == 0           # perp fill was unwound
    # The unwind slippage + fees are a real realized loss and must be booked
    # (previously left NULL, so /pnl silently overstated results).
    assert final.realized_pnl_usd is not None
    assert final.realized_pnl_usd <= 0


async def test_aggressive_exit_closes_immediately(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    positions.set_exit_request(pos_id, "now", None)
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0
