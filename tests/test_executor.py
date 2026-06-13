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
              mexc_bid: str, mexc_ask: str, mexc_ask_qty: str = "100") -> None:
    import time
    ts = int(time.time() * 1000)
    md.aster_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(aster_bid), Decimal(100), Decimal(aster_ask),
        Decimal(100), ts)
    md.mexc_books["BTCUSDT"] = BookTicker(
        "BTCUSDT", Decimal(mexc_bid), Decimal(100), Decimal(mexc_ask),
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


async def wait_for_state(positions, position_id, state, timeout=5.0):
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


async def test_passive_exit_closes_with_pnl(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    # basis converged: perp 100.0/100.1 vs spot 99.9/100.0 -> close ~10bps
    # passive exit with target 15bps: close basis is ~10bps < 15 -> not gated
    set_books(md, "100.0", "100.1", "99.9", "100.0")
    positions.set_exit_request(pos_id, "passive", Decimal(15))
    executor.start_exit(positions.get(pos_id))

    # Paper maker BUY fills instantly at the bid (100.0); spot sells at bid (99.9)
    await wait_for_state(positions, pos_id, pm.CLOSED)

    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0
    # short perp 100.5 -> buy back 100.0 (+0.5/unit), spot 100.0 -> sell 99.9 (-0.1/unit)
    assert final.realized_pnl_usd is not None
    gross = (Decimal("0.5") - Decimal("0.1")) * Decimal("9.95")
    assert final.realized_pnl_usd <= gross
    assert final.realized_pnl_usd > gross - Decimal("2")


async def test_passive_exit_respects_target_gate(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    # close basis is ~50bps (same as entry), target 5bps -> gated, no order
    positions.set_exit_request(pos_id, "passive", Decimal(5))
    executor.start_exit(positions.get(pos_id))
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


async def test_aggressive_exit_closes_immediately(env):
    md, positions, executor, notifier, conn = env
    pos_id = await open_position(md, positions, executor)

    positions.set_exit_request(pos_id, "now", None)
    executor.start_exit(positions.get(pos_id))
    await wait_for_state(positions, pos_id, pm.CLOSED)
    final = positions.get(pos_id)
    assert final.perp_qty == 0 and final.spot_qty == 0
