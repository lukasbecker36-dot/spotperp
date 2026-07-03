"""Alert chunking so Telegram's 4096-char limit can't drop an emergency alert."""
from notify import _chunks


def test_chunks_under_limit_single_message():
    assert _chunks("short", 4000) == ["short"]


def test_chunks_split_by_lines_within_limit():
    msg = "\n".join(f"line {i}" for i in range(1000))
    parts = _chunks(msg, 200)
    assert all(len(p) <= 200 for p in parts)
    # Round-trips (modulo the join newlines) — no content lost.
    assert "".join(parts).replace("\n", "") == msg.replace("\n", "")


def test_chunks_hard_splits_an_overlong_line():
    parts = _chunks("x" * 9500, 4000)
    assert len(parts) == 3 and all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == "x" * 9500
