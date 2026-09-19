"""Pairing entry fills into clips, so quoted basis can be compared with locked."""
import sys
from pathlib import Path

import pytest

import database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import adverse_selection as adv  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = database.init_db(tmp_path / "t.db")
    yield c
    c.close()


def _position(conn, symbol="GUSDT", paper=0):
    cur = conn.execute(
        "INSERT INTO positions (symbol,direction,state,paper,target_notional,"
        "created_ms,updated_ms) VALUES (?,?,?,?,?,?,?)",
        (symbol, "premium", "CLOSED", paper, "1000", 0, 0),
    )
    conn.commit()
    return cur.lastrowid


def _fill(conn, pid, venue, side, qty, price, ts):
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,ts_ms)"
        " VALUES (?,?,'entry',?,?,?,'0',?)",
        (pid, venue, side, str(qty), str(price), ts),
    )
    conn.commit()


def test_infer_multiplier_from_the_price_ratio():
    """Contract multipliers are not in the DB. Getting one wrong moves the
    basis by ~(mult-1)x1e4 bps, so a ratio near nothing is refused."""
    assert adv.infer_multiplier(1.003, 1.0) == 1
    assert adv.infer_multiplier(1003.0, 1.0) == 1000
    assert adv.infer_multiplier(37.0, 1.0) is None      # near no power of ten
    assert adv.infer_multiplier(0.0, 1.0) is None


def test_clip_pairs_perp_fills_oldest_first(conn):
    """A spot buy hedges the perp fills that were waiting, oldest first — the
    order the executor drains them in. The locked basis must use the VWAP of
    exactly those, not of every perp fill in the position."""
    pid = _position(conn)
    _fill(conn, pid, "aster", "SELL", 600, 1.010, 1_000)   # +100bps vs 1.0
    _fill(conn, pid, "aster", "SELL", 400, 1.020, 2_000)   # +200bps
    _fill(conn, pid, "mexc", "BUY", 600, 1.000, 3_000)     # hedges the FIRST
    _fill(conn, pid, "mexc", "BUY", 400, 1.000, 4_000)     # ...then the second
    clips = adv.clip_basis(conn)
    assert [c["locked_bps"] for c in clips] == [100.0, 200.0]


def test_clip_splits_a_perp_fill_across_two_hedges(conn):
    pid = _position(conn)
    _fill(conn, pid, "aster", "SELL", 1000, 1.010, 1_000)
    _fill(conn, pid, "mexc", "BUY", 400, 1.000, 2_000)
    _fill(conn, pid, "mexc", "BUY", 600, 1.000, 3_000)
    clips = adv.clip_basis(conn)
    assert [c["locked_bps"] for c in clips] == [100.0, 100.0]
    assert [c["qty_base"] for c in clips] == [400.0, 600.0]


def test_clip_applies_the_multiplier(conn):
    """A 1000X contract quotes ~1000x the spot price; without dividing through
    the basis is off by ~10 million bps."""
    pid = _position(conn, symbol="1000PEPEUSDT")
    _fill(conn, pid, "aster", "SELL", 1, 10.10, 1_000)     # 1 contract
    _fill(conn, pid, "mexc", "BUY", 1000, 0.01, 2_000)     # 1000 base units
    clips = adv.clip_basis(conn)
    assert clips[0]["locked_bps"] == pytest.approx(100.0, abs=0.5)


def test_unhedged_perp_fills_make_no_clip(conn):
    """A perp fill that was never hedged (unwound) locked nothing, so it is not
    a clip — counting it would invent a basis that was never traded."""
    pid = _position(conn)
    _fill(conn, pid, "aster", "SELL", 1000, 1.010, 1_000)
    clips = adv.clip_basis(conn)
    assert clips == []


def test_paper_fills_are_separated(conn):
    """Paper fills are instant at the reference price — including them would
    wash out exactly the effect being measured."""
    live = _position(conn, paper=0)
    paper = _position(conn, symbol="BUSDT", paper=1)
    for pid in (live, paper):
        _fill(conn, pid, "aster", "SELL", 1000, 1.010, 1_000)
        _fill(conn, pid, "mexc", "BUY", 1000, 1.000, 2_000)
    assert [c["position_id"] for c in adv.clip_basis(conn)] == [live]
    assert [c["position_id"] for c in adv.clip_basis(conn, paper=True)] == [paper]


def test_quoted_window_uses_only_quotes_before_the_fill():
    """By the time a resting order fills the basis has already moved. Pricing
    the comparison at the fill would bake in the very effect being measured."""
    from backtest_divergence import Series
    s = Series()
    for i in range(10):
        # +50 before the fill instant, then a jump to +200 after it.
        s.append(1_000 + i * 60_000, 50.0 if i < 5 else 200.0, 44.0, 1.0, 100.0)
    mean, jit, n = adv.quoted_before(s, 1_000 + 5 * 60_000, window_min=5)
    assert mean == 50.0 and n == 5 and jit == 0.0
