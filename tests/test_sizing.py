"""Tests for depth-aware maker sizing (max_hedgeable_qty)."""
from decimal import Decimal

import pytest

from executor import max_closeable_qty, max_hedgeable_qty

BPS = Decimal(10000)


def basis_bps(perp: Decimal, vwap: Decimal) -> Decimal:
    return (perp - vwap) / vwap * BPS


def vwap_of(asks, qty: Decimal) -> Decimal:
    cost = Decimal(0)
    taken = Decimal(0)
    for p, q in asks:
        take = min(q, qty - taken)
        cost += take * p
        taken += take
        if taken >= qty:
            break
    return cost / taken


def test_full_depth_when_all_levels_clear_floor():
    # perp 100, floor 0 -> vwap must stay <= 100. All asks below 100.
    asks = [(Decimal("99.0"), Decimal(10)), (Decimal("99.5"), Decimal(10))]
    q = max_hedgeable_qty(Decimal(100), asks, Decimal(0))
    assert q == Decimal(20)


def test_zero_when_best_ask_fails_floor():
    # perp 100, floor 50bps -> vwap_max ~99.5. Best ask 99.6 already breaks it.
    asks = [(Decimal("99.6"), Decimal(10))]
    assert max_hedgeable_qty(Decimal(100), asks, Decimal(50)) == Decimal(0)


def test_partial_fill_at_binding_level():
    # perp 100, floor 0 -> vwap_max = 100.
    # L1: 99 x10 (vwap 99, ok). L2: 101 x10 (would push vwap >100).
    # Take x of L2 so vwap == 100: (99*10 + 101*x)/(10+x) = 100
    #   990 + 101x = 1000 + 100x -> x = 10. But only 10 available -> take 10?
    # vwap with full L2: (990+1010)/20 = 100 exactly -> floor met, take all 20.
    asks = [(Decimal("99"), Decimal(10)), (Decimal("101"), Decimal(10))]
    q = max_hedgeable_qty(Decimal(100), asks, Decimal(0))
    assert q == Decimal(20)
    assert basis_bps(Decimal(100), vwap_of(asks, q)) == Decimal(0)


def test_partial_fill_stops_before_full_level():
    # perp 100, floor 0 -> vwap_max = 100.
    # L1: 99 x10. L2: 110 x100. Take x of L2: (990+110x)/(10+x)=100
    #   990 + 110x = 1000 + 100x -> 10x = 10 -> x = 1.
    asks = [(Decimal("99"), Decimal(10)), (Decimal("110"), Decimal(100))]
    q = max_hedgeable_qty(Decimal(100), asks, Decimal(0))
    assert q == Decimal(11)
    # VWAP at the cap sits exactly at the floor.
    assert basis_bps(Decimal(100), vwap_of(asks, q)) == pytest.approx(
        Decimal(0), abs=Decimal("0.001")
    )


def test_monotonic_basis_decreases_past_cap():
    # One unit past the returned cap must violate the floor.
    asks = [(Decimal("99"), Decimal(10)), (Decimal("110"), Decimal(100))]
    floor = Decimal("5")
    q = max_hedgeable_qty(Decimal(100), asks, floor)
    assert basis_bps(Decimal(100), vwap_of(asks, q)) >= floor - Decimal("0.01")
    over = vwap_of(asks, q + Decimal(1))
    assert basis_bps(Decimal(100), over) < floor


def test_empty_book_returns_zero():
    assert max_hedgeable_qty(Decimal(100), [], Decimal(0)) == Decimal(0)
    assert max_hedgeable_qty(Decimal(0), [(Decimal(1), Decimal(1))], Decimal(0)) == 0


# ── exit side: max_closeable_qty (sell spot, basis must stay <= target) ──

def close_bps(perp: Decimal, vwap: Decimal) -> Decimal:
    return (perp - vwap) / vwap * BPS


def test_closeable_full_depth_when_all_bids_clear_target():
    # perp 100, target 0 -> vwap must stay >= 100. All bids at/above 100.
    bids = [(Decimal("100.5"), Decimal(10)), (Decimal("100.0"), Decimal(10))]
    assert max_closeable_qty(Decimal(100), bids, Decimal(0)) == Decimal(20)


def test_closeable_zero_when_best_bid_above_target():
    # perp 100, target 0 -> need vwap >= 100, but best bid 99.5 -> basis > 0.
    bids = [(Decimal("99.5"), Decimal(10))]
    assert max_closeable_qty(Decimal(100), bids, Decimal(0)) == Decimal(0)


def test_closeable_partial_at_binding_level():
    # perp 100, target 0 -> vwap_min = 100.
    # L1: 101 x10 (vwap 101, ok). L2: 99 x100 -> would drop vwap below 100.
    # take x of L2: (1010 + 99x)/(10+x) = 100 -> 1010 + 99x = 1000 + 100x
    #   -> x = 10.
    bids = [(Decimal("101"), Decimal(10)), (Decimal("99"), Decimal(100))]
    q = max_closeable_qty(Decimal(100), bids, Decimal(0))
    assert q == Decimal(20)
    assert close_bps(Decimal(100), vwap_of(bids, q)) == pytest.approx(
        Decimal(0), abs=Decimal("0.001")
    )


def test_closeable_monotonic_basis_rises_past_cap():
    bids = [(Decimal("101"), Decimal(10)), (Decimal("99"), Decimal(100))]
    target = Decimal("5")
    q = max_closeable_qty(Decimal(100), bids, target)
    assert close_bps(Decimal(100), vwap_of(bids, q)) <= target + Decimal("0.01")
    over = vwap_of(bids, q + Decimal(1))
    assert close_bps(Decimal(100), over) > target


def test_closeable_empty_returns_zero():
    assert max_closeable_qty(Decimal(100), [], Decimal(0)) == Decimal(0)
