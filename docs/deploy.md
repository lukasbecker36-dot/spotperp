# Deploying to the Hetzner server

Target layout: `/opt/basis-trade/` (same pattern as hyperaster).

## First-time setup

```bash
# as root or with sudo
mkdir -p /opt/basis-trade
cd /opt/basis-trade
git clone https://github.com/lukasbecker36-dot/spotperp.git .

python3.11 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
chmod 600 .env
# edit .env with real keys

mkdir -p data output
echo "TRADING_MODE=paper" > data/mode.env

cp deploy/basis-trade.service /etc/systemd/system/
cp deploy/basis-trade-control.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now basis-trade.service basis-trade-control.service
```

### Log rotation (gzip old basis logs)

The engine writes `output/basis_log_YYYYMMDD.csv` every `BASIS_LOG_SECONDS`
and never rotates them (~15-18 MB/day, unbounded). A daily timer gzips every
day except today's live file (~5-10x smaller); the backtest readers handle
`.csv` and `.csv.gz` transparently.

```bash
cp deploy/basis-trade-logrotate.service /etc/systemd/system/
cp deploy/basis-trade-logrotate.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now basis-trade-logrotate.timer

systemctl start basis-trade-logrotate.service   # gzip existing old days now
systemctl list-timers basis-trade-logrotate     # confirm it's scheduled
```

### AI advisor (optional, advisory only)

A Claude review of open positions (basis / funding / liq proximity) and the top
funding opportunities, delivered to Telegram. It only ever sends messages — it
cannot place, size or close orders. Trigger on demand with `/review`, or on a
schedule via the timer below (07:30 UK then every 4h to 23:30 — 07:30, 11:30,
15:30, 19:30, 23:30 — nothing overnight). Requires `ANTHROPIC_API_KEY` in `.env` and
outbound HTTPS to `api.anthropic.com`; without the key it simply replies that it
is not configured.

```bash
cp deploy/basis-trade-advisor.service /etc/systemd/system/
cp deploy/basis-trade-advisor.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now basis-trade-advisor.timer

.venv/bin/python advisor.py --force              # send one review now (test)
journalctl -u basis-trade-advisor -n 20         # check a scheduled run
systemctl list-timers basis-trade-advisor       # timer wakes hourly at :30
```

The timer wakes every hour at :30 and `advisor.py` self-gates to the UK run
schedule (`ADVISOR_TIMEZONE` / `ADVISOR_RUN_HOURS`, default 07/11/15/19/23),
so it works on any systemd version, follows DST via the tz database, and needs
nothing special from the box's own clock. Most hourly wakes log "outside run
window" and exit. Use `advisor.py --force` to bypass the gate for a manual test;
`/review` in Telegram is always ungated.

If the bot runs as a non-root user, allow it to control the engine service
without a password (needed for `/start`, `/stop`, `/restart`, `/paper`, `/live`):

```
# /etc/sudoers.d/basis-trade
botuser ALL=(root) NOPASSWD: /usr/bin/systemctl start basis-trade.service, \
    /usr/bin/systemctl stop basis-trade.service, \
    /usr/bin/systemctl restart basis-trade.service
```

## Verifying the install

```bash
cd /opt/basis-trade
.venv/bin/python scripts/probe_mexc.py     # auth + market data + balances
.venv/bin/python scripts/probe_aster.py    # auth + market data + positions
journalctl -u basis-trade -f               # engine logs
```

Then from Telegram: `/status`, `/screen`.

## Updating

```bash
cd /opt/basis-trade
git pull
.venv/bin/pip install -e .
systemctl restart basis-trade.service basis-trade-control.service
```

## Going live

1. Run paper mode for several days; compare `/screen` edges with paper `/pnl`.
2. Verify probe scripts can place + cancel a far-from-market order on both venues.
3. `/live YES` from Telegram (writes `data/mode.env`, restarts the engine).
4. Start with small notional, e.g. `/enter BTC 100`.
