"""Startup reconciliation: the stale-order sweep must cancel our own entry/exit
maker orders but never touch protective /stops or manual orders."""
from decimal import Decimal

import database
import position_manager as pm
import recovery


class _Order:
    def __init__(self, oid, cid, executed=Decimal(0)):
        self.order_id = oid
        self.client_order_id = cid
        self.executed_qty = executed


class _Aster:
    def __init__(self, orders):
        self._orders = orders
        self.cancelled = []

    async def open_orders(self, symbol):
        return list(self._orders)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        return _Order(order_id, "", Decimal(0))

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
