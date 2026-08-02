#!/usr/bin/env python3
"""Render the control room against a synthetic journal.

Lets you see the dashboard without connecting a broker or running the pipeline.
The generated HTML is not committed — the data in it is invented, and a
fabricated equity curve sitting in the repo is the kind of thing that later
gets mistaken for a real track record.

    python scripts/demo_dashboard.py            # writes dashboard-preview.html
    python scripts/demo_dashboard.py out.html
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.config import RiskLimits, Settings  # noqa: E402
from trading_system.dashboard.server import render_page  # noqa: E402
from trading_system.dashboard.state import build_snapshot  # noqa: E402
from trading_system.decision import TradeAction, TradeDecision  # noqa: E402
from trading_system.memory import TradeJournal, TradeRecord  # noqa: E402

HISTORY = [
    # symbol, side, entry, exit, probability, won, qty
    ("SPY", "buy", 512.40, 515.80, 0.71, True, 180),
    ("EURUSD", "sell", 1.0842, 1.0798, 0.68, True, 120_000),
    ("QQQ", "buy", 438.10, 435.60, 0.61, False, 150),
    ("SPY", "buy", 509.20, 514.05, 0.77, True, 180),
    ("USDJPY", "buy", 152.10, 151.42, 0.59, False, 90_000),
    ("AAPL", "buy", 228.40, 232.10, 0.73, True, 300),
    ("SPY", "sell", 517.90, 519.30, 0.58, False, 180),
]

GATE_CHECKS = [
    ("circuit_breaker", True, "state=trading_allowed"),
    ("news_events", False, "high-impact news in 12 min (blackout 30 min)"),
    ("quote_data", True, ""),
    ("spread", True, "spread 0.0039% vs cap 0.05%"),
    ("slippage", True, "stop distance 1.30000 vs 4x spread 0.08000"),
    ("liquidity", True, "relative volume 1.42"),
    ("open_positions", True, "1 open vs max 2"),
    ("correlation", False, "1 correlated open positions in group ('SPY','QQQ','ES','NQ')"),
    ("duplicate_position", True, ""),
    ("position_data_fresh", True, ""),
    ("position_size", True, "risk 0.75% qty 180"),
    ("risk_reward", True, "RR 3.00 vs min 1.5"),
    ("probability", True, "p=0.66 vs min 0.55"),
    ("volatility", True, "ATR 0.412% is 1.18x the 0.349% baseline (cap 4x)"),
    ("trading_hours", True, ""),
    ("data_heartbeat", True, ""),
]

LIVE_ANALYSIS = {
    "symbol": "QQQ", "timeframe": "5m", "direction": "long", "probability": 0.66,
    "structure": {
        "trend": "bullish", "bos": True, "choch": False, "mss": False,
        "in_discount": True, "displacement": True,
        "unmitigated_fvgs": [{}, {}], "order_blocks": [{}, {}, {}],
        "liquidity_pools": [{}, {}, {}, {}],
    },
    "indicators": {
        "price": 438.62, "vwap": 437.90, "ema_20": 438.10, "ema_50": 436.55,
        "atr_pct": 0.412, "baseline_atr_pct": 0.349, "volatility_ratio": 1.18,
        "relative_volume": 1.42, "volume_expansion": False,
        "session": "new_york", "kill_zone": "ny_open",
    },
    "confluence": [
        {"name": "Bullish market structure", "detail": "BOS above the 437.80 swing high", "weight": 0.9},
        {"name": "Liquidity sweep", "detail": "Sell-side taken at 435.10, closed back inside", "weight": 0.85},
        {"name": "Unmitigated FVG", "detail": "Bullish imbalance 437.2–438.0 rebalanced", "weight": 0.7},
        {"name": "Bullish order block", "detail": "Last down candle before the displacement leg", "weight": 0.65},
        {"name": "NY open kill zone", "detail": "Within the first 90 minutes", "weight": 0.55},
        {"name": "Discount array", "detail": "Trading below 50% of the dealing range", "weight": 0.5},
    ],
    "agent_reads": {
        "market_structure": {"trend": "bullish", "narrative":
            "Higher highs and higher lows intact since the London session. BOS above "
            "437.80 confirms continuation, and price sits in the discount half of the "
            "dealing range."},
        "liquidity": {"internal_liquidity":
            "Sell-side liquidity at 435.10 was swept and reclaimed on the same candle.",
            "next_draw_on_liquidity": 441.30},
        "session": {"session": "new_york", "in_kill_zone": True, "volume_expansion": False},
        "smart_money": {"institutional_footprint": 72, "market_maker_read":
            "Displacement leg with a clean order block and one unmitigated imbalance — "
            "consistent with accumulation rather than distribution."},
        "probability": {"reasoning":
            "Six independent confluences align long. The setup is held back only by the "
            "pending economic release."},
        "risk": {"notes":
            "Stop beyond the order block low at 436.90, target the 441.30 draw. Risk "
            "trimmed to 0.75% given news proximity."},
    },
}


def seed(journal: TradeJournal, now: datetime) -> dict:
    for i, (symbol, side, entry, exit_price, prob, won, qty) in enumerate(HISTORY):
        # Scale the stop to the instrument — a fixed offset is meaningless
        # across a 512-dollar index and a 1.08 currency pair.
        risk = entry * 0.0024
        stop = entry - risk if side == "buy" else entry + risk
        target = entry + 3 * risk if side == "buy" else entry - 3 * risk
        decision = TradeDecision(
            symbol=symbol, action=TradeAction(side), entry=entry, stop=stop,
            target=target, risk_pct=0.75, quantity=qty, probability=prob,
            timeframe="5m", session="new_york",
            reasoning="Liquidity sweep below equal lows, bullish FVG rebalanced, "
                      "entry taken in the NY kill zone.",
        )
        record = TradeRecord.from_decision(decision, {"trend": "bullish", "bos": True}, {})
        record.opened_at = now - timedelta(days=8 - i, hours=2)
        trade_id = journal.record_trade(record)
        pnl = round(abs(exit_price - entry) * qty * (1 if won else -1), 2)
        journal.close_trade(trade_id, exit_price=exit_price, pnl=pnl,
                            outcome="win" if won else "loss",
                            closed_at=now - timedelta(days=8 - i))

    journal.record_trade(TradeRecord.from_decision(TradeDecision(
        symbol="SPY", action=TradeAction.BUY, entry=514.20, stop=512.90,
        target=518.10, risk_pct=0.75, quantity=180, probability=0.74,
        timeframe="5m", session="new_york",
        reasoning="BOS up, discount array, London to NY continuation.",
    ), {"trend": "bullish"}, {}))
    journal.update_high_water_mark(101_400)

    verdict = {"approved": False, "breaker": "trading_allowed", "checks": [
        {"name": n, "passed": p, "detail": d} for n, p, d in GATE_CHECKS]}
    journal.record_gate_decision("QQQ", verdict)
    for name in ("news_events", "spread", "news_events", "volatility"):
        journal.record_gate_decision("SPY", {
            "approved": False, "breaker": "trading_allowed",
            "checks": [{"name": name, "passed": False, "detail": "…"}]})
    return verdict


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "dashboard-preview.html")
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "demo.db")
        journal = TradeJournal(db)
        try:
            verdict = seed(journal, now)
            settings = Settings(risk=RiskLimits(account_equity=100_000),
                                journal_db_path=db)
            analysis = dict(LIVE_ANALYSIS, verdict=verdict)
            snapshot = asyncio.run(build_snapshot(settings, journal, None, analysis))
            snapshot["positions"] = {
                "items": [{"symbol": "SPY", "side": "long", "quantity": 180,
                           "avg_entry_price": 514.20, "unrealized_pnl": 327.60,
                           "source": "broker"}],
                "source": "broker", "degraded": False, "limit": 2,
            }
            snapshot["gates"].update(approved=False, checks=verdict["checks"],
                                     symbol="QQQ", at=now.isoformat())
            out.write_text(render_page(snapshot), encoding="utf-8")
        finally:
            journal.close()
    print(f"wrote {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
