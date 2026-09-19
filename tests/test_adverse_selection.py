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
    mean, jit, n, depth = adv.quoted_before(s, 1_000 + 5 * 60_000, window_min=5)
    assert mean == 50.0 and n == 5 and jit == 0.0
    assert depth == 100.0


def test_exit_clips_pair_the_same_way(conn):
    """Entry and exit are mirror images — entry rests a perp SELL and takes
    spot at the ask, exit rests a perp BUY and takes spot at the bid — so one
    pairing serves both."""
    pid = _position(conn)
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,ts_ms)"
        " VALUES (?,'aster','exit','BUY','1000','1.010','0',1000)", (pid,))
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,ts_ms)"
        " VALUES (?,'mexc','exit','SELL','1000','1.000','0',2000)", (pid,))
    conn.commit()
    assert adv.clip_basis(conn, phase="entry") == []
    clips = adv.clip_basis(conn, phase="exit")
    assert [c["locked_bps"] for c in clips] == [100.0]


def test_unwind_fills_are_not_clips(conn):
    """An unwind reverses an entry that was never hedged. There is no spot leg
    and nothing was locked, so it is not a clip of either phase."""
    pid = _position(conn)
    conn.execute(
        "INSERT INTO fills (position_id,venue,phase,side,qty,price,fee_usd,ts_ms)"
        " VALUES (?,'aster','unwind','BUY','1000','1.010','0',1000)", (pid,))
    conn.commit()
    assert adv.clip_basis(conn, phase="entry") == []
    assert adv.clip_basis(conn, phase="exit") == []


def test_quote_series_matches_the_phase():
    """An entry is quoted ask/ask and an exit bid/bid. Comparing a realised
    exit against the entry series would charge it the whole spread."""
    from backtest_divergence import Series
    s = Series()
    for i in range(6):
        s.append(1_000 + i * 60_000, 50.0, 44.0, 1.0, 100.0)
    at = 1_000 + 6 * 60_000
    assert adv.quoted_before(s, at, 10, "entry")[0] == 50.0
    assert adv.quoted_before(s, at, 10, "exit")[0] == 44.0


def test_ols_recovers_a_known_relationship():
    """Pure-Python least squares, because this runs on the trading box and a
    few features over a few hundred clips does not need a linear algebra
    dependency. It still has to be right."""
    import random
    random.seed(4)
    rows = [
        {"notional_usd": n, "size_frac": f,
         "slippage_bps": 2.0 + 0.05 * n + 3.0 * f + random.gauss(0, 0.4)}
        for n, f in (
            (random.uniform(5, 150), random.uniform(0.01, 3.0))
            for _ in range(400)
        )
    ]
    beta, r2, n = adv._ols(rows, "slippage_bps", ["notional_usd", "size_frac"])
    assert beta[0] == pytest.approx(2.0, abs=0.3)
    assert beta[1] == pytest.approx(0.05, abs=0.01)
    assert beta[2] == pytest.approx(3.0, abs=0.2)
    assert r2 > 0.95 and n == 400


def test_ols_refuses_a_sample_too_small_to_fit():
    rows = [{"a": 1.0, "y": 1.0}, {"a": 2.0, "y": 2.0}]
    assert adv._ols(rows, "y", ["a"]) is None


def test_ols_skips_rows_with_a_missing_feature():
    """size_frac is blank when the logs had no depth for that minute. Those
    rows must drop out of the fit rather than be read as zero."""
    rows = [{"a": float(i), "y": 2.0 * i} for i in range(20)]
    rows.append({"a": "", "y": 99.0})
    beta, _r2, n = adv._ols(rows, "y", ["a"])
    assert n == 20 and beta[1] == pytest.approx(2.0, abs=1e-6)


def test_report_skips_rows_whose_bucket_column_is_blank(capsys):
    """size_frac is blank when the logs had no depth for that minute. Bucketing
    compared those against a float and crashed the whole run."""
    rows = [
        {"phase": "entry", "symbol": "A", "quoted_bps": 10.0,
         "locked_bps": 5.0, "slippage_bps": 5.0, "jit_bps": 1.0,
         "size_frac": (0.5 if i % 3 else "")}
        for i in range(40)
    ]
    assert adv._report(rows, "entry", "size_frac") == 5.0
    assert "no size_frac" in capsys.readouterr().err
