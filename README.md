# spotperp — Aster perp ↔ MEXC spot basis trades

Captures perp-spot basis (cash-and-carry) trades: short Aster DEX perps and
buy MEXC spot when the perp trades at a premium, earning the basis as it
converges plus funding. See `CLAUDE.md` for the full design.

## v1 behaviour

- **Manual control from Telegram.** The engine never enters on its own: a
  continuously updated screener (`/screen`) ranks opportunities by executable
  net edge (touch prices, both spreads, fees, slippage buffer, funding carry)
  and you fire entries with `/enter SYMBOL NOTIONAL`.
- **Maker entry, taker hedge.** Entries rest a post-only (GTX) sell on the
  Aster perp joined to the best ask, repricing as the book moves; every fill
  increment is instantly hedged with a MEXC spot IOC buy. Failed hedges are
  unwound within a hard timeout.
- **Two exit modes.** `/exit ID now` crosses both legs immediately;
  `/exit ID passive [target_bps]` works a maker buy-back over time, only
  resting while the closeable basis is at or below your target.
- **Safety stops always on:** adverse-basis hard stop and max-hold timeout
  force an aggressive close even on manual positions.
- **Premium trades only** (short perp + long spot). Discount trades (margin
  short-sell on MEXC) are designed for but not implemented.
- **Paper mode by default**, with identical state machines and P&L accounting
  against live market data. `/live YES` switches to real orders.

## Layout

| File | Purpose |
|---|---|
| `live_monitor.py` | engine: market data, screener, command queue, safety stops |
| `executor.py` | entry/exit state machines, leg-risk handling, paper/live |
| `exchange_client.py` | Aster perp + MEXC spot async REST clients |
| `auth.py` | Aster EIP-712 agent signing, MEXC HMAC signing |
| `screener.py` | executable-basis math, ranking, snapshot |
| `position_manager.py` / `database.py` / `intents.py` | SQLite state, crash-safe intents |
| `recovery.py` | startup reconciliation |
| `control_bot.py` | Telegram bot (long polling, allowlist, YES confirms) |
| `scripts/probe_*.py` | live API validation, run before trading |
| `docs/deploy.md` | Hetzner/systemd deployment |

## Quick start (development)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
cp .env.example .env   # fill in keys
.venv/bin/python scripts/probe_mexc.py
.venv/bin/python scripts/probe_aster.py
.venv/bin/python live_monitor.py     # paper mode by default
.venv/bin/python control_bot.py
```
