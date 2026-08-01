from datetime import datetime, timedelta, timezone

import pytest

from trading_system.config import RiskLimits, Settings
from trading_system.decision import TradeAction, TradeDecision, position_size
from trading_system.safety import (
    AccountState,
    BreakerState,
    MarketState,
    SafetyLayer,
)

EQUITY = 25_000.0


def settings() -> Settings:
    return Settings(risk=RiskLimits(account_equity=EQUITY))


def good_decision() -> TradeDecision:
    entry, stop, target = 100.0, 99.0, 103.0
    return TradeDecision(
        symbol="SPY", action=TradeAction.BUY, entry=entry, stop=stop, target=target,
        risk_pct=1.0, quantity=position_size(EQUITY, 1.0, entry, stop),
        probability=0.72, reasoning="test",
    )


def healthy_account() -> AccountState:
    return AccountState(equity=EQUITY, high_water_mark=EQUITY)


def calm_market() -> MarketState:
    return MarketState(bid=99.99, ask=100.01, atr_pct=1.0, baseline_atr_pct=1.0,
                       relative_volume=1.2, is_market_open=True)


def test_clean_setup_is_approved():
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), calm_market())
    assert verdict.approved, verdict.failures
    assert verdict.breaker == BreakerState.TRADING_ALLOWED


def test_no_trade_decision_is_never_approved():
    d = TradeDecision(symbol="SPY", action=TradeAction.NO_TRADE)
    verdict = SafetyLayer(settings()).evaluate(d, healthy_account(), calm_market())
    assert not verdict.approved


def test_daily_loss_limit_halts_trading():
    acct = healthy_account()
    acct.daily_pnl = -EQUITY * 0.031  # past the 3% cap
    layer = SafetyLayer(settings())
    assert layer.breaker_state(acct) == BreakerState.HALTED
    verdict = layer.evaluate(good_decision(), acct, calm_market())
    assert not verdict.approved
    assert any(c.name == "circuit_breaker" for c in verdict.failures)


def test_drawdown_from_high_water_mark_halts_trading():
    acct = AccountState(equity=EQUITY * 0.89, high_water_mark=EQUITY)
    assert SafetyLayer(settings()).breaker_state(acct) == BreakerState.HALTED


def test_losing_streak_triggers_cooldown_then_releases():
    now = datetime.now(timezone.utc)
    acct = healthy_account()
    acct.consecutive_losses = 2
    acct.last_loss_at = now - timedelta(hours=2)
    layer = SafetyLayer(settings())
    assert layer.breaker_state(acct, now) == BreakerState.COOLDOWN

    acct.last_loss_at = now - timedelta(hours=30)
    assert layer.breaker_state(acct, now) == BreakerState.TRADING_ALLOWED


def test_news_blackout_blocks_entry():
    market = calm_market()
    market.high_impact_news_within_minutes = 5.0
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    assert any(c.name == "news_events" for c in verdict.failures)


def test_wide_spread_blocks_entry():
    market = calm_market()
    market.bid, market.ask = 99.0, 100.5   # 1.5% spread
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "spread" for c in verdict.failures)


def test_max_open_positions_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "open_positions" for c in verdict.failures)


def test_correlated_exposure_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [{"symbol": "QQQ"}]   # same group as SPY
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "correlation" for c in verdict.failures)


def test_duplicate_symbol_position_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [{"symbol": "SPY"}]
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "duplicate_position" for c in verdict.failures)


def test_poor_risk_reward_blocks_entry():
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=100, stop=99,
                      target=101.2, risk_pct=1.0,
                      quantity=position_size(EQUITY, 1.0, 100, 99), probability=0.8)
    verdict = SafetyLayer(settings()).evaluate(d, healthy_account(), calm_market())
    assert any(c.name == "risk_reward" for c in verdict.failures)


def test_low_probability_blocks_entry():
    d = good_decision().model_copy(update={"probability": 0.4})
    verdict = SafetyLayer(settings()).evaluate(d, healthy_account(), calm_market())
    assert any(c.name == "probability" for c in verdict.failures)


def test_volatility_spike_blocks_entry():
    market = calm_market()
    market.atr_pct, market.baseline_atr_pct = 5.0, 1.0   # 5x normal
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "volatility" for c in verdict.failures)


def test_market_closed_blocks_entry():
    market = calm_market()
    market.is_market_open = False
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "trading_hours" for c in verdict.failures)


def test_oversized_position_blocks_entry():
    d = good_decision().model_copy(update={"quantity": 5_000.0})  # implies $5k risk
    verdict = SafetyLayer(settings()).evaluate(d, healthy_account(), calm_market())
    assert any(c.name == "position_size" for c in verdict.failures)


def test_verdict_is_json_serializable():
    import json

    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), calm_market())
    json.dumps(verdict.to_dict())
