"""Pricing an abort: what the buy-back gave up, against the clip it reversed."""
import sys
from pathlib import Path

import pytest

import database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import unwind_cost as uw  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = database.init_db(tmp_path / "t.db")
    yield c
    c.close()


def _position(conn, symbol="STONKUSDT", paper=0):
    cur = conn.execute(
        "INSERT INTO positions (symbol,direction,state,paper,target_notional,"
        "created_ms,updated_ms) VALUES (?,?,?,?,?,?,?)",
        (symbol, "premium", "OPEN", paper, "300", 0, 0),
    )
    conn.commit()
    return cur.lastrowid


def _fill(conn, pid, phase, qty, price, ts, venue="aster"):
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,ts_ms)"
        " VALUES (?,?,?,?,?,?,'0',?)",
        (pid, venue, phase, "SELL" if phase == "entry" else "BUY",
         str(qty), str(price), ts),
    )
    conn.commit()


def test_prices_position_217s_real_unwind(conn):
    """The engine recorded unwind_pnl_usd = -0.9720 for this: 360 units sold at
    0.2774 and bought back at 0.2801. That is 97bps of a $100 clip, to avoid
    entering at -15.8."""
    pid = _position(conn)
    _fill(conn, pid, "entry", 1106, "0.2708", 100)
    _fill(conn, pid, "entry", 360, "0.2774", 300)
    _fill(conn, pid, "unwind", 360, "0.2801", 301)
    ev, = uw.unwind_events(conn)
    assert ev["qty"] == 360
    assert ev["cost_usd"] == pytest.approx(0.972, abs=1e-4)
    assert ev["cost_bps"] == pytest.approx(97.33, abs=0.01)
    assert ev["notional_usd"] == pytest.approx(99.86, abs=0.01)


def test_unwind_priced_against_the_clip_it_reversed(conn):
    """Newest-open-first, as PositionManager derives the averages. Pricing the
    buy-back against the position's whole entry average would charge it to
    clips it never touched."""
    pid = _position(conn)
    _fill(conn, pid, "entry", 1000, "0.100", 100)   # old, cheap
    _fill(conn, pid, "entry", 500, "0.200", 200)    # the clip that aborts
    _fill(conn, pid, "unwind", 500, "0.204", 201)
    ev, = uw.unwind_events(conn)
    assert ev["entry_vwap"] == pytest.approx(0.200)   # not the 0.133 blend
    assert ev["cost_bps"] == pytest.approx(200.0, abs=0.5)


def test_unwind_spanning_two_entry_clips(conn):
    pid = _position(conn)
    _fill(conn, pid, "entry", 100, "1.00", 100)
    _fill(conn, pid, "entry", 100, "1.10", 200)
    _fill(conn, pid, "unwind", 150, "1.10", 201)      # all of the 2nd, half 1st
    ev, = uw.unwind_events(conn)
    assert ev["qty"] == 150
    # VWAP of 100 @ 1.10 + 50 @ 1.00
    assert ev["entry_vwap"] == pytest.approx(1.0666667, abs=1e-6)


def test_a_profitable_unwind_is_a_negative_cost(conn):
    """Buying back below where the clip sold is a gain, and must not be
    reported as a cost — the sign has to survive the good case."""
    pid = _position(conn)
    _fill(conn, pid, "entry", 100, "1.00", 100)
    _fill(conn, pid, "unwind", 100, "0.99", 101)
    ev, = uw.unwind_events(conn)
    assert ev["cost_usd"] == pytest.approx(-1.0)
    assert ev["cost_bps"] == pytest.approx(-100.0, abs=0.5)


def test_spot_and_paper_fills_are_excluded(conn):
    """Only the perp is ever unwound, and paper fills are instant at the
    reference price — including either would wash the cost out."""
    live = _position(conn)
    paper = _position(conn, symbol="BUSDT", paper=1)
    for pid in (live, paper):
        _fill(conn, pid, "entry", 100, "1.00", 100)
        _fill(conn, pid, "unwind", 100, "1.02", 101)
    _fill(conn, live, "entry", 100, "1.00", 102, venue="mexc")
    assert [e["position_id"] for e in uw.unwind_events(conn)] == [live]
    assert [e["position_id"] for e in uw.unwind_events(conn, paper=True)] == [paper]


def test_no_unwinds_is_not_an_error(conn):
    pid = _position(conn)
    _fill(conn, pid, "entry", 100, "1.00", 100)
    assert uw.unwind_events(conn) == []


def test_prefers_the_recorded_hedge_basis_over_the_board_proxy(conn):
    """The engine decides on the EXECUTABLE hedge basis for the size being
    hedged. On a thin book that sits far below the top-of-book quote, which is
    why aborts fire while the board still reads positive — so the recorded
    number and the log proxy are not interchangeable."""
    pid = _position(conn)
    _fill(conn, pid, "entry", 100, "1.00", 100)
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,"
        "basis_bps,ts_ms) VALUES (?,'aster','unwind','BUY','100','1.02','0',"
        "'-40.5',101)", (pid,))
    conn.commit()
    ev, = uw.unwind_events(conn)
    assert ev["hedge_basis_bps"] == -40.5


def test_hedge_basis_absent_on_older_fills(conn):
    """Unwinds recorded before the column existed have to be distinguishable
    from ones that genuinely aborted at zero."""
    pid = _position(conn)
    _fill(conn, pid, "entry", 100, "1.00", 100)
    _fill(conn, pid, "unwind", 100, "1.02", 101)
    ev, = uw.unwind_events(conn)
    assert ev["hedge_basis_bps"] == ""
