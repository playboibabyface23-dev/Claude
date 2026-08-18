import pytest

from futures_agent.risk.lucid_eval import (
    LucidEvalConfig,
    LucidEvalGuard,
    consistency_violation,
    trailing_drawdown_floor,
)


def config(**kw) -> LucidEvalConfig:
    base = dict(starting_balance=50_000.0, trailing_drawdown_amount=2_000.0,
               profit_target=3_000.0, max_consistency_pct=30.0, min_trading_days=0)
    base.update(kw)
    return LucidEvalConfig(**base)


# --------------------------------------------------------------- trailing_drawdown_floor

def test_floor_starts_below_starting_balance_with_no_history():
    c = config()
    assert trailing_drawdown_floor(c, []) == 48_000.0


def test_floor_trails_up_with_highest_eod_close():
    c = config()
    assert trailing_drawdown_floor(c, [51_000.0, 50_500.0]) == 49_000.0


def test_floor_never_lowers_after_a_losing_day():
    c = config()
    # Highest EOD close was 52,000 -- a later losing day must not lower the floor.
    assert trailing_drawdown_floor(c, [52_000.0, 47_000.0]) == 50_000.0


# --------------------------------------------------------------- consistency_violation

def test_consistency_violation_none_when_disabled():
    c = config(max_consistency_pct=0)
    assert consistency_violation(c, {"2026-08-01": 5_000.0}) is None


def test_consistency_violation_none_with_even_split():
    c = config(max_consistency_pct=30.0)
    pnl = {"d1": 250.0, "d2": 250.0, "d3": 250.0, "d4": 250.0}
    assert consistency_violation(c, pnl) is None


def test_consistency_violation_flags_dominant_day():
    c = config(max_consistency_pct=30.0)
    pnl = {"d1": 800.0, "d2": 100.0, "d3": 100.0}   # d1 = 80% of profit
    violation = consistency_violation(c, pnl)
    assert violation is not None
    assert "d1" in violation


def test_consistency_violation_ignores_losing_days():
    c = config(max_consistency_pct=60.0)
    # A losing day must not lower the profit denominator and make a
    # winning day's share look bigger than it is among winning days alone.
    pnl = {"d1": 500.0, "d2": -1_000.0, "d3": 500.0}   # 50/50 split of the profit
    assert consistency_violation(c, pnl) is None


def test_consistency_violation_none_with_no_profit_yet():
    c = config()
    assert consistency_violation(c, {"d1": -100.0}) is None


# --------------------------------------------------------------- LucidEvalGuard.evaluate

def test_evaluate_approves_clean_account():
    guard = LucidEvalGuard(config())
    pnl_by_day = {f"d{i}": 100.0 for i in range(5)}   # evenly split, well under the 30% cap
    verdict = guard.evaluate(current_equity=50_500.0, eod_closes=[50_000.0],
                             pnl_by_day=pnl_by_day, trading_days=5)
    assert verdict.approved
    assert verdict.failures == []


def test_evaluate_rejects_when_equity_breaches_trailing_floor():
    guard = LucidEvalGuard(config())
    # Highest close 51,000 -> floor 49,000. Current equity below that.
    verdict = guard.evaluate(current_equity=48_500.0, eod_closes=[51_000.0],
                             pnl_by_day={}, trading_days=1)
    assert not verdict.approved
    failure = verdict.failures[0]
    assert failure.name == "trailing_drawdown"


def test_evaluate_rejects_on_consistency_violation():
    guard = LucidEvalGuard(config(max_consistency_pct=30.0))
    verdict = guard.evaluate(current_equity=51_800.0, eod_closes=[50_000.0],
                             pnl_by_day={"d1": 1_800.0, "d2": 0.0}, trading_days=2)
    assert not verdict.approved
    assert any(f.name == "consistency" for f in verdict.failures)


def test_evaluate_rejects_below_min_trading_days_when_enabled():
    guard = LucidEvalGuard(config(min_trading_days=5))
    verdict = guard.evaluate(current_equity=50_000.0, eod_closes=[], pnl_by_day={},
                             trading_days=2)
    assert not verdict.approved
    assert any(f.name == "min_trading_days" for f in verdict.failures)


def test_evaluate_skips_min_trading_days_check_when_disabled():
    guard = LucidEvalGuard(config(min_trading_days=0))
    verdict = guard.evaluate(current_equity=50_000.0, eod_closes=[], pnl_by_day={},
                             trading_days=0)
    assert all(c.name != "min_trading_days" for c in verdict.checks)


# --------------------------------------------------------------- profit_target_reached

def test_profit_target_reached_true_once_target_hit():
    guard = LucidEvalGuard(config(profit_target=3_000.0))
    assert not guard.profit_target_reached(52_000.0)
    assert guard.profit_target_reached(53_000.0)


# --------------------------------------------------------------- from_env

def test_config_from_env_reads_lucid_prefixed_vars(monkeypatch):
    monkeypatch.setenv("LUCID_STARTING_BALANCE", "100000")
    monkeypatch.setenv("LUCID_TRAILING_DRAWDOWN_AMOUNT", "4000")
    monkeypatch.setenv("LUCID_PROFIT_TARGET", "6000")
    monkeypatch.setenv("LUCID_MAX_CONSISTENCY_PCT", "25")
    monkeypatch.setenv("LUCID_MIN_TRADING_DAYS", "10")
    c = LucidEvalConfig.from_env()
    assert c.starting_balance == 100_000.0
    assert c.trailing_drawdown_amount == 4_000.0
    assert c.profit_target == 6_000.0
    assert c.max_consistency_pct == 25.0
    assert c.min_trading_days == 10


def test_config_from_env_falls_back_to_defaults_when_unset(monkeypatch):
    for var in ("LUCID_STARTING_BALANCE", "LUCID_TRAILING_DRAWDOWN_AMOUNT",
               "LUCID_PROFIT_TARGET", "LUCID_MAX_CONSISTENCY_PCT", "LUCID_MIN_TRADING_DAYS"):
        monkeypatch.delenv(var, raising=False)
    c = LucidEvalConfig.from_env()
    assert c == LucidEvalConfig()
