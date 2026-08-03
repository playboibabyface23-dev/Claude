from datetime import datetime, timedelta, timezone

import pytest

from trading_system.broker import Position, PositionSide
from trading_system.config import RiskLimits, Settings
from trading_system.decision import TradeAction, TradeDecision, position_size
from trading_system.market_hours import MarketStatus
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
                relative_volume=1.2, news_checked=True,
                high_impact_news_within_minutes=None,
                market_status=MarketStatus(is_open=True, source="test"),
                last_candle_age_seconds=60.0, expected_bar_seconds=300.0)
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


# --------------------------------------------------------------- breaker detail

def test_breaker_detail_trading_allowed_has_no_reactivation_time():
    detail = SafetyLayer(settings()).breaker_detail(healthy_account())
    assert detail.state == BreakerState.TRADING_ALLOWED
    assert detail.reactivates_at is None


def test_breaker_detail_daily_halt_reactivates_at_next_midnight_utc():
    now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
    acct = healthy_account()
    acct.daily_pnl = -EQUITY * 0.031
    detail = SafetyLayer(settings()).breaker_detail(acct, now)
    assert detail.state == BreakerState.HALTED
    assert "daily loss" in detail.reason
    assert detail.reactivates_at == datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)


def test_breaker_detail_weekly_halt_reactivates_next_monday():
    # 2026-08-03 is a Monday; a breach then should clear the following Monday.
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    acct = healthy_account()
    acct.weekly_pnl = -EQUITY * 0.061
    detail = SafetyLayer(settings()).breaker_detail(acct, now)
    assert detail.state == BreakerState.HALTED
    assert "weekly loss" in detail.reason
    assert detail.reactivates_at == datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    assert detail.reactivates_at.weekday() == 0


def test_breaker_detail_drawdown_halt_has_no_reactivation_time():
    """A drawdown breach has no calendar boundary — it only clears once equity
    itself recovers, so there is nothing to schedule."""
    acct = AccountState(equity=EQUITY * 0.89, high_water_mark=EQUITY)
    detail = SafetyLayer(settings()).breaker_detail(acct)
    assert detail.state == BreakerState.HALTED
    assert "drawdown" in detail.reason
    assert detail.reactivates_at is None


def test_breaker_detail_cooldown_reactivates_exactly_at_the_cooldown_boundary():
    now = datetime.now(timezone.utc)
    acct = healthy_account()
    acct.consecutive_losses = 2
    acct.last_loss_at = now - timedelta(hours=2)
    detail = SafetyLayer(settings()).breaker_detail(acct, now)
    assert detail.state == BreakerState.COOLDOWN
    assert detail.reactivates_at == acct.last_loss_at + timedelta(hours=24)


def test_breaker_state_and_breaker_detail_never_disagree():
    """breaker_state is a thin wrapper — it must not drift from breaker_detail."""
    now = datetime.now(timezone.utc)
    layer = SafetyLayer(settings())
    for acct in (
        healthy_account(),
        AccountState(equity=EQUITY, high_water_mark=EQUITY, daily_pnl=-EQUITY * 0.05),
        AccountState(equity=EQUITY * 0.85, high_water_mark=EQUITY),
    ):
        assert layer.breaker_state(acct, now) == layer.breaker_detail(acct, now).state


def test_news_blackout_blocks_entry():
    market = calm_market(high_impact_news_within_minutes=5.0)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    assert any(c.name == "news_events" for c in verdict.failures)


def test_news_just_released_also_blocks():
    """The minutes after a release are as violent as the minutes before."""
    market = calm_market(high_impact_news_within_minutes=-3.0)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "news_events" for c in verdict.failures)


def test_distant_news_does_not_block():
    market = calm_market(high_impact_news_within_minutes=180.0)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


def test_unchecked_news_blocks_by_default():
    """No calendar configured is 'unknown', not 'clear'."""
    market = calm_market(news_checked=False)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    failure = next(c for c in verdict.failures if c.name == "news_events")
    assert "REQUIRE_NEWS_CHECK" in failure.detail   # tell the user how to proceed


def test_unchecked_news_allowed_when_policy_opts_out():
    limits = RiskLimits(account_equity=EQUITY, require_news_check=False)
    market = calm_market(news_checked=False)
    verdict = SafetyLayer(Settings(risk=limits)).evaluate(
        good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


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
    market = calm_market(atr_pct=5.0, baseline_atr_pct=1.0)   # 5x normal
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "volatility" for c in verdict.failures)


def test_missing_volatility_baseline_blocks_by_default():
    market = calm_market(baseline_atr_pct=None)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "volatility" for c in verdict.failures)


def test_missing_volatility_baseline_allowed_when_policy_opts_out():
    limits = RiskLimits(account_equity=EQUITY, require_volatility_baseline=False)
    market = calm_market(baseline_atr_pct=None)
    verdict = SafetyLayer(Settings(risk=limits)).evaluate(
        good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


def test_market_closed_blocks_entry():
    market = calm_market(market_status=MarketStatus(
        is_open=False, reason="after close", source="test"))
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert any(c.name == "trading_hours" for c in verdict.failures)


def test_holiday_blocks_entry():
    market = calm_market(market_status=MarketStatus(
        is_open=False, is_holiday=True, reason="Good Friday", source="test"))
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    failure = next(c for c in verdict.failures if c.name == "trading_hours")
    assert "Good Friday" in failure.detail


def test_undetermined_market_session_blocks_by_default():
    """No calendar answered — 'unknown' must not read as 'open'."""
    market = calm_market(market_status=None)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    assert any(c.name == "trading_hours" for c in verdict.failures)


def test_undetermined_market_session_allowed_when_policy_opts_out():
    limits = RiskLimits(account_equity=EQUITY, require_market_hours=False)
    market = calm_market(market_status=None)
    verdict = SafetyLayer(Settings(risk=limits)).evaluate(
        good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


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


def test_stale_feed_during_open_session_blocks_as_possible_halt():
    """A calendar cannot know about a halt or a dead feed; silent data during
    an open session is the observable symptom of both."""
    market = calm_market(last_candle_age_seconds=3600, expected_bar_seconds=300)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert not verdict.approved
    failure = next(c for c in verdict.failures if c.name == "data_heartbeat")
    assert "halt" in failure.detail


def test_fresh_feed_passes_heartbeat():
    market = calm_market(last_candle_age_seconds=120, expected_bar_seconds=300)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures


def test_heartbeat_not_applied_when_session_closed():
    """Stale data outside a session is expected, not a halt signal."""
    market = calm_market(
        market_status=MarketStatus(is_open=False, reason="after close", source="test"),
        last_candle_age_seconds=50_000, expected_bar_seconds=300)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    names = {c.name for c in verdict.failures}
    assert "data_heartbeat" not in names
    assert "trading_hours" in names


def test_heartbeat_tolerates_one_slow_bar():
    market = calm_market(last_candle_age_seconds=700, expected_bar_seconds=300)
    verdict = SafetyLayer(settings()).evaluate(good_decision(), healthy_account(), market)
    assert verdict.approved, verdict.failures
