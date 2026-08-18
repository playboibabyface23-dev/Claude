# Futures Trading Agent

An autonomous futures trading agent: Tradovate/TradersPost execution, a
Claude decision engine, and a deterministic risk manager that has the only
say over what actually gets sized and sent. Runs continuously, is monitored
from Windows PowerShell, and never places an order the risk manager hasn't
independently approved.

**Core design principle: Claude never sends orders.** `ai/engine.py` returns
a `AIDecision` JSON object — action, confidence, entry reason, and
stop/target as point *distances*. `risk/manager.py` re-validates it,
applies every hard cap in code, and computes position size itself; only an
approved, sized decision ever reaches `execution/engine.py`.

## Architecture

```
Market data → Indicators → Claude decision engine → Risk manager → Execution
   (market/)   (market/)         (ai/)                 (risk/)      (execution/)
                                                                          │
                                                            Tradovate ────┼──── TradersPost
                                                                          │
                                                                     Database (database/)
                                                                          │
                                                              Dashboard (dashboard/)
```

## Components

| Folder | What it does |
|---|---|
| `config/` | Typed settings from `.env`, futures contract specs (MNQ/NQ/MES/ES/GC) |
| `market/` | Tradovate REST connector, tick→candle aggregation, session/prior-day levels, indicators (EMA/ATR/RSI/VWAP/relative volume/structure) |
| `ai/` | The Claude decision engine — one structured-output call per symbol per cycle |
| `strategies/` | Deterministic reference strategy (confluence counting) used **only** by the backtester as a stand-in for Claude |
| `risk/` | Hard-capped risk manager: confidence floor, daily loss, trades/day, drawdown, open positions, position sizing, kill switch — plus an optional Lucid/prop-firm eval guard (EOD trailing drawdown, consistency rule) |
| `execution/` | Tradovate order placement and the TradersPost webhook client, validated and duplicate-safe; `main.py` reconciles closed positions back into the database (see below) |
| `database/` | SQLite: trades, AI decision log, rejections, high-water mark, daily equity — all scoped per trading account (see Multi-account trading below) |
| `dashboard/` | stdlib HTTP dashboard: positions, balance, P&L, win rate, confidence history, trade history, per-account breakdown |
| `notifications/` | External alerting (generic Slack/Discord-compatible webhook) for kill-switch trips, execution failures, and crashes — off unless `ALERT_WEBHOOK_URL` is set |
| `news/` | Finnhub adapter for market headlines and the high-impact economic calendar — used only by the scanner (see below), never by the live-trading `ai/engine.py` |
| `scanner/` | Advisory-only market scanner: scans every symbol against price action *and* news, and notifies (never trades) when Claude thinks a symbol deserves a closer look. See "Scanner" below |
| `backtesting/` | Replays history through the real risk manager and position management |
| `powershell/` | `start_agent.ps1`, `restart_agent.ps1`, `watchdog.ps1`, `emergency_stop.ps1`, `update_agent.ps1` |

## Live market data

`market/tradovate_ws.py` implements Tradovate's real-time WebSocket feed —
the piece originally left out because this session had no way to verify the
wire format. It has since been fetched and confirmed directly from
Tradovate's own tutorial repository (`tradovate/example-api-js`,
`tutorial/WebSockets/EX-05` through `EX-10`), and the core of it was
confirmed **live** against the real service during development:

- Connecting to `wss://md.tradovateapi.com/v1/websocket` returns the
  documented `'o'` open frame immediately.
- Sending an `authorize` request (in the documented
  `endpoint\nid\nquery\nbody` format) with a deliberately invalid token got
  back a real `{"s": <non-200>, "i": 0, "d": "Access is denied"}` rejection —
  i.e. the frame protocol, the request/response envelope, and the auth
  round-trip are confirmed against production, not just documented.

What is **not** yet re-verified live (no valid demo account was available in
that session): the actual push-event shapes of `md/subscribequote` and
`md/getchart` once authorized. `TradovateLiveFeed` combines historical
warmup (`get_chart`) with live tick aggregation (`subscribe_quote` →
`CandleAggregator`) to keep each traded symbol's candle history current —
wire up real demo credentials and watch its output before trusting it with
a funded account.

`TradovateLiveFeed` also watches its own connection: a background watchdog
(`market/tradovate_ws.py`) checks every 10 seconds whether the shared
WebSocket has dropped and, if so, reconnects and re-subscribes every symbol
that was active — without this, a single network blip during a 24/7 run
would have silently stopped new bars from arriving for the rest of the
process's life, with nothing surfacing the failure. A brief gap in bars
during the outage itself is expected and not backfilled.

When Tradovate credentials are configured, `main.py`'s `Agent` builds a
`TradovateLiveFeed` automatically — no separate flag needed. Without them,
set `HISTORICAL_BARS_CSV_TEMPLATE` to poll a CSV each cycle instead
(paper/demo runs only, never a substitute for the live feed in production).
`market/data.py`'s `TickSource`/`HistoricalBarsProvider` seam means any
other live feed (a different vendor, a different protocol) can be wired in
the same way without touching risk, execution, or the database.

`market/tradovate.py`'s REST auth, account, position, and order endpoints
follow Tradovate's long-stable list/item/find/placeorder conventions and are
covered by tests, but were not separately re-verified live in this session;
run `TradovateClient.check_connection()` against demo before trusting them
with a real account.

## Multi-account trading

The agent can trade N broker accounts at once from one process. One shared
market-data feed and one shared Claude decision per symbol per cycle are
fanned out to every configured account — each gets its own `RiskManager`,
its own `ExecutionEngine`, its own broker client(s), its own kill-switch
file, and its own optional Lucid eval guard, so sizing, risk gating, and
order placement are fully independent per account. **Claude never sees or
picks an account — only a symbol** — the same "Claude never sends orders"
invariant from the top of this file applies per account, not just once.

Configure it with indexed `ACCOUNT_1_*`, `ACCOUNT_2_*`, ... env vars (see
`.env.example` for the full field list per account — execution mode,
Tradovate/TradersPost credentials, risk limits, Lucid settings). Indexes
can be sparse, up to 20 accounts. **Leave every `ACCOUNT_N_*` var unset to
keep the single legacy account** built from the existing top-level fields
(`TRADOVATE_*`, `EXECUTION_MODE`, `ACCOUNT_EQUITY`, ...) — an existing
single-account `.env` file needs no changes.

Every trade, rejection, and equity snapshot in the database is tagged with
the account name that produced it (`database/db.py`'s `account` column),
so daily-loss limits, trades-per-day caps, drawdown, and open-position
counts are all counted per account, never pooled across accounts. A
database created before multi-account support existed is migrated in
place the first time it's opened — every pre-existing row is treated as
belonging to the `default` account, with no data loss and no separate
migration step to run by hand. The dashboard's `Accounts` section (see
below) shows the live per-account breakdown alongside the existing
aggregate tiles.

**Position reconciliation is per account too** (see below): each
account's open trades are checked only against that same account's own
broker connection, never against a different account's. An account with
Tradovate credentials configured reconciles normally; a TradersPost-only
account (no Tradovate credentials at all) has no broker-side read API and
is skipped for that account specifically — the same limitation the
single-account path has always had, just scoped per account instead of
globally.

## Setup

```bash
cd futures-trading-agent
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env   # fill in your keys — start with TRADOVATE_ENV=demo
```

## Running

```bash
python -m futures_agent.main
```

Starts the continuous loop and the dashboard (`http://127.0.0.1:8788` by
default). On Windows, use the PowerShell scripts instead so the process
survives logoff and restarts automatically:

```powershell
powershell/start_agent.ps1       # launch in the background, records the PID
powershell/watchdog.ps1          # run continuously — restarts the agent if it dies
powershell/restart_agent.ps1     # stop + start (e.g. after a config change)
powershell/update_agent.ps1      # git pull + reinstall dependencies
powershell/emergency_stop.ps1    # trips the kill switch and stops the process NOW
```

`emergency_stop.ps1` does **not** close open broker positions — verify and
close those manually in Tradovate/TradersPost.

## Scanner

```bash
python -m futures_agent.scanner
```

A second, independent process from `python -m futures_agent.main` above.
Where the trading agent's `ai/engine.py` is deliberately kept blind to news
(see the design principle at the top of this file — Claude never sends
orders, so it never needs an excuse to jump the risk manager), the scanner
never sends an order at all, so there's no equivalent reason to keep it
blind. Each scan cycle:

1. Pulls recent market headlines and the next high-impact US economic event
   from Finnhub (`news/finnhub_news.py`) — skipped, not fatal, when
   `FINNHUB_API_KEY` isn't set; the scanner still runs on price action
   alone.
2. Refreshes candles/indicators/session levels for every symbol in
   `SCAN_SYMBOLS` (default: every symbol in `config/symbols.SYMBOL_CATALOG`
   — "scan everything" — independent of `TRADED_SYMBOLS`, which only
   affects the live-trading agent).
3. Asks Claude for one BUY/SELL/HOLD + confidence + reasoning per symbol
   (`scanner/engine.py`'s `ScanDecisionEngine`), explicitly weighing the
   news/event context alongside the technicals.
4. Posts a notification — the same Slack/Discord-compatible webhook
   mechanism as `ALERT_WEBHOOK_URL` (`SCAN_ALERT_WEBHOOK_URL`, or falls
   back to `ALERT_WEBHOOK_URL` if unset) — whenever a symbol's action isn't
   HOLD and its confidence clears `SCAN_MIN_CONFIDENCE` (default 70), with
   a `SCAN_ALERT_COOLDOWN_MINUTES` cooldown per symbol so a persistent
   setup doesn't re-notify every cycle.

**This is advisory only.** Nothing in `scanner/` calls `risk/manager.py` or
`execution/engine.py` — no confidence score, however high, results in an
order. A notification means "Claude thinks this is worth a look," not
"trade this." Decide for yourself, same as reading any other analysis.

See `.env.example`'s Scanner section for every `SCAN_*`/`FINNHUB_API_KEY`
variable. Point `SCAN_ALERT_WEBHOOK_URL` (or `ALERT_WEBHOOK_URL`) at a
Slack/Discord incoming webhook, or a generic HTTP relay (e.g. a service
that turns a webhook POST into a phone push notification) to actually get
notified on your phone.

## Backtesting

```bash
python -m futures_agent.backtesting --symbol MNQ --csv bars.csv
```

Drives the real `RiskManager` and position-management logic over historical
bars with the deterministic reference strategy in `strategies/` standing in
for Claude. **This is not a backtest of the AI** — Claude will read the same
indicators differently. What it does prove: position sizing, the risk
checklist, and stop/target management all work as they would live. Bias
controls: no look-ahead (`candles[:i+1]` only), fills at the next bar's
open, a bar that spans both stop and target resolves as a loss
(`ambiguous_exit`), and positions still open at the end of the data are
never counted as a win.

**Cost modeling is opt-in and 0 by default**, which makes an unconfigured
run frictionless and overstate edge — the report says so explicitly when
nothing is set:

```bash
python -m futures_agent.backtesting --symbol MNQ --csv bars.csv \
  --commission-per-contract 4.50 --slippage-ticks 1
```

`--slippage-ticks` is applied against you on entries and stop-loss exits
(both effectively market fills once triggered) but never on target exits,
which are modeled as limit fills at exactly the target price. There's no
universally "correct" slippage number — it depends on the contract's
liquidity and your size — so treat this as a knob to stress-test results
against, not a validated figure. `--commission-per-contract` is a
round-trip total charged once per closed trade. The report and
`metrics()` break commission and slippage out from net P&L separately so
their impact stays visible instead of disappearing into one number.

## Position reconciliation

Nothing else in this file ever calls `db.close_trade()` or
`execution.submit_exit()` — a bracket/OCO stop or target filled at the
broker (the TradersPost path, or a fill this process didn't itself
observe on Tradovate) would otherwise leave the trade recorded `open` in
the database forever. With the default `MAX_OPEN_POSITIONS=1`, that means
the agent trades exactly once and then silently refuses every signal
after, permanently. `Agent.reconcile_positions()` runs every cycle: it
checks the broker's actual net position (via Tradovate's REST API, shared
between execution and market data — see `main.py`) for every symbol with
an open DB trade, and closes it in the database once the broker shows
flat. The exit price used for P&L is the latest known candle close, not a
broker-confirmed fill price — Tradovate's fill/order-history endpoints
weren't something this session could verify live — so it's logged and
alerted as an **estimate**, and recorded in the `rejections` table as
`position_reconciled` for audit. Always verify the real fill against the
broker. This only works when a Tradovate REST session exists (which
`.env.example` already requires for live market data); the CSV-fallback
paper path has no way to know broker state and isn't a substitute for
this in production.

## Tests

```bash
python -m pytest tests/ -v      # 361 tests, fully offline — no API keys or network required
```

Every external integration (Tradovate, TradersPost, Claude) is exercised
against an injected fake or an `httpx.MockTransport`, never a live call.
`main.py`'s `Agent` class accepts injectable `bars_provider`,
`tradovate_client`, `traderspost_client`, and `ai_client`, so the full
market-data → AI → risk → execution → database cycle can be tested
end-to-end without touching the network (see `tests/test_main.py`).

## Safety model

1. **Deterministic risk gate** — every cap in `RiskLimits` (confidence
   floor, daily loss, trades/day, drawdown, open positions) is enforced in
   plain Python after the AI responds. A decision that fails even one check
   is rejected outright, never sized down and let through partially.
2. **Structured output, revalidated** — Claude must return JSON matching a
   fixed schema; `AIDecision.from_json` re-validates every field regardless
   (missing fields, out-of-range confidence, non-positive stop/target).
   `risk/manager.py` has its own independent `evaluate_raw()` gate for
   malformed output too — two layers refusing bad input on purpose.
3. **Refusal handling** — a Claude response is checked for
   `stop_reason == "refusal"` before its content is touched, with a
   server-side model fallback configured.
4. **Kill switch** — a sentinel file (`logs/KILL_SWITCH` by default) that
   `emergency_stop.ps1` can create by hand, and that the risk manager also
   trips automatically on a daily-loss or drawdown breach. Checked first, on
   every decision, before anything else.
5. **Duplicate-order prevention** — `execution/engine.py` refuses to resend
   an identifier it has already submitted, backed by the database (not just
   process memory) via an injectable `duplicate_check`.
6. **Lucid/prop-firm eval guard (optional)** — `risk/lucid_eval.py`, off by
   default (`LUCID_EVAL_ENABLED=false`). When enabled, `main.py` records a
   closing equity snapshot every cycle (`database/db.py`'s `daily_equity`
   table) and checks it every cycle against an EOD trailing-drawdown floor
   and a consistency-rule cap — a breach trips the same kill switch as the
   generic risk caps above. **The trailing-drawdown amount is taken from a
   real Lucid account summary; the consistency percentage is an
   unconfirmed placeholder.** Read that module's docstring and confirm
   every threshold against Lucid's actual published rules before this is
   trusted with a real evaluation fee.

7. **External alerting (optional)** — `notifications/alerts.py`, off unless
   `ALERT_WEBHOOK_URL` is set. Posts a kill-switch trip (generic or Lucid),
   an execution failure, an unhandled per-cycle crash, and process
   start/stop to a generic webhook (Slack- and Discord-compatible
   `{"text": ...}` payload) so an unattended run surfaces problems
   immediately instead of only in a log file nobody's watching. A delivery
   failure here is always swallowed — it never takes down the trading loop
   that triggered it.

> Research and paper-trading scaffold. Not financial advice. Confirm
> `TRADOVATE_ENV=demo` and test thoroughly before ever pointing this at a
> funded account.
