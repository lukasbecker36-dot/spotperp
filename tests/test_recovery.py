"""Startup reconciliation: the stale-order sweep must cancel our own entry/exit
maker orders but never touch protective /stops or manual orders."""
from decimal import Decimal

import database
import position_manager as pm
import recovery


class _Order:
    def __init__(self, oid, cid, executed=Decimal(0), status="NEW"):
        self.order_id = oid
        self.client_order_id = cid
        self.executed_qty = executed
        self.status = status
        self.avg_price = Decimal(0)
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
