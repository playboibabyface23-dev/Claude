"""Backtest CLI.

    # From a configured data provider
    python -m trading_system.backtest --symbol SPY --timeframe 5m --days 30

    # From a CSV: timestamp,open,high,low,close,volume
    python -m trading_system.backtest --symbol SPY --csv bars.csv --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings
from ..models import Candle, Timeframe
from .engine import Backtester, BacktestConfig
from .report import format_report

log = logging.getLogger("backtest")


def load_csv(path: str | Path) -> list[Candle]:
    out: list[Candle] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("timestamp") or row.get("time") or row.get("date") or "").strip()
            try:
                ts = (datetime.fromtimestamp(float(raw), tz=timezone.utc)
                      if raw.replace(".", "", 1).isdigit()
                      else datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                log.warning("skipping row with unparseable time: %r", raw)
                continue
            try:
                out.append(Candle(
                    timestamp=ts,
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                ))
            except (KeyError, ValueError) as exc:
                log.warning("skipping malformed row: %s", exc)
    out.sort(key=lambda c: c.timestamp)
    return out


async def fetch(symbol: str, timeframe: Timeframe, days: int,
                settings: Settings) -> list[Candle]:
    from ..pipeline import build_provider

    provider = build_provider(settings)
    try:
        end = datetime.now(timezone.utc)
        return await provider.candles(symbol, timeframe, end - timedelta(days=days), end)
    finally:
        await provider.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Backtest the deterministic layers")
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeframe", default="5m", choices=[t.value for t in Timeframe])
    p.add_argument("--csv", help="candle CSV instead of a live provider")
    p.add_argument("--days", type=int, default=30, help="history to fetch")
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--risk", type=float, default=1.0, help="percent risked per trade")
    p.add_argument("--min-rr", type=float, default=2.0)
    p.add_argument("--spread-bps", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=0.5)
    p.add_argument("--no-gates", action="store_true",
                   help="skip the safety layer (shows what it is filtering out)")
    p.add_argument("--json", help="also write the full result as JSON")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    settings = Settings.from_env()
    timeframe = Timeframe(args.timeframe)
    if args.csv:
        candles = load_csv(args.csv)
    else:
        candles = asyncio.run(fetch(args.symbol, timeframe, args.days, settings))

    if not candles:
        print("no candles loaded", file=sys.stderr)
        return 1

    config = BacktestConfig(
        symbol=args.symbol, timeframe=timeframe.value,
        starting_equity=args.equity, risk_pct=args.risk, min_rr=args.min_rr,
        spread_bps=args.spread_bps, slippage_bps=args.slippage_bps,
        apply_safety_layer=not args.no_gates,
    )
    result = Backtester(config, settings).run(candles)
    print(format_report(result))

    if args.json:
        Path(args.json).write_text(json.dumps(result.to_dict(), indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
