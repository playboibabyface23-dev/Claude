from datetime import datetime, timedelta, timezone

import pytest

from trading_system.broker import Position, PositionSide
from trading_system.config import RiskLimits, Settings
from trading_system.decision import TradeAction, TradeDecision, position_size
from trading_system.models import Quote
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


def held(symbol: str, side: PositionSide = PositionSide.LONG) -> Position:
    return Position(symbol=symbol, side=side, quantity=10.0, avg_entry_price=100.0)


def quote(bid: float = 99.99, ask: float = 100.01,
          age_seconds: float = 1.0, symbol: str = "SPY") -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask, provider="test",
                 timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_seconds))


def calm_market(**overrides) -> MarketState:
    base = dict(quote=quote(), atr_pct=1.0, baseline_atr_pct=1.0,
                relative_volume=1.2, is_market_open=True)
    base.update(overrides)
    return MarketState(**base)


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
    market = calm_market(quote=quote(bid=99.0, ask=100.5))   # 1.5% spread
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "spread" for c in verdict.failures)


def test_spread_eating_the_stop_blocks_on_slippage():
    """Spread within the cap but large next to a tight stop: a normal fill
    would consume a big share of the risk budget."""
    limits = RiskLimits(account_equity=EQUITY, max_spread_pct=1.0)
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=100.0,
                      stop=99.9, target=100.5, risk_pct=1.0,
                      quantity=position_size(EQUITY, 1.0, 100.0, 99.9),
                      probability=0.8)
    market = calm_market(quote=quote(bid=99.95, ask=100.05))   # 0.10 spread vs 0.10 stop
    verdict = SafetyLayer(Settings(risk=limits)).evaluate(d, healthy_account(), market)
    assert any(c.name == "slippage" for c in verdict.failures)


def test_missing_quote_blocks_by_default():
    market = calm_market(quote=None, quote_error="provider has no bid/ask")
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    failed = {c.name for c in verdict.failures}
    assert "quote_data" in failed
    # Spread and slippage must not silently pass on a missing quote.
    assert "spread" in failed and "slippage" in failed


def test_missing_quote_allowed_when_policy_opts_out():
    limits = RiskLimits(account_equity=EQUITY, require_quote=False)
    market = calm_market(quote=None, quote_error="finnhub has no bid/ask")
    verdict = SafetyLayer(Settings(risk=limits)).evaluate(
        good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


def test_stale_quote_is_rejected():
    market = calm_market(quote=quote(age_seconds=300))   # 5 minutes old
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "quote_data" for c in verdict.failures)
    assert any("old" in c.detail for c in verdict.failures if c.name == "quote_data")


def test_crossed_book_is_rejected():
    """bid > ask means bad data or a halted/auction market — never trade it."""
    market = calm_market(quote=quote(bid=100.05, ask=99.95))
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    failed = {c.name: c.detail for c in verdict.failures}
    assert "quote_data" in failed
    assert "crossed" in failed["quote_data"]


def test_future_dated_quote_is_rejected_as_untrustworthy():
    market = calm_market(quote=quote(age_seconds=-120))   # clock skew / bad parse
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "quote_data" for c in verdict.failures)


def test_max_open_positions_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [held("AAPL"), held("MSFT")]
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "open_positions" for c in verdict.failures)


def test_correlated_exposure_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [held("QQQ")]   # same group as SPY
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "correlation" for c in verdict.failures)


def test_duplicate_symbol_position_blocks_entry():
    acct = healthy_account()
    acct.open_positions = [held("SPY")]
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


def test_degraded_position_data_blocks_entry():
    """If the broker was unreachable the position set is journal-only, so every
    position-derived check is unreliable and the trade must not proceed."""
    acct = healthy_account()
    acct.positions_degraded = True
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert not verdict.approved
    assert any(c.name == "position_data_fresh" for c in verdict.failures)


def test_untracked_broker_position_counts_against_limits():
    """A position opened outside this system still consumes risk budget."""
    acct = healthy_account()
    acct.open_positions = [held("SPY", PositionSide.SHORT)]
    verdict = SafetyLayer(settings()).evaluate(good_decision(), acct, calm_market())
    assert any(c.name == "duplicate_position" for c in verdict.failures)


def test_verdict_is_json_serializable():
    import json

    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), calm_market())
    json.dumps(verdict.to_dict())
