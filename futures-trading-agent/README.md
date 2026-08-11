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
| `execution/` | Tradovate order placement and the TradersPost webhook client, validated and duplicate-safe |
| `database/` | SQLite: trades, AI decision log, rejections, high-water mark |
| `dashboard/` | stdlib HTTP dashboard: positions, balance, P&L, win rate, confidence history, trade history |
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

## Tests

```bash
python -m pytest tests/ -v      # 264 tests, fully offline — no API keys or network required
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

> Research and paper-trading scaffold. Not financial advice. Confirm
> `TRADOVATE_ENV=demo` and test thoroughly before ever pointing this at a
> funded account.
