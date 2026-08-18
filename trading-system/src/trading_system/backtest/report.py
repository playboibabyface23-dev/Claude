"""Human-readable backtest report.

Leads with what the run did *not* test. A backtest's caveats are the part a
reader most needs and least wants, so they go at the top rather than in a
footnote nobody reaches.
"""

from __future__ import annotations

from .engine import SKIPPED_GATES, BacktestResult


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v, d: int = 2) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:.{d}f}"


def format_report(result: BacktestResult) -> str:
    m = result.metrics()
    cfg = result.config
    lines: list[str] = []

    lines.append("=" * 66)
    lines.append(f"  BACKTEST  {cfg.symbol}  {cfg.timeframe}")
    if result.first_bar and result.last_bar:
        lines.append(f"  {result.first_bar:%Y-%m-%d %H:%M} → "
                     f"{result.last_bar:%Y-%m-%d %H:%M} UTC "
                     f"({m['bars_tested']} bars)")
    lines.append("=" * 66)

    lines.append("")
    lines.append("WHAT THIS DOES NOT TEST")
    lines.append("  The reasoning layer. This runs a deterministic reference")
    lines.append("  strategy over the same structure primitives; Claude will read")
    lines.append("  them differently. No result here transfers to the live agent.")
    for gate, why in SKIPPED_GATES.items():
        lines.append(f"  Gate skipped — {gate:<14} {why}")
    if m["ambiguous_bars"]:
        lines.append(f"  {m['ambiguous_bars']} bar(s) hit stop and target together;")
        lines.append("  OHLC cannot order them, so the loss was assumed.")

    lines.append("")
    lines.append("RESULTS")
    rows = [
        ("Trades", str(m["trades"])),
        ("Win rate", f"{_pct(m['win_rate'])}  ({m['wins']}W / {m['losses']}L)"),
        ("Expectancy", f"{_num(m['expectancy_r'], 3)} R per trade"),
        ("Total R", _num(m["total_r"], 2)),
        ("Profit factor", _num(m["profit_factor"])),
        ("Net P&L", f"${m['net_pnl']:,.2f}"),
        ("Costs paid", f"${m['total_costs']:,.2f}"),
        ("Return", f"{m['return_pct']:.2f}%"),
        ("Max drawdown", f"{m['max_drawdown_pct']:.2f}%"),
        ("Avg win / loss", f"{_num(m['avg_win_r'])} R / {_num(m['avg_loss_r'])} R"),
    ]
    for k, v in rows:
        lines.append(f"  {k:<16} {v}")

    lines.append("")
    lines.append("SIGNAL FUNNEL")
    lines.append(f"  {m['signals_generated']} signal(s) generated, "
                 f"{m['signals_rejected']} rejected by gates, "
                 f"{len(result.trades)} taken")
    if result.gate_rejections:
        lines.append("  Rejections by gate:")
        for name, n in sorted(result.gate_rejections.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {name:<24} {n}")

    closed = result.closed
    if closed:
        lines.append("")
        lines.append("EXITS")
        by_reason: dict[str, int] = {}
        for t in closed:
            by_reason[t.exit_reason or "?"] = by_reason.get(t.exit_reason or "?", 0) + 1
        for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<24} {n}")

        trailed = [t for t in closed if t.stop_moves > 0]
        lines.append(f"  stop trailed on         {len(trailed)} of {len(closed)} trades")

        # Excursion tells you whether stops are placed sensibly: consistently
        # large favourable excursion on losing trades means exits are too late.
        losers = [t for t in closed if t.pnl < 0]
        if losers:
            avg_mfe = sum(t.max_favourable_r for t in losers) / len(losers)
            lines.append(f"  avg MFE on losers       {avg_mfe:.2f} R "
                         "(high values suggest exits are too late)")

    if len(closed) < 30:
        lines.append("")
        lines.append(f"NOTE  {len(closed)} closed trade(s) is too small a sample to")
        lines.append("      conclude anything about edge. Treat this as a wiring")
        lines.append("      check, not evidence.")

    lines.append("=" * 66)
    return "\n".join(lines)
