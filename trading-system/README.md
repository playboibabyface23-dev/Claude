# Trading System — Structure-Aware AI Trading Pipeline

Market data flows through a deterministic price-action structure engine and an
indicator engine; a Claude Fable 5 multi-agent reasoning layer produces a
structured trade decision; a deterministic safety layer and execution validator
gate it; approved trades route to a broker through TradersPost. Open positions
are monitored continuously with a dynamic stop-loss engine, and every trade is
journaled for daily review.

**Claude never sends orders.** It emits a `TradeDecision` JSON object. Python
validates the schema, re-checks every risk rule, screens for duplicates, and
only then forwards to TradersPost.

## Architecture

```
Market Data
  TradingView (webhook alerts) · Polygon.io · Alpaca · Finnhub
                       │
             Structure Detection Engine
         (Python + Pine Script + AI Vision)
                       │
         ┌─────────────┴─────────────┐
 Price Action AI              Indicator Engine
 Market Structure             Volume
 Liquidity                    VWAP
 FVG                          EMA
 BOS / CHOCH / MSS            ATR
 Order & Breaker Blocks       Sessions
                       │
               Claude Fable 5
      Multi-Agent Reasoning System
                       │
   Market Structure · Liquidity · Session · Smart Money
        → Probability Engine → Risk AI → Journal AI
                       │
            Trade Decision JSON
                       │
     Python Validation → Risk Check → Duplicate Check
                       │
                  TradersPost API
                       │
                  Broker Account
                       │
            Position Monitoring AI
                       │
          Dynamic Stop Loss Engine
                       │
          Close Position Automatically
                       │
        Trade Memory → Daily Learning Review
```

## The agents

| # | Agent | Reads | Emits |
|---|---|---|---|
| 1 | **Market Structure** | HTF trend, BOS, CHOCH, MSS, swing highs/lows, liquidity pools, premium/discount, FVGs, order blocks, breaker blocks | `{trend, bos, choch, mss, discount, liquidity, key_level, confidence}` |
| 2 | **Liquidity** | Equal highs/lows, stop hunts, buy-side/sell-side, internal vs external liquidity | Liquidity targets + next draw on liquidity |
| 3 | **Session** | London/NY/Asia sessions, kill zones, news time, holidays, volume expansion | Deterministic — clock and calendar facts, no LLM call |
| 4 | **Smart Money** | Institutional footprint, order blocks, mitigation, displacement, volume imbalance | Footprint scores 0–100 |
| 5 | **Probability Engine** | All of the above | Stacked confluence → calibrated probability. Not "buy because RSI is oversold" — *bull trend + liquidity sweep + FVG + bullish OB + London kill zone + volume expansion = 0.93 long* |
| 6 | **Risk AI** | Probability read + hard caps | Position size, risk %, RR, scaling. Sizing is computed in Python; the model may only tighten risk, never loosen it |
| 7 | **Trade Execution** | Validated decision | `TradeDecision` JSON — the sole contract with the execution layer |

Sizing example: a $100,000 account at 0.25% risk with an 18-pip stop and $10/pip
per standard lot gives 1.39 lots — computed by `position_size()`, not by the model.

## Components

| Layer | Module | What it does |
|---|---|---|
| Market data | `trading_system.data` | Async Polygon / Alpaca / Finnhub REST adapters + TradingView webhook parser, all normalized to `Candle` |
| Indicators | `trading_system.indicators` | EMA, session-anchored VWAP, Wilder ATR, relative volume, session & kill-zone tagging |
| Structure | `trading_system.structure` | Fractal swings, BOS/CHOCH/MSS, FVGs with mitigation, order & breaker blocks, equal-high/low liquidity pools with sweep detection, premium/discount, displacement |
| Reasoning | `trading_system.reasoning` | The seven agents above, on Claude Fable 5 with structured outputs |
| Vision | `trading_system.reasoning.vision` | Chart screenshot analysis — trend, BOS, CHOCH, liquidity, OBs, FVGs, entry/stop/target, with an explanation |
| Decision | `trading_system.decision` | `TradeDecision` schema + deterministic position sizing |
| Safety | `trading_system.safety` | 13-point pre-trade checklist + drawdown circuit breaker |
| Execution | `trading_system.execution` | Schema/risk/duplicate validator, then the TradersPost webhook client |
| Monitoring | `trading_system.monitoring` | Position monitor + dynamic stop engine (breakeven, ATR trail, structure trail, auto-close) |
| Memory | `trading_system.memory` | SQLite journal of every trade and gate decision, plus the daily learning review |
| Pine Script | `pine/structure_alerts.pine` | TradingView indicator firing BOS/CHOCH webhook alerts as an event source |

## Safety layer

Every check runs in Python after the AI step and before execution. Any failure
rejects the trade:

✓ maximum daily loss ✓ news events ✓ spread ✓ slippage ✓ liquidity
✓ drawdown ✓ open positions ✓ correlation ✓ position size ✓ volatility
✓ trading hours ✓ risk:reward ✓ probability floor

On top of that a circuit breaker returns `TRADING_ALLOWED`, `COOLDOWN`
(losing-streak cooldown), or `HALTED` (daily / weekly / max-drawdown breach).

## Memory and daily learning

Every trade stores chart reference, reason, entry, exit, win/loss, emotion,
mistake, news, RR, structure, confidence, and market context. After each
session `DailyLearning` reviews the day's trades, finds mistakes, identifies
recurring patterns, and assesses confidence calibration.

**Strategy changes are never auto-deployed.** Every proposed rule change is
written to the review report with `requires_human_review: true` — the flag is
forced in code regardless of model output.

## Setup

```bash
cd trading-system
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in your keys
```

## Running

```bash
# Analyze only — prints the TradeDecision JSON, sends nothing
python -m trading_system.pipeline --symbol SPY --timeframe 5m --dry-run

# Execute approved decisions and monitor the position
python -m trading_system.pipeline --symbol SPY --timeframe 5m --live
```

Execution requires the explicit `--live` flag; anything else is analysis only.

## Tests

```bash
python -m pytest tests/ -v      # 69 tests, no API keys or network required
```

Coverage is on the deterministic layers where correctness is checkable:
indicators, structure detection, decision geometry and sizing, the full safety
checklist, the execution validator, the dynamic stop engine, the journal, and
webhook parsing.

## Safety model

1. **Deterministic risk gate** — `RiskLimits` are hard caps applied in code
   after reasoning. A decision violating them is rejected regardless of model
   output.
2. **Structured output** — the model must return JSON matching the schema;
   geometry (`stop < entry < target` for longs) is re-validated by pydantic.
3. **Refusal handling** — responses are checked for `stop_reason == "refusal"`
   with server-side fallback to Opus, so a declined request degrades gracefully
   instead of crashing the loop.
4. **Human in the loop for strategy** — analytics update automatically; rule
   changes wait for review.

> Research and paper-trading scaffold. Not financial advice. Test against a
> paper account before connecting real money.
