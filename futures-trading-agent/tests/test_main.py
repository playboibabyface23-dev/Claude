"""End-to-end wiring: market data -> indicators -> AI -> risk -> execution ->
database, exercised with every external dependency faked so it runs fully
offline."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from futures_agent.config.settings import LucidEvalSettings, RiskLimits, Settings
from futures_agent.main import Agent, MIN_BARS_TO_ANALYZE
from futures_agent.market.models import Candle

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


def candles(n: int = 40) -> list[Candle]:
    start = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    price = 100.0
    out = []
    for i in range(n):
        price += 0.1
        out.append(Candle(timestamp=start + timedelta(minutes=5 * i), open=price,
                          high=price + 1, low=price - 1, close=price + 0.2, volume=1000))
    return out


class FakeBarsProvider:
    def __init__(self, bars: list[Candle]) -> None:
        self._bars = bars

    async def candles(self, symbol, timeframe, start, end):
        return self._bars


class FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason=None) -> None:
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response) -> None:
        self._response = response

    def create(self, **kwargs):
        return self._response


class FakeAIClient:
    def __init__(self, payload_or_text) -> None:
        if isinstance(payload_or_text, dict):
            text = json.dumps(payload_or_text)
        else:
            text = payload_or_text
        response = FakeResponse(content=[FakeBlock("text", text)])
        self.messages = FakeMessages(response)

        class _Beta:
            pass

        self.beta = _Beta()
        self.beta.messages = self.messages


class FakeTradovate:
    def __init__(self, raise_on_place=None) -> None:
        self.calls: list[dict] = []
        self._raise_on_place = raise_on_place
        self.closed = False

    async def find_contract(self, symbol):
        return {"id": 1, "name": symbol}

    async def get_account(self):
        return {"id": 1}

    async def place_order(self, **kwargs):
        if self._raise_on_place:
            raise self._raise_on_place
        self.calls.append(kwargs)
        return {"orderId": 555}

    async def list_positions(self, account_id):
        return []

    async def close(self):
        self.closed = True


HOLD_PAYLOAD = {"symbol": "MNQ", "action": "HOLD", "confidence": 30,
               "entry_reason": "no setup", "stop_loss": 0, "take_profit": 0}

BUY_PAYLOAD = {"symbol": "MNQ", "action": "BUY", "confidence": 90,
              "entry_reason": "confluence", "stop_loss": 20, "take_profit": 60}


def make_settings(tmp_path, lucid: LucidEvalSettings | None = None, **risk_kw) -> Settings:
    return Settings(
        anthropic_api_key="k", execution_mode="tradovate",
        traded_symbols=("MNQ",), database_path=str(tmp_path / "agent.db"),
        kill_switch_file=str(tmp_path / "KILL_SWITCH"),
        dashboard_port=0,   # OS-assigned ephemeral port, avoids test collisions
        risk=RiskLimits(account_equity=50_000.0, risk_pct_per_trade=0.5, **risk_kw),
        lucid=lucid or LucidEvalSettings(),
    )


def make_agent(tmp_path, ai_payload, bars=None, tradovate=None, **risk_kw) -> tuple[Agent, FakeTradovate]:
    settings = make_settings(tmp_path, **risk_kw)
    tv = tradovate if tradovate is not None else FakeTradovate()
    agent = Agent(settings, bars_provider=FakeBarsProvider(bars if bars is not None else candles()),
                 tradovate_client=tv, ai_client=FakeAIClient(ai_payload))
    return agent, tv


# --------------------------------------------------------------- run_cycle

def test_skips_when_insufficient_history(tmp_path):
    agent, tv = make_agent(tmp_path, BUY_PAYLOAD, bars=candles(5))
    run(agent.run_cycle("MNQ"))
    assert tv.calls == []
    assert agent.db.stats()["total_trades"] == 0
    agent.db.close()


def test_hold_decision_is_recorded_but_not_traded(tmp_path):
    agent, tv = make_agent(tmp_path, HOLD_PAYLOAD)
    run(agent.run_cycle("MNQ"))
    assert tv.calls == []
    assert agent.db.stats()["total_trades"] == 0
    history = agent.db.confidence_history()
    assert history[0]["action"] == "HOLD"
    agent.db.close()


def test_approved_buy_decision_is_executed_and_recorded(tmp_path):
    agent, tv = make_agent(tmp_path, BUY_PAYLOAD)
    run(agent.run_cycle("MNQ"))
    assert len(tv.calls) == 1
    assert tv.calls[0]["action"] == "Buy"

    trades = agent.db.recent_trades(limit=1)
    assert len(trades) == 1
    assert trades[0]["symbol"] == "MNQ"
    assert trades[0]["status"] == "open"
    assert trades[0]["ai_confidence"] == 90.0
    # 50000 * 0.5% = $250 budget; 20pt stop on MNQ = $40/contract -> 6 contracts.
    assert trades[0]["contracts"] == 6
    agent.db.close()


def test_low_confidence_decision_is_rejected_without_executing(tmp_path):
    low_conf = dict(BUY_PAYLOAD, confidence=10)
    agent, tv = make_agent(tmp_path, low_conf)
    run(agent.run_cycle("MNQ"))
    assert tv.calls == []
    rejections = agent.db.recent_rejections()
    assert rejections[0]["reason"] == "confidence"
    agent.db.close()


def test_malformed_ai_output_is_rejected_without_executing(tmp_path):
    agent, tv = make_agent(tmp_path, "not valid json{{")
    run(agent.run_cycle("MNQ"))
    assert tv.calls == []
    rejections = agent.db.recent_rejections()
    assert rejections[0]["reason"] == "malformed_ai_output"
    assert agent.db.stats()["total_trades"] == 0
    agent.db.close()


def test_execution_failure_is_recorded_as_a_rejection_not_a_crash(tmp_path):
    tv = FakeTradovate(raise_on_place=RuntimeError("margin call"))
    agent, _ = make_agent(tmp_path, BUY_PAYLOAD, tradovate=tv)
    run(agent.run_cycle("MNQ"))   # must not raise
    rejections = agent.db.recent_rejections()
    assert rejections[0]["reason"] == "execution_error"
    assert agent.db.stats()["total_trades"] == 0
    agent.db.close()


def test_kill_switch_blocks_new_trades(tmp_path):
    agent, tv = make_agent(tmp_path, BUY_PAYLOAD)
    agent.risk_manager.trip_kill_switch("test")
    run(agent.run_cycle("MNQ"))
    assert tv.calls == []
    rejections = agent.db.recent_rejections()
    assert rejections[0]["reason"] == "kill_switch"
    agent.db.close()


def test_max_open_positions_blocks_a_second_entry_same_symbol(tmp_path):
    agent, tv = make_agent(tmp_path, BUY_PAYLOAD, max_open_positions=1)
    run(agent.run_cycle("MNQ"))
    assert len(tv.calls) == 1
    run(agent.run_cycle("MNQ"))   # still open — second entry must be blocked
    assert len(tv.calls) == 1
    rejections = agent.db.recent_rejections()
    assert rejections[0]["reason"] == "max_open_positions"
    agent.db.close()


# --------------------------------------------------------------- Lucid eval guard

def test_lucid_guard_not_built_when_disabled(tmp_path):
    agent, _ = make_agent(tmp_path, HOLD_PAYLOAD)
    assert agent.lucid_guard is None
    assert agent._check_lucid_eval() is None
    agent.db.close()


def test_lucid_guard_built_when_enabled(tmp_path):
    settings = make_settings(tmp_path, lucid=LucidEvalSettings(enabled=True))
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD))
    assert agent.lucid_guard is not None
    agent.db.close()


def test_lucid_guard_trips_kill_switch_on_trailing_drawdown_breach(tmp_path):
    settings = make_settings(tmp_path, lucid=LucidEvalSettings(
        enabled=True, starting_balance=50_000.0, trailing_drawdown_amount=2_000.0))
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD))
    agent.db.record_daily_equity("2020-01-01", 52_000.0)   # highest EOD close so far
    agent._equity = 49_500.0   # below the 50,000 trailing floor (52,000 - 2,000)
    reason = agent._check_lucid_eval()
    assert reason is not None
    assert "trailing_drawdown" in reason
    assert agent.risk_manager.kill_switch_active()
    agent.db.close()


def test_lucid_guard_approves_when_within_limits(tmp_path):
    settings = make_settings(tmp_path, lucid=LucidEvalSettings(enabled=True))
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD))
    assert agent._check_lucid_eval() is None
    assert not agent.risk_manager.kill_switch_active()
    agent.db.close()


def test_run_forever_records_daily_equity_each_cycle(tmp_path):
    settings = make_settings(tmp_path)
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD))

    original_run_cycle = agent.run_cycle

    async def run_cycle_then_stop(symbol):
        await original_run_cycle(symbol)
        agent.stop()   # let the loop finish this iteration's bookkeeping, then exit

    agent.run_cycle = run_cycle_then_stop
    run(agent.run_forever())

    today = datetime.now(UTC).date().isoformat()
    history = agent.db.daily_equity_history()
    assert any(h["date"] == today for h in history)
    run(agent.close())


# --------------------------------------------------------------- lifecycle

def test_close_shuts_down_the_broker_client(tmp_path):
    agent, tv = make_agent(tmp_path, HOLD_PAYLOAD)
    run(agent.close())
    assert tv.closed


def test_account_state_reflects_database(tmp_path):
    agent, tv = make_agent(tmp_path, BUY_PAYLOAD)
    run(agent.run_cycle("MNQ"))
    state = agent._account_state()
    assert state.open_positions == 1
    assert state.trades_today == 1
    agent.db.close()


# --------------------------------------------------------------- live feed wiring

class FakeLiveFeed:
    def __init__(self, candles_by_symbol=None, raise_on_start=None) -> None:
        self._candles = candles_by_symbol or {}
        self._raise_on_start = raise_on_start
        self.started: list[str] = []
        self.closed = False

    async def start(self, symbol: str) -> None:
        if self._raise_on_start:
            raise self._raise_on_start
        self.started.append(symbol)

    def candles(self, symbol: str):
        return self._candles.get(symbol, [])

    async def close(self) -> None:
        self.closed = True


def test_agent_builds_a_live_feed_automatically_when_tradovate_is_configured(tmp_path):
    from futures_agent.config.settings import TradovateCredentials

    settings = Settings(
        anthropic_api_key="k", execution_mode="tradovate",
        traded_symbols=("MNQ",), database_path=str(tmp_path / "agent.db"),
        kill_switch_file=str(tmp_path / "KILL_SWITCH"), dashboard_port=0,
        risk=RiskLimits(account_equity=50_000.0, risk_pct_per_trade=0.5),
        tradovate=TradovateCredentials(environment="demo", username="u", password="p",
                                      cid="1", sec="s"),
    )
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD))
    assert agent.live_feed is not None
    agent.db.close()


def test_agent_has_no_live_feed_when_tradovate_is_not_configured(tmp_path):
    agent, _ = make_agent(tmp_path, HOLD_PAYLOAD)
    assert agent.live_feed is None
    agent.db.close()


def test_refresh_history_prefers_the_live_feed_over_the_csv_provider(tmp_path):
    settings = make_settings(tmp_path)
    live_candles = candles(30)
    live_feed = FakeLiveFeed(candles_by_symbol={"MNQ": live_candles})
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles(5)),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD),
                 live_feed=live_feed)
    run(agent.refresh_history("MNQ"))
    assert live_feed.started == ["MNQ"]
    assert len(agent.history["MNQ"]) == 30   # from the live feed, not the 5-bar CSV fixture
    agent.db.close()


def test_refresh_history_falls_back_to_csv_when_live_feed_fails(tmp_path):
    settings = make_settings(tmp_path)
    live_feed = FakeLiveFeed(raise_on_start=RuntimeError("socket unavailable"))
    fallback_candles = candles(30)
    agent = Agent(settings, bars_provider=FakeBarsProvider(fallback_candles),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD),
                 live_feed=live_feed)
    run(agent.refresh_history("MNQ"))   # must not raise
    assert len(agent.history["MNQ"]) == 30   # fell back to the CSV provider
    agent.db.close()


def test_close_closes_the_live_feed(tmp_path):
    settings = make_settings(tmp_path)
    live_feed = FakeLiveFeed()
    agent = Agent(settings, bars_provider=FakeBarsProvider(candles()),
                 tradovate_client=FakeTradovate(), ai_client=FakeAIClient(HOLD_PAYLOAD),
                 live_feed=live_feed)
    run(agent.close())
    assert live_feed.closed
