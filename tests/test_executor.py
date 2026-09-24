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
    monkeypatch.setattr(config, "EXIT_REPRICE_MIN_INTERVAL_SECONDS", 0.0)
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


async def test_entry_emits_clip_alert_with_basis(env):
    """Each spot hedge on entry pings Telegram with the fill and live basis."""
    md, positions, executor, notifier, conn = env
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)

    clips = [m for m in notifier.messages if "entry clip" in m]
    assert clips, f"no entry clip alert in {notifier.messages}"
    assert "spot" in clips[0] and "basis" in clips[0]
    assert "bps" in clips[0]


async def test_aggressive_exit_emits_clip_alert(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    notifier.messages.clear()
    positions.set_exit_request(pos_id, "now", None, None)
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)

    clips = [m for m in notifier.messages if "exit clip" in m]
    assert clips, f"no exit clip alert in {notifier.messages}"
    assert "basis" in clips[0]


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


async def test_add_reverts_cleanly_when_hedge_basis_collapses(env, monkeypatch):
    """Sizing up an OPEN position: the add's maker fills, but the hedge-time
    basis collapses below the salvage floor and the add is unwound. The
    position must keep its TRUE prior basis (not a phantom negative blended
    basis from the reversed fills), report 'reverted', and stay balanced."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    pos_id = await open_position(md, positions, executor)   # +50bps, hedged
    before = positions.get(pos_id)
    prior_qty = before.perp_qty
    prior_basis = before.entry_basis_bps
    notifier.messages.clear()

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]     # placement: fine
        return [(Decimal("100.6"), Decimal(100))]         # hedge: basis ~ -10bps
    executor._trader.spot_depth = flip_depth

    executor.start_add(positions.get(pos_id), Decimal(500))
    await wait_for_message(notifier, "add NOT taken")
    await wait_for_state(positions, pos_id, pm.OPEN)

    final = positions.get(pos_id)
    assert final.perp_qty == prior_qty                    # unchanged
    assert final.spot_qty == final.perp_qty               # still hedged, no naked leg
    assert final.entry_basis_bps == prior_basis           # true basis preserved
    assert not any("added" in m for m in notifier.messages)   # not a phantom add


async def test_entry_unwinds_when_hedge_basis_below_salvage_floor(env, monkeypatch):
    """Adverse-selection guard: the resting maker passes the floor at
    placement, but by hedge time the spot has rallied and the executable basis
    has collapsed BELOW the salvage floor (ENTRY_HEDGE_MIN_BPS). Holding would
    lock a loss, so the engine unwinds the perp fill instead."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(10))  # unwind if < 10bps
    # Gate sees +50bps (perp ask 100.5 vs spot ask 100.0) -> places at floor 30.
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        # First call (placement-time hedgeable cap) sees good depth; by the
        # hedge-time re-check the ask has rallied to ~100.49 (basis ~1bps < 10).
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


async def test_entry_salvages_when_basis_below_floor_but_above_min(env, monkeypatch):
    """Salvage: basis collapses below the entry floor (30) but stays above the
    salvage floor (0) — the engine hedges and KEEPS the position rather than
    paying to unwind a still-positive entry."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    # rest the full size in one clip so the test completes to OPEN promptly
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("100000"))
    # Gate sees +50bps -> rests at floor 30. Hedge-time basis ~1bps (>= 0).
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        return [(Decimal("100.49"), Decimal(100))]   # ~1bps, above salvage floor
    executor._trader.spot_depth = flip_depth

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)   # kept, not unwound
    final = positions.get(pos.id)
    assert final.perp_qty > 0
    assert final.spot_qty == final.perp_qty            # fully hedged and held


async def test_entry_salvages_a_deliberately_negative_target(env, monkeypatch):
    """A negative entry target is a real trade, not a mistake. Short perp +
    long spot pays (entry - exit), so entering at -15 to exit at -50 earns
    35bps, and a positive carry pays you to wait. The absolute salvage floor
    assumed every entry was a premium trade and unwound the fill for being
    negative — refusing the position the user actually asked for (AINUSDT
    #194: "collapsed to -14.9bps (below hedge-min 0bps)")."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(15))
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("100000"))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        # Perp rests at 100.5; spot ask 100.65 -> hedge basis ~-14.9bps.
        # Below the absolute floor of 0, but the target was -20, so this is
        # the level that was ASKED for: floor is min(0, -20-15) = -35.
        return [(Decimal("100.65"), Decimal(100))]
    executor._trader.spot_depth = flip_depth

    pos = positions.create(
        "BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(-20)
    )
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)      # entered, not refused
    final = positions.get(pos.id)
    assert final.perp_qty > 0
    assert final.spot_qty == final.perp_qty
    assert not any("collapsed" in m for m in notifier.messages)


async def test_entry_still_unwinds_far_below_a_negative_target(env, monkeypatch):
    """The relative floor must not become no floor: a fill far below even a
    negative target is still a collapse and must unwind."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(15))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        return [(Decimal("101.0"), Decimal(100))]    # ~-49.5bps, below -35
    executor._trader.spot_depth = flip_depth

    pos = positions.create(
        "BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(-20)
    )
    executor.start_entry(pos)
    await wait_for_message(notifier, "collapsed")
    await wait_for_state(positions, pos.id, pm.CANCELLED)
    assert positions.get(pos.id).spot_qty == 0


async def test_premium_entry_salvage_floor_is_unchanged(env, monkeypatch):
    """Regression guard on the change above: for a premium target the absolute
    floor is already the lower of the two, so a +30 entry must still unwind at
    +1bps exactly as before — the relative floor must not loosen it."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(15))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        return [(Decimal("100.6"), Decimal(100))]    # ~-10bps, below 0
    executor._trader.spot_depth = flip_depth

    pos = positions.create(
        "BTCUSDT", Decimal(1000), paper=True, min_entry_bps=Decimal(30)
    )
    executor.start_entry(pos)
    await wait_for_message(notifier, "collapsed")
    await wait_for_state(positions, pos.id, pm.CANCELLED)


async def test_aggressive_exit_closes_immediately(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    positions.set_exit_request(pos_id, "now", None)
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0


def test_executed_basis_bps_applies_multiplier():
    """For a 1000PEPE-style contract the perp price is ~mult x the spot price,
    so the basis must divide perp by qty_multiplier. Without it the number is
    off by ~(mult-1)*1e4 bps (the source of nonsensical exit-basis readings)."""
    from decimal import Decimal
    from executor import Executor
    # perp 10.05 per 1000-coin contract, spot 0.01 per coin, mult 1000
    # true basis = (10.05/1000 - 0.01)/0.01 = +50 bps
    b = Executor._executed_basis_bps(Decimal("10.05"), Decimal("0.01"), Decimal("1000"))
    assert abs(float(b) - 50.0) < 0.01
    # mult=1 unchanged
    b1 = Executor._executed_basis_bps(Decimal("1.2440"), Decimal("1.2451"))
    assert -9.0 < float(b1) < -8.5
    # missing avg -> None
    assert Executor._executed_basis_bps(None, Decimal("1")) is None


async def test_exit_clip_cap_bounds_each_passive_buyback(env, monkeypatch):
    """With EXIT_MAX_CLIP_NOTIONAL_USD set, no single resting perp buy-back on a
    passive exit exceeds the cap — so one sweep can only close a small clip
    before the next tick re-reads spot depth (bounding leg desync)."""
    monkeypatch.setattr(config, "EXIT_MAX_CLIP_NOTIONAL_USD", Decimal(100))
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")

    placed: list[Decimal] = []
    orig = executor._trader.place_perp_maker

    async def spy(symbol, side, qty, price, client_id):
        placed.append(qty)
        return await orig(symbol, side, qty, price, client_id)
    executor._trader.place_perp_maker = spy

    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)

    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0   # fully closed via clips
    assert len(placed) > 1                               # chunked, not one order
    clip_qty = Decimal("1.0")                            # round_qty(100 / 100.0 bid) = 1.000
    assert all(q <= clip_qty for q in placed)            # no buy-back over the cap


def test_passive_exit_uses_faster_reprice_interval():
    """A resting buy-back is sized to spot bid depth that can vanish, so the
    exit must be able to pull it faster than an entry reprices."""
    import inspect
    import executor as ex
    src = inspect.getsource(ex.Executor._run_passive_exit)
    assert "EXIT_REPRICE_MIN_INTERVAL_SECONDS" in src
    assert (config.EXIT_REPRICE_MIN_INTERVAL_SECONDS
            <= config.REPRICE_MIN_INTERVAL_SECONDS)


async def test_exit_basis_prefers_executed_clip_bases(env):
    """The reported exit basis must come from the bases the clips ACTUALLY
    filled at — not the completion-instant quote, which lies when the book
    flickers (BULLA #170 reported -20.5 while the clip filled at +26.8)."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    live = executor._close_basis_bps("BTCUSDT")
    assert live is not None

    # Clips actually filled at +26.8bps on $100 of notional.
    executor._exit_clip_basis[pos_id] = [26.8 * 100.0, 100.0]
    reported = executor._exit_basis_for_msg(positions.get(pos_id))

    assert float(reported) == pytest.approx(26.8)
    assert abs(float(reported) - float(live)) > 1.0   # not the live quote


async def test_exit_basis_weights_clips_by_notional(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    # $300 at +10bps and $100 at +50bps -> notional-weighted mean = +20bps
    executor._exit_clip_basis[pos_id] = [10.0 * 300 + 50.0 * 100, 400.0]
    reported = executor._exit_basis_for_msg(positions.get(pos_id))
    assert float(reported) == pytest.approx(20.0)


async def test_exit_basis_falls_back_to_live_without_clips(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    reported = executor._exit_basis_for_msg(positions.get(pos_id))
    live = executor._close_basis_bps("BTCUSDT")
    assert float(reported) == pytest.approx(float(live))


async def test_exit_completes_with_sub_minimum_notional_dust(env):
    """STONK #169: perp fully closed, 4 coins of spot (~$0.75) left against a
    $5 minimum. Several whole lots, so the old lot-size-only dust test called
    it 'exit incomplete' and wedged the position in EXITING forever."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    # Close the perp entirely and all but a sliver of the spot.
    pos = positions.get(pos_id)
    positions.record_fill(pos_id, "aster", "exit", "BUY",
                          pos.perp_qty, Decimal("100.0"), Decimal(0))
    positions.record_fill(pos_id, "mexc", "exit", "SELL",
                          pos.spot_qty - Decimal("0.02"), Decimal("99.9"), Decimal(0))
    left = positions.get(pos_id)
    assert left.perp_qty == 0
    assert left.spot_qty == Decimal("0.02")          # 0.02 * ~100 = $2 < $5 min

    await executor._finalize_close(pos_id)

    assert positions.get(pos_id).state == pm.CLOSED
    assert not any("exit incomplete" in m for m in notifier.messages)
    assert positions.get(pos_id).realized_pnl_usd is not None   # P&L booked


async def test_exit_still_incomplete_for_a_tradeable_residual(env):
    """A residual that CAN be sold must still block the close — this guard only
    writes off what no order could ever trade."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    pos = positions.get(pos_id)
    positions.record_fill(pos_id, "aster", "exit", "BUY",
                          pos.perp_qty, Decimal("100.0"), Decimal(0))
    # Leave 1.0 coin ~ $100: well above the $5 minimum, genuinely sellable.
    positions.record_fill(pos_id, "mexc", "exit", "SELL",
                          pos.spot_qty - Decimal("1.0"), Decimal("99.9"), Decimal(0))

    await executor._finalize_close(pos_id)

    assert positions.get(pos_id).state != pm.CLOSED
    assert any("exit incomplete" in m for m in notifier.messages)


def test_is_dust_uses_min_notional_not_just_lot_size():
    from exchange_client import SymbolInfo
    inf = SymbolInfo(symbol="X", base_asset="X", quote_asset="USDT",
                     tick_size=Decimal("0.0001"), step_size=Decimal("1"),
                     min_notional=Decimal("5"))
    # 4 whole lots, but only $0.75 -> untradeable (the STONK case)
    assert Executor._is_dust(inf, Decimal(4), Decimal("0.1867")) is True
    # same 4 lots at a price that clears the minimum -> genuinely sellable
    assert Executor._is_dust(inf, Decimal(4), Decimal("10")) is False
    assert Executor._is_dust(inf, Decimal(0), Decimal("10")) is True
    # unknown price: don't write off what we can't value
    assert Executor._is_dust(inf, Decimal(4), Decimal(0)) is False


async def test_entry_clip_reports_fill_to_fill_basis_not_live_quote(env):
    """STONK #186: clips pinged +69/+125.6/+178.5/+54.1 (avg +121bps) but the
    position locked +58.76. The ping quoted the LIVE perp ask, which had risen
    above the price the resting maker actually filled at. It must report the
    basis between the two legs' real fill prices."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    # Live perp ask has run up to 102 ; the maker actually filled at 100.5.
    set_books(md, "101.9", "102.0", "99.9", "100.0")
    live = executor._entry_basis_bps("BTCUSDT")
    notifier.messages.clear()

    await executor._notify_clip(
        pos_id, "BTCUSDT", "entry", "BUY",
        Decimal("10"), Decimal("100.0"), Decimal("100.5"),   # spot 100.0 / perp 100.5
    )

    msg = next(m for m in notifier.messages if "entry clip" in m)
    # (100.5 - 100.0)/100.0 = +50bps locked, NOT the ~200bps the live ask implies
    assert "+50.0bps" in msg
    assert float(live) > 150            # the misleading number it used to print


async def test_clip_falls_back_to_live_basis_without_a_perp_price(env):
    """The tranche sell-down has already reconciled the perp, so there is no
    fill price to pair with — the live basis stays the fallback."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    notifier.messages.clear()

    await executor._notify_clip(
        pos_id, "BTCUSDT", "exit", "SELL", Decimal("10"), Decimal("99.9"),
    )

    msg = next(m for m in notifier.messages if "exit clip" in m)
    live = executor._close_basis_bps("BTCUSDT")
    assert f"{float(live):+.1f}bps" in msg


async def test_passive_exit_sells_spot_left_unhedged_by_an_earlier_run(env):
    """STONK #189: perp already flat, 366 spot (~$85) stranded. `to_sell` only
    accumulates from perp buy-backs made by THIS run, so a run starting with
    the perp already closed concluded 'nothing to sell', finalised, and wedged
    in EXITING with real unhedged exposure."""
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")

    # Close the perp WITHOUT selling the spot — as a prior run / ADL reconcile
    # would leave it.
    pos = positions.get(pos_id)
    positions.record_fill(pos_id, "aster", "exit", "BUY",
                          pos.perp_qty, Decimal("100.0"), Decimal(0))
    stranded = positions.get(pos_id)
    assert stranded.perp_qty == 0
    assert stranded.spot_qty > 0          # real exposure, not dust

    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)

    final = positions.get(pos_id)
    assert final.spot_qty == 0                                   # actually sold
    assert not any("exit incomplete" in m for m in notifier.messages)
    assert final.realized_pnl_usd is not None                    # P&L booked


async def test_oversold_sale_is_throttled_and_backs_off(env, monkeypatch):
    """STONK #189: the DB held 366 spot against 4 on the venue, so every sell
    came back 'Oversold'. Retrying can never succeed until the spot-integrity
    check reconciles, so it must not hammer the venue or the operator."""
    from exchange_client import ExchangeError
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "PASSIVE_UNREACHABLE_ALERT_SECONDS", 600)
    monkeypatch.setattr(config, "HEDGE_RETRY_ATTEMPTS", 1)   # keep the test quick
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")

    # Perp flat, spot still on the books -> the exit will try to sell it.
    pos = positions.get(pos_id)
    positions.record_fill(pos_id, "aster", "exit", "BUY",
                          pos.perp_qty, Decimal("100.0"), Decimal(0))

    async def oversold(symbol, side, qty, cap):
        raise ExchangeError("mexc", "Oversold", 30005)
    executor._trader.spot_taker = oversold

    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await asyncio.sleep(3.0)            # several sell attempts
    await executor._cancel_task(pos_id)

    sale_alerts = [m for m in notifier.messages if "sale incomplete" in m]
    assert len(sale_alerts) == 1                      # throttled, not per-poll
    # No resting order of ours was locking the balance here, so the coins
    # really are missing — and the perp is already closed, so the alert must
    # say this is a naked long rather than promise a reconcile. "Will
    # reconcile it shortly" is what kept position 226 waiting 35 minutes.
    assert "no resting order of ours is locking it" in sale_alerts[0]
    assert "NAKED LONG" in sale_alerts[0]


async def test_add_reports_this_run_basis_separately_from_the_blend(env, monkeypatch):
    """An add to an older, cheaper position blends DOWN correctly and then
    reads as though the new clips went in badly. The two numbers answer
    different questions — how this add executed, and what the whole position
    now averages — so the alert says both."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("100000"))
    monkeypatch.setattr(config, "ENTRY_MIN_EDGE_FLOOR_BPS", Decimal("12"))

    # A pre-existing position entered at a thin +10bps.
    pos = positions.create("BTCUSDT", Decimal(1000), paper=True)
    positions.record_fill(pos.id, "aster", "entry", "SELL", Decimal(100),
                          Decimal("100.10"), Decimal(0))
    positions.record_fill(pos.id, "mexc", "entry", "BUY", Decimal(100),
                          Decimal("100.00"), Decimal(0))
    positions.set_state(pos.id, pm.OPEN)

    # Add at a much richer +50bps.
    set_books(md, "100.4", "100.5", "99.9", "100.0")
    positions.set_min_entry_bps(pos.id, Decimal(30))
    executor.start_add(positions.get(pos.id), Decimal(1000))
    # The position is already OPEN, so wait on the alert rather than the state.
    await wait_for_message(notifier, "added")
    msg = next(m for m in notifier.messages if "added" in m)
    # This run went in at ~+50; the position now averages somewhere between.
    assert "at basis +50" in msg
    blended = float(positions.get(pos.id).entry_basis_bps)
    assert 10 < blended < 50


async def test_partial_salvage_hedges_what_the_ladder_supports(env, monkeypatch):
    """94 recorded unwinds aborted at a quoted basis of +11 to +88 while the
    executable basis for the FULL clip sat under the floor — the hedge was
    walking the book, not chasing a moved market. So take the part the ladder
    supports at the floor and unwind only the excess, instead of buying back
    the lot at a median 38bps."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("100000"))
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal(1))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        # Part of the size is available at a basis that still clears 0; past
        # that the ladder jumps and the VWAP for the WHOLE clip goes negative,
        # which is what the all-or-nothing abort used to react to.
        return [(Decimal("100.0"), Decimal(5)), (Decimal("110.0"), Decimal(100))]
    executor._trader.spot_depth = flip_depth

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True,
                           min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_message(notifier, "collapsed")
    msg = next(m for m in notifier.messages if "collapsed" in m)
    assert "hedging" in msg and "unwinding" in msg
    await wait_for_state(positions, pos.id, pm.OPEN)
    final = positions.get(pos.id)
    # Part of the clip survived, hedged, rather than the whole thing unwinding.
    assert final.perp_qty > 0
    assert final.spot_qty == final.perp_qty


async def test_unwinds_everything_when_nothing_is_hedgeable(env, monkeypatch):
    """A ladder that supports nothing at the floor is the old case, and must
    still unwind the lot rather than hedge a dust clip into a bad basis."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(0))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    calls = {"n": 0}

    async def flip_depth(symbol, side, limit=20):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [(Decimal("100.0"), Decimal(100))]
        return [(Decimal("101.0"), Decimal(100))]   # nothing clears the floor
    executor._trader.spot_depth = flip_depth

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True,
                           min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_message(notifier, "collapsed")
    assert "instead of locking a loss" in next(
        m for m in notifier.messages if "collapsed" in m
    )
    await wait_for_state(positions, pos.id, pm.CANCELLED)
    final = positions.get(pos.id)
    assert final.perp_qty == 0 and final.spot_qty == 0


async def test_a_fully_salvaged_clip_does_not_end_the_entry(env, monkeypatch):
    """Position 226: /enter us 609 filled one $50 clip, then stopped.

    The hedge-time estimate for that clip read below the floor, so the salvage
    branch ran — and found the whole clip hedgeable after all. Nothing was
    unwound and nothing was alerted, but the branch set `aborted`
    unconditionally, so the loop broke on its next pass and the other $559 of
    the order was never worked."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("300"))
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal(1))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    real = executor._live_hedge_basis_bps
    calls = {"n": 0}

    async def estimate_low_once(*a, **kw):
        # A depth snapshot that looks worse than the book really is — the
        # sizing re-read a moment later finds the whole clip hedgeable.
        calls["n"] += 1
        if calls["n"] == 1:
            return Decimal(-50)
        return await real(*a, **kw)
    monkeypatch.setattr(executor, "_live_hedge_basis_bps", estimate_low_once)

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True,
                           min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)
    final = positions.get(pos.id)
    # The whole ~$1000 order, not the first ~$300 clip.
    assert final.perp_qty > Decimal("9")
    assert final.spot_qty == final.perp_qty
    assert calls["n"] > 1                     # more than one clip was hedged
    assert not any("collapsed" in m for m in notifier.messages)


async def test_a_partial_salvage_keeps_working_the_order(env, monkeypatch):
    """Part hedgeable, part unwound: the failure was that clip walking the
    book, not the market, and the next clip is re-sized against the ladder
    anyway. Ending the whole entry there would throw away the rest of the
    order for a problem the next clip does not have."""
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "ENTRY_HEDGE_MIN_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_HEDGE_SLIP_BPS", Decimal(0))
    monkeypatch.setattr(config, "ENTRY_MAX_CLIP_NOTIONAL_USD", Decimal("300"))
    monkeypatch.setattr(config, "MIN_HEDGE_NOTIONAL_USD", Decimal(1))
    set_books(md, "100.4", "100.5", "99.9", "100.0")

    real_est = executor._live_hedge_basis_bps
    real_size = executor._hedgeable_at
    first = {"est": True, "size": True}

    async def estimate(*a, **kw):
        if first["est"]:
            first["est"] = False
            return Decimal(-50)
        return await real_est(*a, **kw)

    async def size(perp, pair, floor, cap, info):
        if first["size"]:
            first["size"] = False
            return info.round_qty(cap / 2)      # only half the first clip
        return await real_size(perp, pair, floor, cap, info)

    monkeypatch.setattr(executor, "_live_hedge_basis_bps", estimate)
    monkeypatch.setattr(executor, "_hedgeable_at", size)

    pos = positions.create("BTCUSDT", Decimal(1000), paper=True,
                           min_entry_bps=Decimal(30))
    executor.start_entry(pos)
    await wait_for_state(positions, pos.id, pm.OPEN)
    final = positions.get(pos.id)
    assert any("hedging" in m and "unwinding" in m for m in notifier.messages)
    # Kept working past the partly-unwound first clip.
    assert final.perp_qty > Decimal("5")
    assert final.spot_qty == final.perp_qty


async def test_exit_frees_a_balance_locked_by_our_own_stop(env, monkeypatch):
    """Position 226: the perp closed, the spot sale failed 'Oversold' for 35
    minutes, and the moment the MEXC stop-limit was cancelled by hand the sale
    went through. The coins were never missing — our own /stops sell LIMIT was
    locking them. The exit must find and cancel that itself."""
    from exchange_client import ExchangeError, OrderResult
    md, positions, executor, notifier, conn = env
    monkeypatch.setattr(config, "PASSIVE_UNREACHABLE_ALERT_SECONDS", 600)
    monkeypatch.setattr(config, "HEDGE_RETRY_ATTEMPTS", 1)
    pos_id = await open_position(md, positions, executor)
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    pos = positions.get(pos_id)
    positions.record_fill(pos_id, "aster", "exit", "BUY",
                          pos.perp_qty, Decimal("100.0"), Decimal(0))
    # /stops recorded the MEXC stop-limit's venue id when it placed it.
    database.save_stop_orders(conn, pos_id, "aster-9", "mexc-9", pos.perp_qty)

    state = {"locked": True, "cancelled": []}
    real_taker = executor._trader.spot_taker

    async def taker(symbol, side, qty, cap):
        if state["locked"]:
            raise ExchangeError("mexc", "Oversold", 30005)
        return await real_taker(symbol, side, qty, cap)

    async def open_orders(symbol):
        # The venue does NOT echo our client id — the case the prefix sweep
        # alone could not handle. Only the recorded id identifies it.
        return [OrderResult(
            venue="mexc", symbol=symbol, order_id="mexc-9", client_order_id="",
            side="SELL", status="NEW", price=Decimal("95"),
            orig_qty=pos.spot_qty, executed_qty=Decimal(0),
            avg_price=Decimal(0),
        )]

    async def cancel(symbol, order_id):
        state["cancelled"].append(order_id)
        state["locked"] = False

    executor._trader.spot_taker = taker
    executor._trader.spot_open_orders = open_orders
    executor._trader.cancel_spot_order = cancel

    positions.set_exit_request(pos_id, "passive", Decimal(15))
    await executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    assert state["cancelled"] == ["mexc-9"]
    assert any("locking the balance" in m for m in notifier.messages)
    assert not any("NAKED LONG" in m for m in notifier.messages)
    assert positions.get(pos_id).spot_qty == 0


async def test_release_never_cancels_someone_elses_order(env, monkeypatch):
    """A resting sell that is not ours — a manual order on the same coin — must
    survive. Only the recorded stop id or our own prefix qualifies."""
    from exchange_client import OrderResult
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)
    cancelled = []

    async def open_orders(symbol):
        return [OrderResult(
            venue="mexc", symbol=symbol, order_id="manual-1",
            client_order_id="myManualOrder", side="SELL", status="NEW",
            price=Decimal("120"), orig_qty=Decimal(5),
            executed_qty=Decimal(0), avg_price=Decimal(0),
        )]

    async def cancel(symbol, order_id):
        cancelled.append(order_id)

    executor._trader.spot_open_orders = open_orders
    executor._trader.cancel_spot_order = cancel
    freed = await executor._release_spot_locks(
        positions.get(pos_id), "BTCUSDT"
    )
    assert freed == 0 and cancelled == []
