"""Startup reconciliation: the stale-order sweep must cancel our own entry/exit
maker orders but never touch protective /stops or manual orders."""
from decimal import Decimal

import database
import position_manager as pm
import recovery


class _Order:
    def __init__(self, oid, cid, executed=Decimal(0), status="NEW",
                 side="BUY", avg_price=Decimal(0), symbol="BEATUSDT"):
        self.order_id = oid
        self.client_order_id = cid
        self.executed_qty = executed
        self.status = status
        self.side = side
        self.price = avg_price
        self.avg_price = avg_price
        self.symbol = symbol
        self.raw = {}

    @property
    def is_open(self):
        return self.status in ("NEW", "PARTIALLY_FILLED")


class _Aster:
    def __init__(self, orders, by_client=None):
        self._orders = orders
        self._by_client = by_client or {}
        self.cancelled = []

    async def open_orders(self, symbol):
        return list(self._orders)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        # The venue reports what the order had executed by the time it was
        # pulled; default to the stub's own figure so a test can set it.
        orig = next((o for o in self._orders if o.order_id == order_id), None)
        if orig is not None:
            return _Order(order_id, orig.client_order_id, orig.executed_qty,
                          "CANCELED", orig.side, orig.avg_price)
        return _Order(order_id, "", Decimal(0))

    async def get_order_by_client_id(self, symbol, client_order_id):
        return self._by_client.get(client_order_id)

    async def position_risk(self):
        return []


class _Mexc:
    async def account(self):
        return {"balances": []}


class _Notifier:
    def __init__(self):
        self.messages = []

    async def alert(self, m):
        self.messages.append(m)


async def test_recovery_sweep_preserves_stops_and_manual(tmp_path):
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    pos = positions.create("BEATUSDT", Decimal(100), paper=False)
    positions.set_state(pos.id, pm.OPEN)
    aster = _Aster([
        _Order("1", "sp_pent_5_123"),   # stale entry maker -> cancel
        _Order("2", "sp_pext_5_123"),   # stale passive-exit maker -> cancel
        _Order("3", "sp_stop_5_123"),   # protective stop -> KEEP
        _Order("4", "someManualOrder"),  # manual -> KEEP
    ])
    await recovery.reconcile(
        conn, positions, aster, _Mexc(), _Notifier(), paper=False
    )
    assert aster.cancelled == ["1", "2"]   # only our entry/exit makers
    conn.close()


async def test_recovery_reconciles_orphan_intent_order(tmp_path):
    """A crash left an unresolved intent for a maker order that actually rests
    on the venue, on a symbol with no active position (so the sweep misses it).
    Recovery looks it up by client id and cancels the orphan."""
    import intents
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    intents.record_intent(
        conn, 5, "aster", "place",
        {"symbol": "BEATUSDT", "side": "SELL", "client_order_id": "sp_pent_5_1"},
    )
    resting = _Order("111", "sp_pent_5_1", Decimal(0), "NEW")
    aster = _Aster([], by_client={"sp_pent_5_1": resting})
    await recovery.reconcile(
        conn, positions, aster, _Mexc(), _Notifier(), paper=False
    )
    assert aster.cancelled == ["111"]       # orphan resting order cancelled
    row = conn.execute(
        "SELECT status FROM intents WHERE position_id=5"
    ).fetchone()
    assert row["status"] == "reconciled"
    conn.close()


async def test_recovery_marks_never_landed_intent_failed(tmp_path):
    import intents
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    intents.record_intent(
        conn, 6, "aster", "place",
        {"symbol": "BEATUSDT", "side": "SELL", "client_order_id": "sp_pent_6_1"},
    )
    aster = _Aster([], by_client={})        # venue has no such order
    await recovery.reconcile(
        conn, positions, aster, _Mexc(), _Notifier(), paper=False
    )
    row = conn.execute(
        "SELECT status, result FROM intents WHERE position_id=6"
    ).fetchone()
    assert row["status"] == "failed" and "never landed" in row["result"]
    conn.close()


async def test_recovery_books_a_late_fill_on_a_swept_order(tmp_path):
    """Position 191: a restart mid-exit left our perp buy-back resting on
    Aster. It filled in the gap. Recovery cancelled it and only ALERTED, so the
    DB kept a perp leg the venue no longer had — and the hedge guard later read
    that gap as an ADL and force-sold the spot in 10% tranches."""
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    pos = positions.create("BEATUSDT", Decimal(100), paper=False)
    positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(5000), Decimal("0.01"),
        Decimal(0), order_id="E1",
    )
    positions.set_state(pos.id, pm.OPEN)
    aster = _Aster([
        _Order("9", f"sp_pext_{pos.id}_1", Decimal(2700), "PARTIALLY_FILLED",
               side="BUY", avg_price=Decimal("0.011")),
    ])
    notifier = _Notifier()
    await recovery.reconcile(conn, positions, aster, _Mexc(), notifier,
                             paper=False)
    after = positions.get(pos.id)
    assert after.perp_qty == Decimal(2300)          # 5000 - 2700, matches venue
    assert after.perp_exit_avg == Decimal("0.011")
    assert any("booked into position" in m for m in notifier.messages)
    conn.close()


async def test_recovery_books_only_the_unrecorded_delta(tmp_path):
    """The executor records a resting order's fills incrementally as it polls,
    so a crash can leave PART of an order already in the table. Booking the
    whole executed quantity again would double-count it."""
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    pos = positions.create("BEATUSDT", Decimal(100), paper=False)
    positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(5000), Decimal("0.01"),
        Decimal(0), order_id="E1",
    )
    # 1000 of the 2700 was already polled in before the crash, same order id.
    positions.record_fill(
        pos.id, "aster", "exit", "BUY", Decimal(1000), Decimal("0.011"),
        Decimal(0), order_id="9",
    )
    positions.set_state(pos.id, pm.OPEN)
    aster = _Aster([
        _Order("9", f"sp_pext_{pos.id}_1", Decimal(2700), "PARTIALLY_FILLED",
               side="BUY", avg_price=Decimal("0.011")),
    ])
    await recovery.reconcile(conn, positions, aster, _Mexc(), _Notifier(),
                             paper=False)
    assert positions.get(pos.id).perp_qty == Decimal(2300)   # not 1300
    conn.close()


async def test_recovery_ignores_a_fill_it_cannot_attribute(tmp_path):
    """A manual order carries no position in its client id. It must be reported,
    never guessed into someone's position."""
    conn = database.init_db(tmp_path / "t.db")
    positions = pm.PositionManager(conn)
    pos = positions.create("BEATUSDT", Decimal(100), paper=False)
    positions.record_fill(
        pos.id, "aster", "entry", "SELL", Decimal(5000), Decimal("0.01"),
        Decimal(0), order_id="E1",
    )
    positions.set_state(pos.id, pm.OPEN)
    aster = _Aster([_Order("9", "someManualOrder", Decimal(2700))])
    await recovery.reconcile(conn, positions, aster, _Mexc(), _Notifier(),
                             paper=False)
    assert positions.get(pos.id).perp_qty == Decimal(5000)   # untouched
    assert aster.cancelled == []                             # and not cancelled
    conn.close()
