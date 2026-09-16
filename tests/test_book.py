"""Tests for the /book order-book formatter."""
from decimal import Decimal

import book


ASTER = {
    "asks": [["100.5", "10"], ["100.6", "20"], ["100.7", "30"],
             ["100.8", "40"], ["100.9", "50"], ["101.0", "60"]],
    "bids": [["100.4", "12"], ["100.3", "22"], ["100.2", "32"],
             ["100.1", "42"], ["100.0", "52"], ["99.9", "62"]],
}
MEXC = {
    "asks": [["100.0", "5"], ["100.1", "6"], ["100.2", "7"],
             ["100.3", "8"], ["100.4", "9"]],
    "bids": [["99.9", "5"], ["99.8", "6"], ["99.7", "7"],
             ["99.6", "8"], ["99.5", "9"]],
}


def test_format_book_shows_both_venues_and_basis():
    out = book.format_book("BTCUSDT", Decimal(1), ASTER, MEXC, levels=5)
    assert "ASTER perp" in out and "MEXC spot" in out
    assert "BTCUSDT order book — top 5" in out
    # best Aster ask 100.5 vs best MEXC ask 100.0 -> +50 bps entry
    assert "entry basis" in out and "+50.0bps" in out
    # passive exit: Aster bid 100.4 vs MEXC bid 99.9 -> +50.05 -> +50.1 bps
    assert "exit passive" in out and "+50.1bps" in out
    # taker exit: Aster ask 100.5 vs MEXC bid 99.9 -> +60.06 -> +60.1 bps
    assert "exit taker" in out and "+60.1bps" in out


def test_format_book_taker_exit_worse_than_passive():
    out = book.format_book("BTCUSDT", Decimal(1), ASTER, MEXC, levels=5)
    # taker exit crosses the perp spread -> strictly worse (higher) close basis
    assert "taker exit crosses the perp spread" in out


def test_format_book_shows_funding_when_provided():
    funding = {"current_8h_bps": 12.3, "avg_24h_8h_bps": 8.5,
               "interval_hours": 8, "next_funding_h": 2.4}
    out = book.format_book("BTCUSDT", Decimal(1), ASTER, MEXC, funding=funding)
    assert "funding" in out
    assert "now +12.3" in out and "24h avg +8.5" in out
    assert "next in 2.4h" in out
    assert "short receives" in out


def test_format_book_omits_funding_when_absent():
    out = book.format_book("BTCUSDT", Decimal(1), ASTER, MEXC)
    assert "funding" not in out


def test_format_book_limits_levels():
    out = book.format_book("BTCUSDT", Decimal(1), ASTER, MEXC, levels=5)
    # 6 ask levels supplied, only 5 shown -> the 101.0 level is dropped
    assert "101" not in out.split("MEXC")[0]
    assert out.count("  ask ") == 10  # 5 asks per venue


def test_format_book_normalizes_multiplier():
    # Aster is a 1000x contract: price 0.10 per contract = 0.0001 base units.
    aster = {"asks": [["0.10", "1000"]], "bids": [["0.099", "1000"]]}
    mexc = {"asks": [["0.0001", "1000000"]], "bids": [["0.000099", "1000000"]]}
    out = book.format_book("PEPEUSDT", Decimal(1000), aster, mexc, levels=5)
    assert "÷1000" in out
    assert "0.0001" in out  # 0.10 / 1000 normalized
    # both touch at 0.0001 / 0.000099 -> ~0 bps basis
    assert "entry basis" in out and "+0.0bps" in out


def test_format_book_handles_empty_side():
    out = book.format_book("BTCUSDT", Decimal(1), {"asks": [], "bids": []}, MEXC)
    assert "ASTER perp: no levels" in out
    assert "MEXC spot" in out
    assert "entry basis" not in out  # no basis without both books
