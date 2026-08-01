"""AccountState assembly: the seam where broker, journal, and risk limits meet.

These cover the bug class that made the safety layer decorative — position
count always zero, and a high-water mark equal to current equity so the
drawdown check could never fire.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_system.account import build_account_state
from trading_system.broker import AlpacaBroker, ReconcilingPositionProvider
from trading_system.config import RiskLimits, Settings
from trading_system.decision import TradeAction, TradeDecision
from trading_system.memory import TradeJournal, TradeRecord
from trading_system.safety import BreakerState, SafetyLayer

EQUITY = 100_000.0


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(str(tmp_path / "trades.db"))
    yield j
    j.close()


def settings() -> Settings:
    return Settings(risk=RiskLimits(account_equity=EQUITY))


def broker_with(positions_json, account_json=None) -> AlpacaBroker:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=positions_json)
        return httpx.Response(200, json=account_json or {"equity": str(EQUITY)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://paper-api.alpaca.markets")
    return AlpacaBroker("k", "s", client=client)


def build(journal, provider):
    return asyncio.run(build_account_state(journal, provider, settings()))


def test_open_positions_reach_account_state(journal):
    provider = ReconcilingPositionProvider(journal, broker_with([
        {"symbol": "AAPL", "qty": "10", "side": "long", "avg_entry_price": "150"},
    ]))
    state, _ = build(journal, provider)
    assert [p.symbol for p in state.open_positions] == ["AAPL"]
    assert not state.positions_degraded


def test_equity_comes_from_broker_when_available(journal):
    provider = ReconcilingPositionProvider(
        journal, broker_with([], {"equity": "87500.50", "last_equity": "90000"})
    )
    state, _ = build(journal, provider)
    assert state.equity == pytest.approx(87500.50)


def test_high_water_mark_ratchets_and_enables_drawdown_check(journal):
    # Peak at 100k...
    provider = ReconcilingPositionProvider(journal, broker_with([], {"equity": "100000"}))
    state, _ = build(journal, provider)
    assert state.high_water_mark == pytest.approx(100_000.0)

    # ...then drop 12%. The mark must stay at the peak, not follow equity down.
    provider = ReconcilingPositionProvider(journal, broker_with([], {"equity": "88000"}))
    state, _ = build(journal, provider)
    assert state.high_water_mark == pytest.approx(100_000.0)
    assert state.equity == pytest.approx(88_000.0)

    # 12% drawdown exceeds the 10% cap -> halted.
    assert SafetyLayer(settings()).breaker_state(state) == BreakerState.HALTED


def test_high_water_mark_persists_across_journal_instances(tmp_path):
    path = str(tmp_path / "trades.db")
    j1 = TradeJournal(path)
    j1.update_high_water_mark(120_000.0)
    j1.close()

    j2 = TradeJournal(path)
    assert j2.high_water_mark() == pytest.approx(120_000.0)
    # A lower equity reading must not lower the mark.
    assert j2.update_high_water_mark(90_000.0) == pytest.approx(120_000.0)
    j2.close()


def test_realized_pnl_flows_into_daily_and_weekly(journal):
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=100, stop=99,
                      target=103, risk_pct=1.0, quantity=250, probability=0.7)
    tid = journal.record_trade(TradeRecord.from_decision(d))
    journal.close_trade(tid, exit_price=98.0, pnl=-500.0, outcome="loss",
                        closed_at=datetime.now(timezone.utc))

    provider = ReconcilingPositionProvider(journal, broker_with([]))
    state, _ = build(journal, provider)
    assert state.daily_pnl == pytest.approx(-500.0)
    assert state.weekly_pnl == pytest.approx(-500.0)
    assert state.consecutive_losses == 1


def test_degraded_broker_marks_state_and_blocks_trading(journal):
    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom),
                               base_url="https://paper-api.alpaca.markets")
    provider = ReconcilingPositionProvider(journal, AlpacaBroker("k", "s", client=client))
    state, report = build(journal, provider)

    assert state.positions_degraded
    assert report.degraded

    from trading_system.decision import position_size
    from trading_system.safety import MarketState

    decision = TradeDecision(
        symbol="SPY", action=TradeAction.BUY, entry=100, stop=99, target=103,
        risk_pct=1.0, quantity=position_size(EQUITY, 1.0, 100, 99), probability=0.7,
    )
    verdict = SafetyLayer(settings()).evaluate(decision, state, MarketState())
    assert not verdict.approved
    assert any(c.name == "position_data_fresh" for c in verdict.failures)


def test_without_broker_equity_tracks_realized_pnl(journal):
    """No broker API: equity must still move with realized PnL, otherwise the
    drawdown check compares a constant against itself."""
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=100, stop=99,
                      target=103, risk_pct=1.0, quantity=250, probability=0.7)
    tid = journal.record_trade(TradeRecord.from_decision(d))
    journal.close_trade(tid, exit_price=96.0, pnl=-1000.0, outcome="loss")

    provider = ReconcilingPositionProvider(journal, broker=None)
    state, _ = build(journal, provider)
    assert state.equity == pytest.approx(EQUITY - 1000.0)
