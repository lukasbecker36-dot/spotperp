from decimal import Decimal

import pytest

import database
import position_manager as pm


@pytest.fixture
def manager(tmp_path):
    conn = database.init_db(tmp_path / "test.db")
    yield pm.PositionManager(conn)
    conn.close()


def test_lifecycle_and_pnl(manager):
    pos = manager.create("BTCUSDT", Decimal(1000), paper=True)
    assert pos.state == pm.PENDING_ENTRY

    manager.set_state(pos.id, pm.ENTERING)
    # entry: short perp 0.01 @ 100_500, buy spot 0.01 @ 100_000
    manager.record_fill(pos.id, "aster", "entry", "SELL",
                        Decimal("0.01"), Decimal(100_500), Decimal("0"))
    manager.record_fill(pos.id, "mexc", "entry", "BUY",
                        Decimal("0.01"), Decimal(100_000), Decimal("0.5"))
    manager.set_state(pos.id, pm.OPEN)

    pos = manager.get(pos.id)
    assert pos.perp_qty == Decimal("0.01")
    assert pos.spot_qty == Decimal("0.01")
    assert pos.perp_entry_avg == Decimal(100_500)
    assert pos.opened_ms is not None

    # exit: buy back perp @ 100_050, sell spot @ 100_000
    manager.record_fill(pos.id, "aster", "exit", "BUY",
                        Decimal("0.01"), Decimal(100_050), Decimal("0"))
    manager.record_fill(pos.id, "mexc", "exit", "SELL",
                        Decimal("0.01"), Decimal(100_000), Decimal("0.5"))
    pos = manager.get(pos.id)
    assert pos.perp_qty == 0
    assert pos.spot_qty == 0

    pnl = manager.finalize_pnl(pos.id)
    # perp: (100500-100050)*0.01 = 4.5 ; spot: 0 ; fees: 1.0
    assert pnl == Decimal("3.5")


def test_partial_fills_average_correctly(manager):
    pos = manager.create("ETHUSDT", Decimal(500), paper=True)
    manager.record_fill(pos.id, "aster", "entry", "SELL",
                        Decimal("1"), Decimal(100), Decimal(0))
    manager.record_fill(pos.id, "aster", "entry", "SELL",
                        Decimal("1"), Decimal(102), Decimal(0))
    pos = manager.get(pos.id)
    assert pos.perp_qty == Decimal(2)
    assert pos.perp_entry_avg == Decimal(101)

    manager.record_fill(pos.id, "aster", "exit", "BUY",
                        Decimal("1"), Decimal(99), Decimal(0))
    manager.record_fill(pos.id, "aster", "exit", "BUY",
                        Decimal("1"), Decimal(97), Decimal(0))
    pos = manager.get(pos.id)
    assert pos.perp_qty == 0
    assert pos.perp_exit_avg == Decimal(98)


def test_active_and_closed_listing(manager):
    a = manager.create("BTCUSDT", Decimal(100), paper=True)
    b = manager.create("ETHUSDT", Decimal(100), paper=True)
    manager.set_state(b.id, pm.CANCELLED)
    active_ids = [p.id for p in manager.active()]
    assert a.id in active_ids and b.id not in active_ids
    assert [p.id for p in manager.closed()] == [b.id]


def test_pnl_summary_splits_paper_live(manager):
    pos = manager.create("BTCUSDT", Decimal(100), paper=True)
    manager.record_fill(pos.id, "aster", "entry", "SELL", Decimal(1), Decimal(10), Decimal(0))
    manager.record_fill(pos.id, "aster", "exit", "BUY", Decimal(1), Decimal(9), Decimal(0))
    manager.finalize_pnl(pos.id)
    manager.set_state(pos.id, pm.CLOSED)
    summary = manager.pnl_summary()
    assert summary["paper_all_time"] == Decimal("1")
    assert summary["live_all_time"] == Decimal("0")


def test_entry_avg_correct_after_partial_exit_then_add(manager):
    """Enter, partially exit, then add — the full-close P&L must equal
    sum(short sale proceeds) - sum(buy-back cost), regardless of the ordering.
    Regression: the entry avg was weighted by net-held qty, which skewed it
    after a partial exit and double-counted the partial-exit profit."""
    p = manager.create("X", Decimal(2000), paper=True)
    # Short 10 @ 100, then partially buy back 5 @ 110, then short 10 more @ 120.
    manager.record_fill(p.id, "aster", "entry", "SELL", Decimal(10), Decimal(100), Decimal(0))
    manager.record_fill(p.id, "aster", "exit", "BUY", Decimal(5), Decimal(110), Decimal(0))
    manager.record_fill(p.id, "aster", "entry", "SELL", Decimal(10), Decimal(120), Decimal(0))
    # True weighted entry avg of the 20 shorted units = (100*10 + 120*10)/20 = 110.
    assert manager.get(p.id).perp_entry_avg == Decimal(110)

    # Close the rest: 15 @ 105. Total exit = 20 @ (110*5 + 105*15)/20 = 106.25.
    manager.record_fill(p.id, "aster", "exit", "BUY", Decimal(15), Decimal(105), Decimal(0))
    pnl = manager.finalize_pnl(p.id)
    # Short P&L = 2200 sold - 2125 bought back = +75.
    assert pnl == Decimal(75)
