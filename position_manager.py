"""Position state machine, fill recording and P&L.

States:
    PENDING_ENTRY -> ENTERING -> OPEN -> EXITING -> CLOSED
                       |  \\-> CANCELLED (nothing hedged)
                       \\--> UNWINDING -> CLOSED (leg risk unwind)

A premium position is short `perp_qty` on Aster and long `spot_qty` on MEXC
(quantities in MEXC base units; Aster contract qty = spot qty / multiplier).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal

PENDING_ENTRY = "PENDING_ENTRY"
ENTERING = "ENTERING"
OPEN = "OPEN"
EXITING = "EXITING"
UNWINDING = "UNWINDING"
CLOSED = "CLOSED"
CANCELLED = "CANCELLED"

ACTIVE_STATES = (PENDING_ENTRY, ENTERING, OPEN, EXITING, UNWINDING)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dec(value, default: str = "0") -> Decimal:
    return Decimal(value) if value not in (None, "") else Decimal(default)


@dataclass
class Position:
    id: int
    symbol: str
    direction: str
    state: str
    paper: bool
    target_notional: Decimal
    perp_qty: Decimal
    spot_qty: Decimal
    entry_basis_bps: Decimal | None
    exit_mode: str | None
    exit_target_bps: Decimal | None
    exit_target_qty: Decimal | None   # perp qty to stop at (partial exit floor)
    perp_entry_avg: Decimal | None
    spot_entry_avg: Decimal | None
    perp_exit_avg: Decimal | None
    spot_exit_avg: Decimal | None
    fees_usd: Decimal
    funding_usd: Decimal
    realized_pnl_usd: Decimal | None
    unwind_pnl_usd: Decimal
    opened_ms: int | None
    closed_ms: int | None
    created_ms: int
    min_entry_bps: Decimal | None
    trade_kind: str          # 'convergence' (may auto-close) | 'carry' (manual only)
    note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Position":
        def opt(name: str) -> Decimal | None:
            v = row[name]
            return Decimal(v) if v not in (None, "") else None

        return cls(
            id=row["id"],
            symbol=row["symbol"],
            direction=row["direction"],
            state=row["state"],
            paper=bool(row["paper"]),
            target_notional=_dec(row["target_notional"]),
            perp_qty=_dec(row["perp_qty"]),
            spot_qty=_dec(row["spot_qty"]),
            entry_basis_bps=opt("entry_basis_bps"),
            exit_mode=row["exit_mode"],
            exit_target_bps=opt("exit_target_bps"),
            exit_target_qty=opt("exit_target_qty"),
            perp_entry_avg=opt("perp_entry_avg"),
            spot_entry_avg=opt("spot_entry_avg"),
            perp_exit_avg=opt("perp_exit_avg"),
            spot_exit_avg=opt("spot_exit_avg"),
            fees_usd=_dec(row["fees_usd"]),
            funding_usd=_dec(row["funding_usd"]),
            realized_pnl_usd=opt("realized_pnl_usd"),
            unwind_pnl_usd=_dec(row["unwind_pnl_usd"]),
            opened_ms=row["opened_ms"],
            closed_ms=row["closed_ms"],
            created_ms=row["created_ms"],
            min_entry_bps=opt("min_entry_bps"),
            trade_kind=(row["trade_kind"] or "convergence"),
            note=row["note"],
        )


class PositionManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ── creation / lookup ──

    def create(
        self, symbol: str, notional: Decimal, *, paper: bool,
        direction: str = "premium", min_entry_bps: Decimal | None = None,
        trade_kind: str = "convergence",
    ) -> Position:
        now = _now_ms()
        cur = self._conn.execute(
            "INSERT INTO positions (symbol, direction, state, paper, target_notional,"
            " min_entry_bps, trade_kind, created_ms, updated_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, direction, PENDING_ENTRY, int(paper), str(notional),
             str(min_entry_bps) if min_entry_bps is not None else None,
             trade_kind, now, now),
        )
        self._conn.commit()
        return self.get(int(cur.lastrowid))

    def get(self, position_id: int) -> Position:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"position {position_id} not found")
        return Position.from_row(row)

    def active(self) -> list[Position]:
        rows = self._conn.execute(
            f"SELECT * FROM positions WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})"
            " ORDER BY id",
            ACTIVE_STATES,
        ).fetchall()
        return [Position.from_row(r) for r in rows]

    def closed(self, limit: int = 10) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE state IN (?, ?) ORDER BY id DESC LIMIT ?",
            (CLOSED, CANCELLED, limit),
        ).fetchall()
        return [Position.from_row(r) for r in rows]

    # ── mutation ──

    def set_state(self, position_id: int, state: str, note: str | None = None) -> None:
        now = _now_ms()
        sets = ["state=?", "updated_ms=?"]
        args: list = [state, now]
        if state == OPEN:
            sets.append("opened_ms=COALESCE(opened_ms, ?)")
            args.append(now)
        if state in (CLOSED, CANCELLED):
            sets.append("closed_ms=?")
            args.append(now)
        if note is not None:
            sets.append("note=?")
            args.append(note)
        args.append(position_id)
        self._conn.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE id=?", args
        )
        self._conn.commit()

    def set_exit_request(
        self, position_id: int, mode: str | None, target_bps: Decimal | None,
        target_qty: Decimal | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE positions SET exit_mode=?, exit_target_bps=?, exit_target_qty=?,"
            " updated_ms=? WHERE id=?",
            (
                mode,
                str(target_bps) if target_bps is not None else None,
                str(target_qty) if target_qty is not None else None,
                _now_ms(),
                position_id,
            ),
        )
        self._conn.commit()

    def add_target_notional(self, position_id: int, amount_usd: Decimal) -> None:
        """Grow the target notional when sizing up an existing position."""
        pos = self.get(position_id)
        self._conn.execute(
            "UPDATE positions SET target_notional=?, updated_ms=? WHERE id=?",
            (str(pos.target_notional + amount_usd), _now_ms(), position_id),
        )
        self._conn.commit()

    def set_min_entry_bps(self, position_id: int, min_bps: Decimal | None) -> None:
        """Update the basis floor used while an entry/add works."""
        self._conn.execute(
            "UPDATE positions SET min_entry_bps=?, updated_ms=? WHERE id=?",
            (str(min_bps) if min_bps is not None else None, _now_ms(), position_id),
        )
        self._conn.commit()

    def add_funding(self, position_id: int, amount_usd: Decimal) -> None:
        pos = self.get(position_id)
        self._conn.execute(
            "UPDATE positions SET funding_usd=?, updated_ms=? WHERE id=?",
            (str(pos.funding_usd + amount_usd), _now_ms(), position_id),
        )
        self._conn.commit()

    def set_funding(self, position_id: int, amount_usd: Decimal) -> None:
        """Set absolute realised funding (from Aster income history)."""
        self._conn.execute(
            "UPDATE positions SET funding_usd=?, updated_ms=? WHERE id=?",
            (str(amount_usd), _now_ms(), position_id),
        )
        self._conn.commit()

    def record_fill(
        self,
        position_id: int,
        venue: str,
        phase: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        fee_usd: Decimal,
        order_id: str | None = None,
    ) -> None:
        """Insert the fill and fold it into the position's qty/avg/fee columns."""
        self._conn.execute(
            "INSERT INTO fills (position_id, venue, phase, side, qty, price, fee_usd,"
            " order_id, ts_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position_id,
                venue,
                phase,
                side,
                str(qty),
                str(price),
                str(fee_usd),
                order_id,
                _now_ms(),
            ),
        )
        pos = self.get(position_id)

        def update_avg(
            avg: Decimal | None, held: Decimal, fill_qty: Decimal, fill_px: Decimal
        ) -> Decimal:
            if avg is None or held == 0:
                return fill_px
            return (avg * held + fill_px * fill_qty) / (held + fill_qty)

        sets: dict[str, str] = {"fees_usd": str(pos.fees_usd + fee_usd)}
        if phase == "entry":
            # Weight the entry average by the qty of PRIOR entry fills, not the
            # net held qty. Using net qty (reduced by an intervening partial
            # exit) skews the average when a position is entered, partially
            # exited, then added to — and finalize_pnl multiplies by the total
            # entry qty, so the mismatch double-counts the partial-exit profit.
            if venue == "aster":
                prior = self._phase_qty(position_id, "aster", "entry") - qty
                sets["perp_entry_avg"] = str(
                    update_avg(pos.perp_entry_avg, prior, qty, price)
                )
                sets["perp_qty"] = str(pos.perp_qty + qty)
            else:
                prior = self._phase_qty(position_id, "mexc", "entry") - qty
                sets["spot_entry_avg"] = str(
                    update_avg(pos.spot_entry_avg, prior, qty, price)
                )
                sets["spot_qty"] = str(pos.spot_qty + qty)
        elif phase == "unwind":
            # An unwind cancels an entry increment that was never hedged, so
            # the whole leg has to be re-derived: the reversed entry leaves the
            # entry average and the buy-back never enters the exit average.
            # Incremental folding cannot express that.
            self._conn.execute(
                "UPDATE positions SET updated_ms=? WHERE id=?",
                (_now_ms(), position_id),
            )
            self._conn.commit()
            self.recompute_from_fills(position_id)
            return
        else:  # exit reduces the held quantities
            if venue == "aster":
                prev_exited_qty = self._exited_qty(position_id, "aster", exclude_last=True)
                sets["perp_exit_avg"] = str(
                    update_avg(pos.perp_exit_avg, prev_exited_qty, qty, price)
                )
                sets["perp_qty"] = str(pos.perp_qty - qty)
            else:
                prev_exited_qty = self._exited_qty(position_id, "mexc", exclude_last=True)
                sets["spot_exit_avg"] = str(
                    update_avg(pos.spot_exit_avg, prev_exited_qty, qty, price)
                )
                sets["spot_qty"] = str(pos.spot_qty - qty)

        assignments = ", ".join(f"{k}=?" for k in sets)
        self._conn.execute(
            f"UPDATE positions SET {assignments}, updated_ms=? WHERE id=?",
            (*sets.values(), _now_ms(), position_id),
        )
        self._conn.commit()

    def _exited_qty(
        self, position_id: int, venue: str, exclude_last: bool = False
    ) -> Decimal:
        """Quantity CLOSED out of a real position.

        Deliberately excludes 'unwind'. An unwind buys back a perp increment
        that was never hedged, so it cancels an ENTRY rather than closing a
        position — counting it here would price the exit off a trade that
        closed nothing (see _derive_legs).
        """
        rows = self._conn.execute(
            "SELECT qty FROM fills WHERE position_id=? AND venue=? AND phase='exit'"
            " ORDER BY id",
            (position_id, venue),
        ).fetchall()
        if exclude_last and rows:
            rows = rows[:-1]
        return sum((Decimal(r["qty"]) for r in rows), Decimal(0))

    def update_fill(
        self, fill_id: int, price: Decimal, fee_usd: Decimal, order_id: str
    ) -> None:
        """Correct one recorded fill in place. Used to replace a SYNTHETIC
        price (a close booked at mark because the real order could not be
        looked up) with the venue's own trade record."""
        self._conn.execute(
            "UPDATE fills SET price=?, fee_usd=?, order_id=? WHERE id=?",
            (str(price), str(fee_usd), order_id, fill_id),
        )
        self._conn.commit()

    def recorded_qty_for_order(self, position_id: int, order_id: str) -> Decimal:
        """How much of a venue order this position has already booked.

        The executor records a resting order's fills incrementally as it polls,
        so a crash can leave PART of an order recorded. Recovery needs the
        delta, not the total, or it double-counts what was already there."""
        rows = self._conn.execute(
            "SELECT qty FROM fills WHERE position_id=? AND order_id=?",
            (position_id, str(order_id)),
        ).fetchall()
        return sum((Decimal(r["qty"]) for r in rows), Decimal(0))

    def _derive_legs(self, position_id: int, venue: str) -> dict:
        """Aggregate one leg from its fills, with unwinds cancelling entries.

        An unwind buys back a perp increment whose spot hedge was never
        bought. It is neither an entry nor an exit:

        - leaving its ENTRY fill in the entry average prices a leg that is not
          held and has no spot partner against it, dragging the recorded entry
          basis toward a phantom (GUSDT showed -100bps);
        - putting the buy-back in the EXIT average prices the close of the
          position off a trade that closed nothing.

        So each unwound quantity is matched off against entry fills LIFO — an
        unwind always reverses the increment that just filled — and both sides
        drop out of the averages. The price difference is real money and is
        returned as unwind_pnl (short: sold at the entry price, bought back at
        the unwind price).

        Derived from the fills table rather than tracked incrementally, so it
        is the same answer however many unwinds happen and in what order.
        """
        rows = self._conn.execute(
            "SELECT phase, qty, price, fee_usd FROM fills"
            " WHERE position_id=? AND venue=? ORDER BY id",
            (position_id, venue),
        ).fetchall()
        fees = sum((Decimal(r["fee_usd"] or "0") for r in rows), Decimal(0))

        # Walk the fills IN ORDER, matching each unwind against the entries
        # that preceded it, newest first. Matching globally instead — all the
        # unwind quantity against all the entries — silently reverses clips
        # that filled AFTER the unwind and were hedged and kept, which is how
        # GUSDT #195 reported a -533bps blended entry basis: a later unwind
        # re-matched and ate the good clips, leaving the average on the oldest
        # fills. An unwind can only undo something that already happened.
        open_entries: list[list[Decimal]] = []   # [qty, price], oldest first
        exits: list[tuple[Decimal, Decimal]] = []
        reversed_qty = reversed_cost = Decimal(0)
        unwind_cost = unmatched = Decimal(0)
        for r in rows:
            qty, price = Decimal(r["qty"]), Decimal(r["price"])
            if r["phase"] == "entry":
                open_entries.append([qty, price])
            elif r["phase"] == "unwind":
                unwind_cost += qty * price
                need = qty
                while need > 0 and open_entries:
                    take = min(open_entries[-1][0], need)
                    open_entries[-1][0] -= take
                    need -= take
                    reversed_qty += take
                    reversed_cost += take * open_entries[-1][1]
                    if open_entries[-1][0] <= 0:
                        open_entries.pop()
                unmatched += need   # unwound more than was ever entered
            else:
                exits.append((qty, price))
        in_qty = sum((q for q, _ in open_entries), Decimal(0)) - unmatched
        in_cost = sum((q * p for q, p in open_entries), Decimal(0))
        out_qty = sum((q for q, _ in exits), Decimal(0))
        out_cost = sum((q * p for q, p in exits), Decimal(0))
        # Short leg: sold at the entry price, bought back at the unwind price.
        unwind_pnl = reversed_cost - unwind_cost if venue == "aster" else (
            unwind_cost - reversed_cost
        )
        kept_qty = sum((q for q, _ in open_entries), Decimal(0))
        return {
            "entry_avg": in_cost / kept_qty if kept_qty > 0 else None,
            "exit_avg": out_cost / out_qty if out_qty > 0 else None,
            "qty": in_qty - out_qty,
            "fees": fees,
            "unwind_pnl": unwind_pnl,
        }

    def recompute_from_fills(self, position_id: int) -> None:
        """Rebuild qty, averages and fees for both legs from the fills table.

        record_fill folds each fill in incrementally, so correcting one after
        the fact would leave the stored averages carrying the old price. This
        recomputes them from scratch."""
        sets: dict[str, str] = {}
        total_fees = Decimal(0)
        total_unwind_pnl = Decimal(0)
        for venue, avg_in, avg_out, qty_col in (
            ("aster", "perp_entry_avg", "perp_exit_avg", "perp_qty"),
            ("mexc", "spot_entry_avg", "spot_exit_avg", "spot_qty"),
        ):
            leg = self._derive_legs(position_id, venue)
            if leg["entry_avg"] is not None:
                sets[avg_in] = str(leg["entry_avg"])
            if leg["exit_avg"] is not None:
                sets[avg_out] = str(leg["exit_avg"])
            sets[qty_col] = str(leg["qty"])
            total_fees += leg["fees"]
            total_unwind_pnl += leg["unwind_pnl"]
        sets["fees_usd"] = str(total_fees)
        sets["unwind_pnl_usd"] = str(total_unwind_pnl)
        assignments = ", ".join(f"{k}=?" for k in sets)
        self._conn.execute(
            f"UPDATE positions SET {assignments}, updated_ms=? WHERE id=?",
            (*sets.values(), _now_ms(), position_id),
        )
        self._conn.commit()

    def synthetic_fills(self, position_id: int) -> list[dict]:
        """Exit fills booked at mark rather than from a real order — the ADL /
        lost-stop path. These are the rows whose price is a guess."""
        return [
            dict(r) for r in self._conn.execute(
                "SELECT * FROM fills WHERE position_id=? AND order_id='ADL'"
                " ORDER BY id",
                (position_id,),
            )
        ]

    # ── P&L ──

    def finalize_pnl(self, position_id: int) -> Decimal:
        """Compute realised P&L from fills + funding - fees and store it.

        Premium trade: perp pnl = (entry - exit) * qty (short), spot pnl =
        (exit - entry) * qty (long).
        """
        pos = self.get(position_id)
        perp_pnl = spot_pnl = Decimal(0)
        entry_qty_perp = self._phase_qty(position_id, "aster", "entry")
        exit_qty_perp = self._exited_qty(position_id, "aster")
        if pos.perp_entry_avg is not None and pos.perp_exit_avg is not None:
            qty = min(entry_qty_perp, exit_qty_perp)
            perp_pnl = (pos.perp_entry_avg - pos.perp_exit_avg) * qty
        entry_qty_spot = self._phase_qty(position_id, "mexc", "entry")
        exit_qty_spot = self._exited_qty(position_id, "mexc")
        if pos.spot_entry_avg is not None and pos.spot_exit_avg is not None:
            qty = min(entry_qty_spot, exit_qty_spot)
            spot_pnl = (pos.spot_exit_avg - pos.spot_entry_avg) * qty
        # Unwound clips closed at a price of their own; that money is real but
        # belongs to neither average (see _derive_legs).
        pnl = (
            perp_pnl + spot_pnl + pos.unwind_pnl_usd
            + pos.funding_usd - pos.fees_usd
        )
        self._conn.execute(
            "UPDATE positions SET realized_pnl_usd=?, updated_ms=? WHERE id=?",
            (str(pnl), _now_ms(), position_id),
        )
        self._conn.commit()
        return pnl

    def _phase_qty(self, position_id: int, venue: str, phase: str) -> Decimal:
        rows = self._conn.execute(
            "SELECT qty FROM fills WHERE position_id=? AND venue=? AND phase=?",
            (position_id, venue, phase),
        ).fetchall()
        return sum((Decimal(r["qty"]) for r in rows), Decimal(0))

    def leg_qtys(self, position_id: int) -> dict[str, Decimal]:
        """Entry and exit fill quantities per leg, for display/diagnosis. A
        healthy hedge has perp_entry == spot_entry and perp_exit == spot_exit
        (in matching units); a gap means the position carried naked delta."""
        return {
            "perp_entry": self._phase_qty(position_id, "aster", "entry"),
            "spot_entry": self._phase_qty(position_id, "mexc", "entry"),
            "perp_exit": self._exited_qty(position_id, "aster"),
            "spot_exit": self._exited_qty(position_id, "mexc"),
        }

    def pnl_summary(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        day_start_ms = (_now_ms() // 86_400_000) * 86_400_000
        for label, paper in (("live", 0), ("paper", 1)):
            total = self._conn.execute(
                "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) AS p"
                " FROM positions WHERE paper=? AND realized_pnl_usd IS NOT NULL",
                (paper,),
            ).fetchone()["p"]
            today = self._conn.execute(
                "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) AS p"
                " FROM positions WHERE paper=? AND realized_pnl_usd IS NOT NULL"
                " AND closed_ms >= ?",
                (paper, day_start_ms),
            ).fetchone()["p"]
            out[f"{label}_all_time"] = Decimal(str(total))
            out[f"{label}_today"] = Decimal(str(today))
        return out
