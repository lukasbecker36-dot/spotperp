from decimal import Decimal

import pytest

import funding

HOUR = 3_600_000


def test_derive_interval_one_hour():
    t0 = 1_700_000_000_000
    times = [t0 + i * HOUR for i in range(6)]
    assert funding.derive_interval_hours(times) == 1


def test_derive_interval_eight_hour():
    t0 = 1_700_000_000_000
    times = [t0 + i * 8 * HOUR for i in range(4)]
    assert funding.derive_interval_hours(times) == 8


def test_derive_interval_four_hour_with_jitter():
    t0 = 1_700_000_000_000
    # 4h spacing with small clock jitter
    times = [t0, t0 + 4 * HOUR + 1234, t0 + 8 * HOUR - 500, t0 + 12 * HOUR]
    assert funding.derive_interval_hours(times) == 4


def test_derive_interval_thin_history_defaults_8h():
    assert funding.derive_interval_hours([]) == 8
    assert funding.derive_interval_hours([1_700_000_000_000]) == 8


def test_summarize_one_hour_funding_projects_to_8h():
    # BEAT-style: 0.0823% per 1h funding -> 8h-equivalent 65.84 bps
    now = 1_700_000_000_000 + 30 * HOUR + HOUR // 2  # off the boundary
    rate = Decimal("0.000823")
    history = [(1_700_000_000_000 + i * HOUR, rate) for i in range(31)]
    stat = funding.summarize("BEATUSDT", history, rate, now_ms=now)
    assert stat.interval_hours == 1
    assert stat.current_8h_bps == pytest.approx(65.84, abs=0.01)
    # last 24h has 24 prints of 8.23 bps each -> realised 197.5 bps
    assert stat.samples_24h == 24
    assert stat.realized_24h_bps == pytest.approx(24 * 8.23, abs=0.1)
    # 8h-equiv average = realised / 3
    assert stat.avg_24h_8h_bps == pytest.approx(24 * 8.23 / 3, abs=0.1)


def test_summarize_eight_hour_average_equals_current_when_flat():
    now = 1_700_000_000_000 + 100 * HOUR
    rate = Decimal("0.0001")  # 1 bps / 8h
    history = [(1_700_000_000_000 + i * 8 * HOUR, rate) for i in range(13)]
    stat = funding.summarize("BTCUSDT", history, rate, now_ms=now)
    assert stat.interval_hours == 8
    assert stat.current_8h_bps == pytest.approx(1.0, abs=0.01)
    # 3 prints in last 24h, each 1 bps -> realised 3, /3 = 1.0
    assert stat.avg_24h_8h_bps == pytest.approx(1.0, abs=0.01)


def test_summarize_average_ignores_spike_outside_window():
    now = 1_700_000_000_000 + 100 * HOUR
    base = Decimal("0.0001")
    # one old spike, then flat low funding inside the window
    history = [(1_700_000_000_000, Decimal("0.05"))]  # ancient 500bps spike
    history += [(now - i * 8 * HOUR, base) for i in range(1, 4)]
    history.sort(key=lambda r: r[0])
    stat = funding.summarize("X", history, base, now_ms=now)
    # spike is >24h old, excluded; average reflects only the flat low funding
    assert stat.avg_24h_8h_bps == pytest.approx(1.0, abs=0.01)
