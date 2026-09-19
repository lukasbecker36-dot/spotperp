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


def test_unwound_entry_leaves_the_entry_average(manager):
    """GUSDT showed a -100bps entry basis. An entry clip that filled and was
    then unwound (its spot hedge never bought) stayed in perp_entry_avg, so the
    recorded basis blended a leg that is not held and has no spot against it."""
    mgr = manager
    pos = mgr.create("GUSDT", Decimal(200), paper=False)

    def basis():
        p = mgr.get(pos.id)
        return (p.perp_entry_avg - p.spot_entry_avg) / p.spot_entry_avg * 10000

    # A good clip: short perp at 1.0030 against spot at 1.0000 -> +30bps.
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(1000),
                    Decimal("1.0030"), Decimal(0), "o1")
    mgr.record_fill(pos.id, "mexc", "entry", "BUY", Decimal(1000),
                    Decimal("1.0000"), Decimal(0), "m1")
    assert basis() == Decimal(30)

    # A second, bigger clip fills where the basis had collapsed...
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(3000),
                    Decimal("1.0005"), Decimal(0), "o2")
    assert basis() < Decimal(15)          # blended down while it is unhedged
    # ...and is unwound, never hedged.
    mgr.record_fill(pos.id, "aster", "unwind", "BUY", Decimal(3000),
                    Decimal("1.0012"), Decimal(0), "o3")

    after = mgr.get(pos.id)
    assert basis() == Decimal(30)          # back to the basis actually held
    assert after.perp_qty == Decimal(1000)
    # The buy-back closed nothing, so it must not price the exit either.
    assert after.perp_exit_avg is None
    # Sold 3000 at 1.0005, bought back at 1.0012: a real 2.10 loss, kept apart
    # from both averages and carried into realised P&L.
    assert after.unwind_pnl_usd == Decimal("-2.1000")
    assert mgr.finalize_pnl(pos.id) == Decimal("-2.1000")


def test_partial_unwind_matches_the_most_recent_entries(manager):
    """An unwind always reverses the increment that just filled, so entries are
    matched off LIFO. The fully-reverted case used to be patched by hand in the
    executor, which left a PARTIAL unwind showing the phantom basis."""
    mgr = manager
    pos = mgr.create("GUSDT", Decimal(300), paper=False)
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(1000),
                    Decimal("1.0030"), Decimal(0), "o1")
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(1000),
                    Decimal("1.0000"), Decimal(0), "o2")
    # Reverse only half of the second clip.
    mgr.record_fill(pos.id, "aster", "unwind", "BUY", Decimal(500),
                    Decimal("1.0000"), Decimal(0), "o3")
    after = mgr.get(pos.id)
    assert after.perp_qty == Decimal(1500)
    # 1000 @ 1.0030 + 500 @ 1.0000 -> 1.0020, i.e. the newest clip is the one
    # partly removed, not a pro-rata slice of both.
    assert after.perp_entry_avg == Decimal("1.0020")


def test_unwind_does_not_count_as_an_exit(manager):
    """An unwind reduces the position but closes nothing. Counting it as an
    exit would price the close off a trade that never closed a position."""
    mgr = manager
    pos = mgr.create("GUSDT", Decimal(200), paper=False)
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(2000),
                    Decimal("1.0030"), Decimal(0), "o1")
    mgr.record_fill(pos.id, "mexc", "entry", "BUY", Decimal(2000),
                    Decimal("1.0000"), Decimal(0), "m1")
    mgr.record_fill(pos.id, "aster", "unwind", "BUY", Decimal(1000),
                    Decimal("1.0040"), Decimal(0), "o2")
    assert mgr.leg_qtys(pos.id)["perp_exit"] == 0
    mgr.record_fill(pos.id, "aster", "exit", "BUY", Decimal(1000),
                    Decimal("0.9990"), Decimal(0), "o3")
    after = mgr.get(pos.id)
    assert after.perp_exit_avg == Decimal("0.9990")   # the real close only
    assert mgr.leg_qtys(pos.id)["perp_exit"] == Decimal(1000)
