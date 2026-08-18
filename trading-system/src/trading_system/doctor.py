"""Preflight doctor: exercise every configured integration before risking money.

Runs the same code paths the live pipeline uses — market data, quotes, broker
positions, market hours, news, the journal, and (optionally) Claude — and
reports pass/warn/fail per integration with a remedy for anything broken.

This command is side-effect free by design. It never places an order: even a
"ping" webhook to TradersPost is unsafe to send automatically, because whether
that fires a real fill depends on a paper/live toggle this system cannot read
back (TradersPost exposes no position or account API). TradersPost is checked
for configuration shape only; connectivity is left to manual verification in
the TradersPost dashboard.

    python -m trading_system.doctor --symbol SPY
    python -m trading_system.doctor --symbol SPY --check-claude   # costs tokens
    python -m trading_system.doctor --symbol SPY --json report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from .config import Settings
from .dashboard.state import policy_view
from .memory import TradeJournal
from .market_hours import build_market_hours
from .models import Timeframe

Status = str  # "pass" | "warn" | "fail" | "skip"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Status
    detail: str = ""
    remedy: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "remedy": self.remedy}


def _check(name: str, status: Status, detail: str = "", remedy: str = "") -> DoctorCheck:
    return DoctorCheck(name, status, detail, remedy)


# --------------------------------------------------------------- individual checks

async def check_credentials(settings: Settings) -> list[DoctorCheck]:
    out = []
    out.append(_check(
        "anthropic_api_key", "pass" if settings.anthropic_api_key else "fail",
        "configured" if settings.anthropic_api_key else "not set",
        "" if settings.anthropic_api_key else
        "set ANTHROPIC_API_KEY — the reasoning layer cannot run without it",
    ))
    data_providers = [n for n, v in (
        ("polygon", settings.polygon_api_key),
        ("alpaca", settings.alpaca_key_id and settings.alpaca_secret_key),
        ("finnhub", settings.finnhub_api_key),
    ) if v]
    out.append(_check(
        "market_data_credentials", "pass" if data_providers else "fail",
        f"available: {', '.join(data_providers)}" if data_providers else "none configured",
        "" if data_providers else
        "set POLYGON_API_KEY, ALPACA_API_KEY_ID/SECRET, or FINNHUB_API_KEY",
    ))
    out.append(_check(
        "traderspost_webhook", "pass" if settings.traderspost_webhook_url else "fail",
        "configured" if settings.traderspost_webhook_url else "not set",
        "" if settings.traderspost_webhook_url else
        "set TRADERSPOST_WEBHOOK_URL — required to submit any order",
    ))
    return out


async def check_market_data(settings: Settings, symbol: str,
                            timeframe: Timeframe) -> tuple[list[DoctorCheck], Optional[object]]:
    """Returns (checks, provider). Caller is responsible for closing the provider
    if one was returned, so quote/heartbeat checks can reuse the same connection."""
    from datetime import timedelta

    from .pipeline import build_provider

    try:
        provider = build_provider(settings)
    except SystemExit as exc:
        return [_check("market_data", "fail", str(exc),
                       "configure at least one market data provider")], None

    checks = [_check("market_data_provider", "pass", f"using {provider.name}")]
    try:
        end = datetime.now(timezone.utc)
        candles = await provider.candles(symbol, timeframe,
                                         end - timedelta(hours=6), end)
    except Exception as exc:
        checks.append(_check("market_data_fetch", "fail", f"{provider.name}: {exc}",
                             "check the API key and symbol are valid for this provider"))
        return checks, provider

    if not candles:
        checks.append(_check("market_data_fetch", "fail",
                             f"no candles returned for {symbol} {timeframe.value}",
                             "check the symbol is correct and markets have recently traded"))
        return checks, provider

    age = (datetime.now(timezone.utc) - candles[-1].timestamp).total_seconds()
    limit = max(timeframe.minutes * 60 * settings.risk.stale_bar_multiple, 120.0)
    checks.append(_check(
        "market_data_freshness",
        "pass" if age <= limit else "warn",
        f"{len(candles)} candles, last bar {age:.0f}s old (limit {limit:.0f}s)",
        "" if age <= limit else
        "last bar is stale — markets may be closed, or the feed may be lagging",
    ))
    return checks, provider


async def check_quotes(provider, settings: Settings, symbol: str) -> list[DoctorCheck]:
    from .pipeline import fetch_quote

    if provider is None:
        return [_check("quote_data", "skip", "no market data provider available")]

    quote, error = await fetch_quote(provider, symbol)
    if quote is None:
        status = "warn" if not settings.risk.require_quote else "fail"
        return [_check(
            "quote_data", status, error or "no quote",
            f"REQUIRE_QUOTE={'true' if settings.risk.require_quote else 'false'} — "
            + ("every trade will block on quote_data until this is fixed"
               if settings.risk.require_quote else
               "spread/slippage checks are disabled by policy; trades will "
               "execute without that protection"),
        )]
    stale = quote.is_stale(settings.risk.max_quote_age_seconds)
    return [_check(
        "quote_data", "warn" if stale else "pass",
        f"{provider.name} bid={quote.bid} ask={quote.ask} "
        f"spread={quote.spread:.5f} age={quote.age_seconds():.1f}s",
        "quote is older than MAX_QUOTE_AGE_SECONDS" if stale else "",
    )]


async def check_broker_positions(settings: Settings, journal: TradeJournal) -> list[DoctorCheck]:
    from .pipeline import build_position_provider

    provider = build_position_provider(settings, journal)
    try:
        report = await provider.reconcile()
    except Exception as exc:
        return [_check("broker_positions", "fail", str(exc),
                       "check broker API credentials and connectivity")]
    finally:
        await provider.close()

    has_broker = bool(settings.alpaca_key_id and settings.alpaca_secret_key)
    checks = [_check(
        "broker_positions",
        "fail" if report.degraded else ("pass" if has_broker else "warn"),
        f"{len(report.positions)} open position(s), source="
        + (report.positions[0].source if report.positions else
           ("broker" if has_broker else "journal")),
        "" if not report.degraded else (report.error or "broker unreachable"),
    ) if not report.degraded else _check(
        "broker_positions", "fail", report.error or "broker unreachable",
        "positions fell back to journal-only state — fix broker connectivity "
        "before trading live, or every trade will fail position_data_fresh",
    )]
    if not has_broker:
        checks.append(_check(
            "broker_positions_scope", "warn",
            "no broker API configured — only positions this system itself "
            "opened are visible",
            "set ALPACA_API_KEY_ID/SECRET for real position truth; without it "
            "correlation, open-position-count, and duplicate checks are blind "
            "to anything opened by hand or by another strategy",
        ))
    if report.has_drift:
        checks.append(_check(
            "position_drift", "warn",
            f"stale_journal={report.stale_journal_symbols} "
            f"untracked_broker={report.untracked_broker_symbols}",
            "review — stale journal entries were healed automatically; "
            "untracked broker positions are counted against risk limits",
        ))
    return checks


async def check_circuit_breaker(settings: Settings, journal: TradeJournal) -> list[DoctorCheck]:
    """Whether the drawdown/losing-streak circuit breaker is already tripped.

    A tripped breaker is not a misconfiguration — it is the safety layer doing
    its job — but it means every trade will be refused right out of the gate,
    which is worth knowing before going live rather than discovering it as a
    silent rejection on the first run.
    """
    from .account import build_account_state
    from .pipeline import build_position_provider
    from .safety import BreakerState, SafetyLayer

    provider = build_position_provider(settings, journal)
    try:
        account_state, _ = await build_account_state(journal, provider, settings)
    except Exception as exc:
        return [_check("circuit_breaker", "fail", str(exc),
                       "could not assemble account state from the journal/broker")]
    finally:
        await provider.close()

    detail = SafetyLayer(settings).breaker_detail(account_state)
    if detail.state == BreakerState.TRADING_ALLOWED:
        return [_check(
            "circuit_breaker", "pass",
            f"trading_allowed — equity={account_state.equity:.2f} "
            f"daily={account_state.daily_pnl:.2f} weekly={account_state.weekly_pnl:.2f}",
        )]
    remedy = "the safety layer will refuse every trade until this clears"
    if detail.reactivates_at:
        remedy += f" (clears {detail.reactivates_at.isoformat()})"
    return [_check("circuit_breaker", "warn",
                   f"{detail.state.value}: {detail.reason}", remedy)]


async def check_market_hours(settings: Settings, symbol: str) -> list[DoctorCheck]:
    calendar = build_market_hours(settings, symbol)
    try:
        status = await calendar.status(symbol)
    finally:
        await calendar.close()

    live_source = status.source in ("alpaca_clock",)
    checks = [_check(
        "market_hours", "pass" if status.determined else "fail",
        f"is_open={status.is_open} source={status.source} reason={status.reason!r}",
        "" if status.determined else "market session could not be determined",
    )]
    if not live_source and symbol.upper() not in ():
        from .market_hours import classify_asset, AssetClass

        if classify_asset(symbol) == AssetClass.US_EQUITY:
            checks.append(_check(
                "market_hours_source", "warn",
                f"using offline calendar ({status.source}), not the live broker clock",
                "set ALPACA_API_KEY_ID/SECRET so unscheduled halts and LULD "
                "pauses are detected — the offline calendar only knows "
                "scheduled closures",
            ))
    return checks


async def check_news(settings: Settings, symbol: str) -> list[DoctorCheck]:
    from .pipeline import fetch_news_window

    minutes, checked, error = await fetch_news_window(settings, symbol)
    sources = []
    if settings.use_recurring_macro_events:
        sources.append("recurring")
    if settings.news_calendar_file:
        sources.append("file")
    if settings.finnhub_api_key:
        sources.append("finnhub")

    if not checked:
        status = "fail" if settings.risk.require_news_check else "warn"
        return [_check(
            "news_calendar", status, error or "no news source configured",
            "set FINNHUB_API_KEY, NEWS_CALENDAR_FILE, or leave "
            "USE_RECURRING_MACRO_EVENTS on; otherwise REQUIRE_NEWS_CHECK=true "
            "blocks every trade" if settings.risk.require_news_check else
            "news blackout is disabled by policy",
        )]
    detail = f"sources={'+'.join(sources)}"
    detail += f" nearest_event_in={minutes:.0f}min" if minutes is not None else " no relevant event in view"
    return [_check("news_calendar", "pass" if not error else "warn", detail, error or "")]


async def check_journal(settings: Settings, journal: TradeJournal) -> list[DoctorCheck]:
    import os

    stats = journal.stats()
    open_trades = journal.open_trades()
    return [_check(
        "journal", "pass",
        f"path={os.path.abspath(settings.journal_db_path)} "
        f"closed_trades={stats['total_closed']} open={len(open_trades)} "
        f"high_water_mark={journal.high_water_mark():.2f}",
    )]


def check_risk_policy(settings: Settings) -> list[DoctorCheck]:
    view = policy_view(settings)
    checks = [_check(
        "risk_limits", "pass",
        ", ".join(f"{l['name']}={l['value']}" for l in view["limits"]),
    )]
    for p in view["policies"]:
        checks.append(_check(
            f"policy:{p['name'].lower().replace(' ', '_')}",
            "pass" if p["on"] else "warn",
            f"{'enforced' if p['on'] else 'disabled'} — guards {p['guards']}",
            "" if p["on"] else
            f"unknown inputs to {p['guards']} will not block trades",
        ))
    return checks


def check_traderspost_shape(settings: Settings) -> list[DoctorCheck]:
    url = settings.traderspost_webhook_url
    if not url:
        return [_check("traderspost_config", "fail", "not set",
                       "set TRADERSPOST_WEBHOOK_URL")]
    parsed = urlparse(url)
    well_formed = parsed.scheme == "https" and bool(parsed.netloc)
    return [
        _check(
            "traderspost_config", "pass" if well_formed else "fail",
            f"scheme={parsed.scheme} host={parsed.netloc}",
            "" if well_formed else "webhook URL must be https",
        ),
        _check(
            "traderspost_connectivity", "skip",
            "not tested — sending a probe payload could trigger a real fill "
            "depending on the strategy's paper/live mode, which this system "
            "cannot read back",
            "confirm manually in the TradersPost dashboard that the strategy "
            "is set to Paper before the first live run",
        ),
    ]


async def check_claude(settings: Settings) -> list[DoctorCheck]:
    from .reasoning import ClaudeClient, ClaudeRefusal

    if not settings.anthropic_api_key:
        return [_check("claude_api", "skip", "ANTHROPIC_API_KEY not set")]
    try:
        client = ClaudeClient(settings.anthropic_api_key)
        result = client.structured(
            system="Reply with exactly the requested field.",
            user_content="Return ok=true.",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                   "required": ["ok"], "additionalProperties": False},
            effort="low", max_tokens=64,
        )
        return [_check("claude_api", "pass" if result.get("ok") else "warn",
                       f"model responded: {result}")]
    except ClaudeRefusal as exc:
        return [_check("claude_api", "warn", f"refused: {exc}",
                       "harmless for this probe, but check server-side fallback "
                       "is enabled for production traffic")]
    except Exception as exc:
        return [_check("claude_api", "fail", str(exc),
                       "check ANTHROPIC_API_KEY is valid and has quota")]


# --------------------------------------------------------------- orchestration

async def run_all(settings: Settings, symbol: str, timeframe: Timeframe,
                  include_claude: bool = False) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    checks += await check_credentials(settings)
    checks += check_traderspost_shape(settings)
    checks += check_risk_policy(settings)

    data_checks, provider = await check_market_data(settings, symbol, timeframe)
    checks += data_checks
    try:
        checks += await check_quotes(provider, settings, symbol)
    finally:
        if provider is not None:
            await provider.close()

    checks += await check_market_hours(settings, symbol)
    checks += await check_news(settings, symbol)

    journal = TradeJournal(settings.journal_db_path)
    try:
        checks += await check_broker_positions(settings, journal)
        checks += await check_circuit_breaker(settings, journal)
        checks += await check_journal(settings, journal)
    finally:
        journal.close()

    if include_claude:
        checks += await check_claude(settings)
    else:
        checks.append(_check(
            "claude_api", "skip",
            "not tested — pass --check-claude to verify (uses a small number "
            "of tokens)",
        ))
    return checks


_SYMBOLS = {"pass": "✓", "warn": "!", "fail": "✗", "skip": "·"}


def format_report(checks: list[DoctorCheck], symbol: str, timeframe: str) -> str:
    lines = ["=" * 66, f"  PREFLIGHT  {symbol}  {timeframe}", "=" * 66, ""]
    width = max(len(c.name) for c in checks) + 2
    for c in checks:
        lines.append(f"  {_SYMBOLS[c.status]} {c.name:<{width}} {c.detail}")
        if c.remedy and c.status in ("fail", "warn"):
            lines.append(f"      → {c.remedy}")
    counts = {s: sum(1 for c in checks if c.status == s) for s in _SYMBOLS}
    lines += ["", "-" * 66,
             f"  {counts['pass']} pass, {counts['warn']} warn, "
             f"{counts['fail']} fail, {counts['skip']} skip"]
    if counts["fail"]:
        lines.append("  NOT READY — resolve the ✗ items above before trading live.")
    elif counts["warn"]:
        lines.append("  READY WITH CAVEATS — review the ! items; each names its own risk.")
    else:
        lines.append("  READY — every checked integration resolved cleanly.")
    lines.append("=" * 66)
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    timeframe = Timeframe(args.timeframe)
    checks = await run_all(settings, args.symbol, timeframe, args.check_claude)
    print(format_report(checks, args.symbol, timeframe.value))
    if args.json:
        from pathlib import Path

        Path(args.json).write_text(
            json.dumps([c.to_dict() for c in checks], indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 1 if any(c.status == "fail" for c in checks) else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight check every configured integration — no orders are ever sent")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="5m", choices=[t.value for t in Timeframe])
    parser.add_argument("--check-claude", action="store_true",
                        help="make a small live call to verify Claude auth (uses tokens)")
    parser.add_argument("--json", help="also write results as JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
