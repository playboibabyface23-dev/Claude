import pytest

from futures_agent.ai.engine import AIAction, AIDecision
from futures_agent.config.settings import RiskLimits
from futures_agent.config.symbols import get_symbol
from futures_agent.risk.manager import AccountState, RiskManager

MNQ = get_symbol("MNQ")


def limits(**kw) -> RiskLimits:
    base = dict(account_equity=50_000.0, risk_pct_per_trade=0.5, max_daily_loss_pct=3.0,
               max_trades_per_day=6, max_drawdown_pct=8.0, max_open_positions=1,
               min_ai_confidence=65.0)
    base.update(kw)
    return RiskLimits(**base)


def manager(tmp_path, **limit_kw) -> RiskManager:
    return RiskManager(limits(**limit_kw), kill_switch_file=str(tmp_path / "KILL_SWITCH"))


def clean_account() -> AccountState:
    return AccountState(equity=50_000.0, high_water_mark=50_000.0)


def buy_decision(**kw) -> AIDecision:
    base = dict(symbol="MNQ", action=AIAction.BUY, confidence=80.0,
               entry_reason="test", stop_loss=20.0, take_profit=60.0)
    base.update(kw)
    return AIDecision(**base)


# --------------------------------------------------------------- position sizing

def test_position_size_matches_hand_calculation(tmp_path):
    # 50000 * 0.5% = $250 risk budget. 20pt stop on MNQ = 20*$2 = $40/contract.
    # 250 // 40 = 6 contracts.
    m = manager(tmp_path)
    assert m.position_size(MNQ, stop_loss_points=20.0) == 6


def test_position_size_rounds_down_never_up(tmp_path):
    # $250 budget, $60/contract (30pt stop) -> 4 contracts, not 4.16 rounded up.
    m = manager(tmp_path)
    assert m.position_size(MNQ, stop_loss_points=30.0) == 4


def test_position_size_zero_for_non_positive_stop(tmp_path):
    m = manager(tmp_path)
    assert m.position_size(MNQ, stop_loss_points=0) == 0
    assert m.position_size(MNQ, stop_loss_points=-5) == 0


def test_position_size_zero_when_stop_too_wide_for_budget(tmp_path):
    m = manager(tmp_path, risk_pct_per_trade=0.01)   # $5 budget
    assert m.position_size(MNQ, stop_loss_points=20.0) == 0


# --------------------------------------------------------------- kill switch

def test_kill_switch_inactive_by_default(tmp_path):
    m = manager(tmp_path)
    assert not m.kill_switch_active()


def test_trip_and_clear_kill_switch(tmp_path):
    m = manager(tmp_path)
    m.trip_kill_switch("test trip")
    assert m.kill_switch_active()
    assert "test trip" in m.kill_switch_path.read_text()
    m.clear_kill_switch()
    assert not m.kill_switch_active()


def test_check_and_trip_on_daily_loss_breach(tmp_path):
    m = manager(tmp_path, max_daily_loss_pct=3.0)
    account = AccountState(equity=50_000.0, high_water_mark=50_000.0, daily_pnl=-1600.0)
    reason = m.check_and_trip_if_breached(account)
    assert reason is not None
    assert "daily loss" in reason
    assert m.kill_switch_active()


def test_check_and_trip_on_drawdown_breach(tmp_path):
    m = manager(tmp_path, max_drawdown_pct=8.0)
    account = AccountState(equity=45_000.0, high_water_mark=50_000.0)   # 10% dd
    reason = m.check_and_trip_if_breached(account)
    assert reason is not None
    assert "drawdown" in reason
    assert m.kill_switch_active()


def test_check_and_trip_does_nothing_when_healthy(tmp_path):
    m = manager(tmp_path)
    reason = m.check_and_trip_if_breached(clean_account())
    assert reason is None
    assert not m.kill_switch_active()


# --------------------------------------------------------------- evaluate()

def test_clean_decision_is_approved_with_correct_size(tmp_path):
    m = manager(tmp_path)
    verdict = m.evaluate(buy_decision(), clean_account(), MNQ)
    assert verdict.approved, verdict.failures
    assert verdict.contracts == 6


def test_hold_decision_is_never_approved(tmp_path):
    m = manager(tmp_path)
    hold = buy_decision(action=AIAction.HOLD, stop_loss=0, take_profit=0)
    verdict = m.evaluate(hold, clean_account(), MNQ)
    assert not verdict.approved
    assert verdict.contracts == 0
    assert any(c.name == "actionable" for c in verdict.failures)


def test_kill_switch_blocks_everything_and_short_circuits(tmp_path):
    m = manager(tmp_path)
    m.trip_kill_switch("manual stop")
    verdict = m.evaluate(buy_decision(), clean_account(), MNQ)
    assert not verdict.approved
    assert verdict.contracts == 0
    assert [c.name for c in verdict.checks] == ["kill_switch"]


def test_account_level_kill_switch_flag_also_blocks(tmp_path):
    m = manager(tmp_path)
    account = clean_account()
    account.kill_switch_tripped = True
    verdict = m.evaluate(buy_decision(), account, MNQ)
    assert not verdict.approved


def test_confidence_below_floor_is_rejected(tmp_path):
    m = manager(tmp_path, min_ai_confidence=90.0)
    verdict = m.evaluate(buy_decision(confidence=80.0), clean_account(), MNQ)
    assert not verdict.approved
    assert any(c.name == "confidence" for c in verdict.failures)


def test_daily_loss_limit_rejects_new_trades(tmp_path):
    m = manager(tmp_path, max_daily_loss_pct=3.0)
    account = AccountState(equity=50_000.0, high_water_mark=50_000.0, daily_pnl=-1600.0)
    verdict = m.evaluate(buy_decision(), account, MNQ)
    assert not verdict.approved
    assert any(c.name == "daily_loss_limit" for c in verdict.failures)


def test_max_trades_per_day_rejects_once_reached(tmp_path):
    m = manager(tmp_path, max_trades_per_day=3)
    account = clean_account()
    account.trades_today = 3
    verdict = m.evaluate(buy_decision(), account, MNQ)
    assert not verdict.approved
    assert any(c.name == "max_trades_per_day" for c in verdict.failures)


def test_max_drawdown_rejects_new_trades(tmp_path):
    m = manager(tmp_path, max_drawdown_pct=8.0)
    account = AccountState(equity=45_000.0, high_water_mark=50_000.0)
    verdict = m.evaluate(buy_decision(), account, MNQ)
    assert not verdict.approved
    assert any(c.name == "max_drawdown" for c in verdict.failures)


def test_max_open_positions_rejects_new_trades(tmp_path):
    m = manager(tmp_path, max_open_positions=1)
    account = clean_account()
    account.open_positions = 1
    verdict = m.evaluate(buy_decision(), account, MNQ)
    assert not verdict.approved
    assert any(c.name == "max_open_positions" for c in verdict.failures)


def test_stop_too_wide_for_budget_rejects_via_position_size(tmp_path):
    m = manager(tmp_path, risk_pct_per_trade=0.01)
    verdict = m.evaluate(buy_decision(stop_loss=20.0), clean_account(), MNQ)
    assert not verdict.approved
    assert any(c.name == "position_size" for c in verdict.failures)
    assert verdict.contracts == 0


def test_verdict_to_dict_is_json_serializable(tmp_path):
    import json

    m = manager(tmp_path)
    verdict = m.evaluate(buy_decision(), clean_account(), MNQ)
    json.dumps(verdict.to_dict())


# --------------------------------------------------------------- evaluate_raw()

def test_evaluate_raw_rejects_malformed_payload(tmp_path):
    m = manager(tmp_path)
    payload = {"symbol": "MNQ", "action": "BUY", "confidence": 80}   # missing fields
    verdict = m.evaluate_raw("MNQ", payload, clean_account(), MNQ)
    assert not verdict.approved
    assert verdict.contracts == 0
    assert verdict.checks[0].name == "malformed_ai_output"


def test_evaluate_raw_approves_valid_payload(tmp_path):
    m = manager(tmp_path)
    payload = {"symbol": "MNQ", "action": "BUY", "confidence": 80,
              "entry_reason": "test", "stop_loss": 20, "take_profit": 60}
    verdict = m.evaluate_raw("MNQ", payload, clean_account(), MNQ)
    assert verdict.approved
    assert verdict.contracts == 6
