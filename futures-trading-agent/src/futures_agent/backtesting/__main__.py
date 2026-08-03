"""Backtest CLI.

    python -m futures_agent.backtesting --symbol MNQ --csv bars.csv
    python -m futures_agent.backtesting --symbol ES --csv bars.csv --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..market.data import CsvBarsProvider
from ..market.models import Timeframe
from .engine import Backtester, BacktestConfig
from .report import format_report


def main() -> int:
    p = argparse.ArgumentParser(description="Backtest the deterministic layers over historical bars")
    p.add_argument("--symbol", required=True)
    p.add_argument("--csv", required=True, help="CSV with columns timestamp,open,high,low,close,volume")
    p.add_argument("--equity", type=float, default=50_000.0)
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    p.add_argument("--max-trades-per-day", type=int, default=6)
    p.add_argument("--max-drawdown-pct", type=float, default=8.0)
    p.add_argument("--min-confidence", type=float, default=65.0)
    p.add_argument("--warmup-bars", type=int, default=30)
    p.add_argument("--json", help="also write the full result as JSON")
    args = p.parse_args()

    provider = CsvBarsProvider(args.csv)
    candles = asyncio.run(provider.candles(
        args.symbol, Timeframe.M5,
        datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc),
    ))
    if not candles:
        print("no candles loaded", file=sys.stderr)
        return 1

    config = BacktestConfig(
        symbol=args.symbol, starting_equity=args.equity, risk_pct_per_trade=args.risk_pct,
        max_daily_loss_pct=args.max_daily_loss_pct, max_trades_per_day=args.max_trades_per_day,
        max_drawdown_pct=args.max_drawdown_pct, min_ai_confidence=args.min_confidence,
        warmup_bars=args.warmup_bars,
    )
    result = Backtester(config).run(candles)
    print(format_report(result))

    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
