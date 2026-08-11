from futures_agent.config import (
    LucidEvalSettings,
    RiskLimits,
    Settings,
    TradovateCredentials,
    get_symbol,
)
from futures_agent.config.symbols import SYMBOL_CATALOG


def test_defaults_are_sane():
    s = Settings()
    assert s.execution_mode == "tradovate"
    assert s.risk.max_open_positions == 1
    assert "MNQ" in s.traded_symbols


def test_validate_flags_missing_anthropic_key(tmp_env):
    problems = Settings().validate()
    assert any("ANTHROPIC_API_KEY" in p for p in problems)


def test_validate_flags_missing_tradovate_credentials(tmp_env):
    s = Settings(anthropic_api_key="k", execution_mode="tradovate")
    problems = s.validate()
    assert any("Tradovate credentials" in p for p in problems)


def test_validate_passes_with_traderspost_configured(tmp_env):
    s = Settings(anthropic_api_key="k", execution_mode="traderspost",
                traderspost_webhook_url="https://hooks.example/x")
    assert s.validate() == []


def test_validate_rejects_unknown_execution_mode(tmp_env):
    s = Settings(anthropic_api_key="k", execution_mode="carrier_pigeon")
    assert any("EXECUTION_MODE" in p for p in s.validate())


def test_tradovate_demo_vs_live_base_url():
    assert "demo" in TradovateCredentials(environment="demo").base_url
    assert "live" in TradovateCredentials(environment="live").base_url
    assert TradovateCredentials(environment="demo").base_url != \
           TradovateCredentials(environment="live").base_url


def test_tradovate_configured_flag():
    assert not TradovateCredentials().configured
    full = TradovateCredentials(username="u", password="p", cid="1", sec="s")
    assert full.configured


def test_every_traded_default_symbol_has_a_contract_spec():
    for sym in Settings().traded_symbols:
        assert sym in SYMBOL_CATALOG


def test_get_symbol_is_case_insensitive():
    assert get_symbol("mnq") is get_symbol("MNQ")


def test_get_symbol_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        get_symbol("DOGE")


def test_dollar_risk_matches_hand_calculation():
    mnq = get_symbol("MNQ")
    # 10-point stop on MNQ = 40 ticks * $0.50/tick = $20 per contract.
    assert mnq.dollar_risk(entry=20000.0, stop=19990.0, contracts=1) == 20.0


def test_risk_limits_from_env_uses_defaults_when_unset(tmp_env):
    limits = RiskLimits.from_env()
    assert limits.account_equity == RiskLimits().account_equity


def test_lucid_eval_disabled_by_default():
    assert Settings().lucid.enabled is False


def test_lucid_eval_settings_from_env(tmp_env, monkeypatch):
    monkeypatch.setenv("LUCID_EVAL_ENABLED", "true")
    monkeypatch.setenv("LUCID_STARTING_BALANCE", "100000")
    s = Settings.from_env()
    assert s.lucid.enabled is True
    assert s.lucid.starting_balance == 100_000.0


def test_lucid_eval_settings_from_env_defaults_when_unset(tmp_env):
    s = LucidEvalSettings.from_env()
    assert s == LucidEvalSettings()
