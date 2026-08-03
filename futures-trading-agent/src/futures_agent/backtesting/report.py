"""Human-readable backtest report. Leads with what the run does not prove,
since that is the part a reader most needs and least wants to see."""

from __future__ import annotations

from .engine import BacktestResult


def _num(v, d: int = 2) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:.{d}f}"


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def format_report(result: BacktestResult) -> str:
    m = result.metrics()
    cfg = result.config
    lines: list[str] = []

    lines.append("=" * 66)
    lines.append(f"  BACKTEST  {cfg.symbol}")
    if result.first_bar and result.last_bar:
        lines.append(f"  {result.first_bar:%Y-%m-%d %H:%M} -> {result.last_bar:%Y-%m-%d %H:%M} "
                     f"({m['bars_tested']} bars)")
    lines.append("=" * 66)

    lines.append("")
    lines.append("WHAT THIS DOES NOT TEST")
    lines.append("  The reasoning layer. This runs a deterministic reference strategy")
    lines.append("  (confluence counting over the same indicators) standing in for")
    lines.append("  Claude. Claude will read this structure differently — no result")
    lines.append("  here transfers to the live agent. What IS real: the RiskManager,")
    lines.append("  position sizing, and exit management run exactly as they would live.")
    if m["ambiguous_bars"]:
        lines.append(f"  {m['ambiguous_bars']} bar(s) hit stop and target in the same bar;")
        lines.append("  OHLC cannot order them, so the loss was assumed.")

    lines.append("")
    lines.append("RESULTS")
    rows = [
        ("Trades", str(m["trades"])),
        ("Win rate", f"{_pct(m['win_rate'])}  ({m['wins']}W / {m['losses']}L)"),
        ("Expectancy", f"{_num(m['expectancy_r'], 3)} R per trade"),
        ("Total R", _num(m["total_r"])),
        ("Profit factor", _num(m["profit_factor"])),
        ("Net P&L", f"${m['net_pnl']:,.2f}"),
        ("Return", f"{m['return_pct']:.2f}%"),
        ("Max drawdown", f"{m['max_drawdown_pct']:.2f}%"),
        ("Still open at end", str(m["still_open_at_end"])),
    ]
    for k, v in rows:
        lines.append(f"  {k:<20} {v}")

    lines.append("")
    lines.append("SIGNAL FUNNEL")
    lines.append(f"  {m['signals_generated']} signal(s) generated, "
                 f"{m['signals_rejected']} rejected by the risk manager, "
                 f"{m['trades']} taken")
    if result.gate_rejections:
        lines.append("  Rejections by check:")
        for name, count in sorted(result.gate_rejections.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name:<20} {count}")

    closed = result.closed
    if closed:
        lines.append("")
        lines.append("EXITS")
        by_reason: dict[str, int] = {}
        for t in closed:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<20} {count}")

    if len(closed) < 30:
        lines.append("")
        lines.append(f"NOTE  {len(closed)} closed trade(s) is too small a sample to")
        lines.append("      conclude anything about edge. Treat this as a wiring check.")

    lines.append("=" * 66)
    return "\n".join(lines)
