"""Dashboard snapshot assembly and rendering."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from trading_system.config import RiskLimits, Settings
from trading_system.dashboard.server import render_page
from trading_system.dashboard.state import build_snapshot, policy_view
from trading_system.decision import TradeAction, TradeDecision
from trading_system.memory import TradeJournal, TradeRecord

EQUITY = 100_000.0


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(str(tmp_path / "trades.db"))
    yield j
    j.close()


def settings(tmp_path=None, **risk) -> Settings:
    return Settings(risk=RiskLimits(account_equity=EQUITY, **risk))


def snapshot(journal, **kw):
    return asyncio.run(build_snapshot(settings(), journal, None, kw.get("analysis")))


def a_trade(symbol="SPY") -> TradeDecision:
    return TradeDecision(symbol=symbol, action=TradeAction.BUY, entry=100.0,
                         stop=99.0, target=103.0, risk_pct=1.0, quantity=250,
                         probability=0.7, reasoning="test setup")


def test_empty_journal_still_renders():
    """A fresh install has no trades; the dashboard must not blow up."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        j = TradeJournal(f"{tmp}/t.db")
        try:
            snap = snapshot(j)
            assert snap["journal"]["recent"] == []
            assert snap["gates"]["checks"] == []
            html = render_page(snap)
            assert "Control" in html
        finally:
            j.close()


def test_snapshot_reports_account_and_breaker(journal):
    snap = snapshot(journal)
    acct = snap["account"]
    assert acct["equity"] == pytest.approx(EQUITY)
    assert acct["breaker"] == "trading_allowed"
    assert acct["breaker_ok"] is True


def test_drawdown_reflects_high_water_mark(journal):
    tid = journal.record_trade(TradeRecord.from_decision(a_trade()))
    journal.close_trade(tid, exit_price=96.0, pnl=-8_000.0, outcome="loss")
    journal.update_high_water_mark(EQUITY)

    snap = snapshot(journal)
    assert snap["account"]["equity"] == pytest.approx(92_000.0)
    assert snap["account"]["drawdown_pct"] == pytest.approx(8.0, abs=0.01)
    # An 8% loss today trips the 3% daily-loss halt long before the 10%
    # drawdown cap — the tighter limit is meant to fire first.
    assert snap["account"]["breaker"] == "halted"


def test_equity_uses_all_time_pnl_not_week_to_date(journal):
    """A rolling window would reset equity every Monday and hide a long
    drawdown from the drawdown check."""
    from datetime import timedelta

    old = datetime.now(timezone.utc) - timedelta(days=90)
    tid = journal.record_trade(TradeRecord.from_decision(a_trade()))
    journal.close_trade(tid, exit_price=96.0, pnl=-12_000.0, outcome="loss",
                        closed_at=old)
    journal.update_high_water_mark(EQUITY)

    snap = snapshot(journal)
    assert snap["account"]["weekly_pnl"] == pytest.approx(0.0)   # outside the window
    assert snap["account"]["equity"] == pytest.approx(88_000.0)  # still down
    assert snap["account"]["drawdown_pct"] == pytest.approx(12.0, abs=0.01)
    assert snap["account"]["breaker"] == "halted"                # 12% > 10% cap


def test_gate_verdict_and_failure_counts_surface(journal):
    journal.record_gate_decision("QQQ", {"approved": False, "breaker": "trading_allowed",
        "checks": [{"name": "news_events", "passed": False, "detail": "CPI in 5 min"},
                   {"name": "spread", "passed": True, "detail": "ok"}]})
    journal.record_gate_decision("SPY", {"approved": False, "breaker": "trading_allowed",
        "checks": [{"name": "news_events", "passed": False, "detail": "NFP in 9 min"}]})

    snap = snapshot(journal)
    assert snap["gates"]["approved"] is False
    assert snap["gates"]["symbol"] == "SPY"          # most recent first
    # The rejection histogram is what tells the operator what to tune.
    assert snap["gates"]["failure_counts"]["news_events"] == 2


def test_equity_curve_accumulates_closed_trades(journal):
    for pnl in (500.0, -200.0, 750.0):
        tid = journal.record_trade(TradeRecord.from_decision(a_trade()))
        journal.close_trade(tid, exit_price=101.0, pnl=pnl,
                            outcome="win" if pnl > 0 else "loss")
    curve = snapshot(journal)["journal"]["equity_curve"]
    assert [round(p["equity"]) for p in curve] == [100000, 100500, 100300, 101050]


def test_open_trade_appears_in_journal_but_not_as_closed(journal):
    journal.record_trade(TradeRecord.from_decision(a_trade()))
    snap = snapshot(journal)
    assert snap["journal"]["recent"][0]["outcome"] == "open"
    assert snap["journal"]["stats"]["total_closed"] == 0


def test_confluence_json_is_decoded_not_left_as_string(journal):
    d = a_trade().model_copy(update={"confluence": []})
    journal.record_trade(TradeRecord.from_decision(d))
    row = snapshot(journal)["journal"]["recent"][0]
    assert isinstance(row["confluence"], list)
    assert isinstance(row["structure"], dict)


def test_policy_view_exposes_failsafe_flags():
    view = policy_view(settings())
    names = {p["name"]: p["on"] for p in view["policies"]}
    assert names["Require quote"] is True
    assert names["Require news check"] is True
    assert any(l["name"] == "Max drawdown" for l in view["limits"])


def test_policy_view_reflects_a_disabled_flag():
    view = policy_view(Settings(risk=RiskLimits(require_news_check=False)))
    assert {p["name"]: p["on"] for p in view["policies"]}["Require news check"] is False


def test_snapshot_is_json_serializable(journal):
    journal.record_trade(TradeRecord.from_decision(a_trade()))
    json.dumps(snapshot(journal), default=str)


def test_render_page_embeds_snapshot_and_escapes_nothing_dangerous(journal):
    snap = snapshot(journal)
    html = render_page(snap)
    assert "window.__SNAPSHOT__" in html
    assert html.startswith("<!doctype html>")
    # The payload must be valid JSON inside the script tag.
    payload = html.split("window.__SNAPSHOT__ = ", 1)[1].split(";</script>", 1)[0]
    json.loads(payload)


def test_live_analysis_overrides_persisted_verdict(journal):
    journal.record_gate_decision("SPY", {"approved": True, "checks": [
        {"name": "spread", "passed": True, "detail": "stale"}]})
    live = {"verdict": {"approved": False, "checks": [
        {"name": "news_events", "passed": False, "detail": "fresh"}]}}
    snap = asyncio.run(build_snapshot(settings(), journal, None, live))
    assert snap["gates"]["approved"] is False
    assert snap["gates"]["checks"][0]["detail"] == "fresh"
