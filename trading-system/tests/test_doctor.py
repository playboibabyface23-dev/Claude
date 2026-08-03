"""Preflight doctor: every check must degrade gracefully, never crash, when an
integration is unconfigured — that is the whole point of a command meant to
run before the first live trade.
"""

import asyncio

import httpx
import pytest

from trading_system import doctor
from trading_system.config import RiskLimits, Settings
from trading_system.data import PolygonData
from trading_system.memory import TradeJournal
from trading_system.models import Quote, Timeframe


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(str(tmp_path / "trades.db"))
    yield j
    j.close()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- credentials

def test_credentials_all_missing_fail():
    checks = run(doctor.check_credentials(Settings()))
    assert all(c.status == "fail" for c in checks)


def test_credentials_present_pass():
    settings = Settings(anthropic_api_key="k", polygon_api_key="p",
                        traderspost_webhook_url="https://hooks.example/x")
    checks = run(doctor.check_credentials(settings))
    by_name = {c.name: c for c in checks}
    assert by_name["anthropic_api_key"].status == "pass"
    assert by_name["market_data_credentials"].status == "pass"
    assert "polygon" in by_name["market_data_credentials"].detail
    assert by_name["traderspost_webhook"].status == "pass"


# --------------------------------------------------------------- market data

def test_market_data_fails_with_no_provider_configured():
    checks, provider = run(doctor.check_market_data(Settings(), "SPY", Timeframe.M5))
    assert provider is None
    assert checks[0].status == "fail"
    assert checks[0].name == "market_data"


def test_market_data_freshness_pass_when_recent(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from trading_system.models import Candle

    class FakeProvider:
        name = "fake"
        supports_quotes = True

        async def candles(self, symbol, timeframe, start, end):
            return [Candle(timestamp=datetime.now(timezone.utc) - timedelta(seconds=30),
                           open=1, high=1, low=1, close=1, volume=1)]

        async def close(self):
            pass

    monkeypatch.setattr("trading_system.pipeline.build_provider",
                        lambda settings: FakeProvider())
    checks, provider = run(doctor.check_market_data(Settings(), "SPY", Timeframe.M5))
    assert provider is not None
    by_name = {c.name: c for c in checks}
    assert by_name["market_data_provider"].status == "pass"
    assert by_name["market_data_freshness"].status == "pass"


def test_market_data_freshness_warns_when_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from trading_system.models import Candle

    class FakeProvider:
        name = "fake"
        supports_quotes = True

        async def candles(self, symbol, timeframe, start, end):
            return [Candle(timestamp=datetime.now(timezone.utc) - timedelta(hours=5),
                           open=1, high=1, low=1, close=1, volume=1)]

        async def close(self):
            pass

    monkeypatch.setattr("trading_system.pipeline.build_provider",
                        lambda settings: FakeProvider())
    checks, _ = run(doctor.check_market_data(Settings(), "SPY", Timeframe.M5))
    by_name = {c.name: c for c in checks}
    assert by_name["market_data_freshness"].status == "warn"


def test_market_data_fetch_failure_is_reported_not_raised(monkeypatch):
    class BoomProvider:
        name = "fake"
        supports_quotes = True

        async def candles(self, symbol, timeframe, start, end):
            raise RuntimeError("feed unreachable")

        async def close(self):
            pass

    monkeypatch.setattr("trading_system.pipeline.build_provider",
                        lambda settings: BoomProvider())
    checks, provider = run(doctor.check_market_data(Settings(), "SPY", Timeframe.M5))
    assert provider is not None   # caller still closes it
    assert any(c.name == "market_data_fetch" and c.status == "fail" for c in checks)


# --------------------------------------------------------------- quotes

def test_quotes_skip_with_no_provider():
    checks = run(doctor.check_quotes(None, Settings(), "SPY"))
    assert checks[0].status == "skip"


def test_quotes_fail_when_missing_and_required():
    p = PolygonData("key")

    async def no_quote(symbol):
        return None

    p.latest_quote = no_quote
    settings = Settings(risk=RiskLimits(require_quote=True))
    checks = run(doctor.check_quotes(p, settings, "SPY"))
    assert checks[0].status == "fail"


def test_quotes_warn_when_missing_and_not_required():
    p = PolygonData("key")

    async def no_quote(symbol):
        return None

    p.latest_quote = no_quote
    settings = Settings(risk=RiskLimits(require_quote=False))
    checks = run(doctor.check_quotes(p, settings, "SPY"))
    assert checks[0].status == "warn"


def test_quotes_pass_when_fresh():
    from datetime import datetime, timezone

    p = PolygonData("key")

    async def fresh_quote(symbol):
        return Quote(symbol="SPY", bid=99.99, ask=100.01,
                     timestamp=datetime.now(timezone.utc), provider="polygon")

    p.latest_quote = fresh_quote
    checks = run(doctor.check_quotes(p, Settings(), "SPY"))
    assert checks[0].status == "pass"


def test_quotes_warn_when_stale():
    from datetime import datetime, timedelta, timezone

    p = PolygonData("key")

    async def stale_quote(symbol):
        return Quote(symbol="SPY", bid=99.99, ask=100.01,
                     timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
                     provider="polygon")

    p.latest_quote = stale_quote
    checks = run(doctor.check_quotes(p, Settings(), "SPY"))
    assert checks[0].status == "warn"


# --------------------------------------------------------------- broker positions

def test_broker_positions_warn_journal_only_with_no_broker(journal):
    checks = run(doctor.check_broker_positions(Settings(), journal))
    by_name = {c.name: c for c in checks}
    assert by_name["broker_positions"].status == "warn"
    assert by_name["broker_positions_scope"].status == "warn"


def test_broker_positions_pass_when_broker_configured_and_reachable(journal, monkeypatch):
    class FakeBroker:
        async def positions(self):
            return []

        async def account(self):
            return None

        async def close(self):
            pass

    monkeypatch.setattr("trading_system.pipeline.AlpacaBroker",
                        lambda *a, **kw: FakeBroker())
    settings = Settings(alpaca_key_id="k", alpaca_secret_key="s")
    checks = run(doctor.check_broker_positions(settings, journal))
    by_name = {c.name: c for c in checks}
    assert by_name["broker_positions"].status == "pass"
    assert "broker_positions_scope" not in by_name


def test_broker_positions_fail_when_broker_unreachable(journal, monkeypatch):
    class BoomBroker:
        async def positions(self):
            raise httpx.ConnectError("connection refused")

        async def close(self):
            pass

    monkeypatch.setattr("trading_system.pipeline.AlpacaBroker",
                        lambda *a, **kw: BoomBroker())
    settings = Settings(alpaca_key_id="k", alpaca_secret_key="s")
    checks = run(doctor.check_broker_positions(settings, journal))
    by_name = {c.name: c for c in checks}
    assert by_name["broker_positions"].status == "fail"


# --------------------------------------------------------------- market hours

def test_market_hours_offline_calendar_warns_for_us_equity():
    checks = run(doctor.check_market_hours(Settings(), "SPY"))
    by_name = {c.name: c for c in checks}
    assert by_name["market_hours"].status == "pass"
    assert by_name["market_hours"].detail  # is_open / source / reason present
    assert by_name["market_hours_source"].status == "warn"


# --------------------------------------------------------------- news

def test_news_fails_when_no_source_and_required():
    settings = Settings(use_recurring_macro_events=False,
                        risk=RiskLimits(require_news_check=True))
    checks = run(doctor.check_news(settings, "SPY"))
    assert checks[0].status == "fail"


def test_news_warns_when_no_source_and_not_required():
    settings = Settings(use_recurring_macro_events=False,
                        risk=RiskLimits(require_news_check=False))
    checks = run(doctor.check_news(settings, "SPY"))
    assert checks[0].status == "warn"


def test_news_passes_with_recurring_source():
    checks = run(doctor.check_news(Settings(), "SPY"))
    assert checks[0].status == "pass"
    assert "recurring" in checks[0].detail


# --------------------------------------------------------------- journal

def test_journal_check_reports_counts(journal):
    checks = run(doctor.check_journal(Settings(), journal))
    assert checks[0].status == "pass"
    assert "closed_trades=0" in checks[0].detail
    assert "open=0" in checks[0].detail


# --------------------------------------------------------------- risk policy

def test_risk_policy_all_enforced_pass():
    checks = doctor.check_risk_policy(Settings())
    assert all(c.status == "pass" for c in checks)


def test_risk_policy_disabled_flag_warns():
    settings = Settings(risk=RiskLimits(require_news_check=False))
    checks = doctor.check_risk_policy(settings)
    by_name = {c.name: c for c in checks}
    assert by_name["policy:require_news_check"].status == "warn"


# --------------------------------------------------------------- traderspost

def test_traderspost_shape_fails_when_unset():
    checks = doctor.check_traderspost_shape(Settings())
    assert checks[0].status == "fail"


def test_traderspost_shape_fails_on_non_https():
    settings = Settings(traderspost_webhook_url="http://hooks.example/x")
    checks = doctor.check_traderspost_shape(settings)
    assert checks[0].status == "fail"


def test_traderspost_shape_passes_and_never_probes_connectivity():
    settings = Settings(traderspost_webhook_url="https://hooks.traderspost.io/abc")
    checks = doctor.check_traderspost_shape(settings)
    by_name = {c.name: c for c in checks}
    assert by_name["traderspost_config"].status == "pass"
    assert by_name["traderspost_connectivity"].status == "skip"


# --------------------------------------------------------------- claude

def test_claude_skips_when_no_key():
    checks = run(doctor.check_claude(Settings()))
    assert checks[0].status == "skip"


# --------------------------------------------------------------- orchestration

def test_run_all_on_fully_unconfigured_settings_never_raises(journal, tmp_path, monkeypatch):
    settings = Settings(journal_db_path=str(tmp_path / "trades.db"))
    checks = run(doctor.run_all(settings, "SPY", Timeframe.M5, include_claude=False))
    assert any(c.status == "fail" for c in checks)
    assert any(c.name == "claude_api" and c.status == "skip" for c in checks)


def test_run_all_closes_provider_even_on_quote_failure(monkeypatch, tmp_path):
    closed = {"value": False}

    class FakeProvider:
        name = "fake"
        supports_quotes = True

        async def candles(self, symbol, timeframe, start, end):
            from datetime import datetime, timezone

            from trading_system.models import Candle

            return [Candle(timestamp=datetime.now(timezone.utc),
                           open=1, high=1, low=1, close=1, volume=1)]

        async def latest_quote(self, symbol):
            raise RuntimeError("boom")

        async def close(self):
            closed["value"] = True

    monkeypatch.setattr("trading_system.pipeline.build_provider",
                        lambda settings: FakeProvider())
    settings = Settings(journal_db_path=str(tmp_path / "trades.db"))
    run(doctor.run_all(settings, "SPY", Timeframe.M5, include_claude=False))
    assert closed["value"]


# --------------------------------------------------------------- report formatting

def test_format_report_verdict_not_ready_on_any_fail():
    checks = [doctor.DoctorCheck("x", "fail", "broken", "fix it")]
    text = doctor.format_report(checks, "SPY", "5m")
    assert "NOT READY" in text
    assert "fix it" in text


def test_format_report_verdict_ready_with_caveats_on_warn_only():
    checks = [doctor.DoctorCheck("x", "pass"), doctor.DoctorCheck("y", "warn", "meh", "check it")]
    text = doctor.format_report(checks, "SPY", "5m")
    assert "READY WITH CAVEATS" in text


def test_format_report_verdict_ready_when_all_pass():
    checks = [doctor.DoctorCheck("x", "pass"), doctor.DoctorCheck("y", "skip")]
    text = doctor.format_report(checks, "SPY", "5m")
    assert text.strip().endswith("=" * 66)
    assert "READY — every checked integration resolved cleanly." in text


def test_check_to_dict_is_json_serializable():
    import json

    checks = [doctor.DoctorCheck("x", "pass", "detail", "")]
    json.dumps([c.to_dict() for c in checks])
