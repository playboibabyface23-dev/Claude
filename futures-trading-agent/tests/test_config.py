from futures_agent.config import (
    AccountConfig,
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


def test_alert_webhook_url_defaults_empty():
    assert Settings().alert_webhook_url == ""


def test_alert_webhook_url_from_env(tmp_env, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/webhook")
    s = Settings.from_env()
    assert s.alert_webhook_url == "https://hooks.example/webhook"


# --------------------------------------------------------------- multi-account

def test_no_account_env_vars_yields_one_legacy_default_account(tmp_env, monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "traderspost")
    monkeypatch.setenv("TRADERSPOST_WEBHOOK_URL", "https://hooks.example/legacy")
    monkeypatch.setenv("ACCOUNT_EQUITY", "75000")
    s = Settings.from_env()
    assert len(s.accounts) == 1
    account = s.accounts[0]
    assert account.name == "default"
    assert account.execution_mode == "traderspost"
    assert account.traderspost_webhook_url == "https://hooks.example/legacy"
    assert account.risk.account_equity == 75_000.0


def test_indexed_account_env_vars_are_discovered(tmp_env, monkeypatch):
    monkeypatch.setenv("ACCOUNT_1_NAME", "prop_eval")
    monkeypatch.setenv("ACCOUNT_1_EXECUTION_MODE", "traderspost")
    monkeypatch.setenv("ACCOUNT_1_TRADERSPOST_WEBHOOK_URL", "https://hooks.example/a1")
    monkeypatch.setenv("ACCOUNT_1_ACCOUNT_EQUITY", "50000")
    monkeypatch.setenv("ACCOUNT_1_LUCID_EVAL_ENABLED", "true")

    monkeypatch.setenv("ACCOUNT_2_NAME", "live_direct")
    monkeypatch.setenv("ACCOUNT_2_EXECUTION_MODE", "tradovate")
    monkeypatch.setenv("ACCOUNT_2_TRADOVATE_USERNAME", "u2")
    monkeypatch.setenv("ACCOUNT_2_TRADOVATE_PASSWORD", "p2")
    monkeypatch.setenv("ACCOUNT_2_TRADOVATE_CID", "c2")
    monkeypatch.setenv("ACCOUNT_2_TRADOVATE_SEC", "s2")
    monkeypatch.setenv("ACCOUNT_2_ACCOUNT_EQUITY", "150000")

    s = Settings.from_env()
    assert [a.name for a in s.accounts] == ["prop_eval", "live_direct"]

    prop_eval, live_direct = s.accounts
    assert prop_eval.execution_mode == "traderspost"
    assert prop_eval.traderspost_webhook_url == "https://hooks.example/a1"
    assert prop_eval.risk.account_equity == 50_000.0
    assert prop_eval.lucid.enabled is True
    assert prop_eval.configured is True

    assert live_direct.execution_mode == "tradovate"
    assert live_direct.tradovate.username == "u2"
    assert live_direct.tradovate.configured is True
    assert live_direct.risk.account_equity == 150_000.0
    assert live_direct.lucid.enabled is False


def test_indexed_accounts_skip_unconfigured_slots(tmp_env, monkeypatch):
    monkeypatch.setenv("ACCOUNT_1_NAME", "first")
    monkeypatch.setenv("ACCOUNT_1_EXECUTION_MODE", "traderspost")
    monkeypatch.setenv("ACCOUNT_1_TRADERSPOST_WEBHOOK_URL", "https://hooks.example/1")
    # ACCOUNT_2_* deliberately left unset.
    monkeypatch.setenv("ACCOUNT_3_NAME", "third")
    monkeypatch.setenv("ACCOUNT_3_EXECUTION_MODE", "traderspost")
    monkeypatch.setenv("ACCOUNT_3_TRADERSPOST_WEBHOOK_URL", "https://hooks.example/3")

    s = Settings.from_env()
    assert [a.name for a in s.accounts] == ["first", "third"]


def test_account_config_validate_flags_incomplete_tradovate_credentials():
    account = AccountConfig(name="acct_1", execution_mode="tradovate")
    problems = account.validate()
    assert any("acct_1" in p and "Tradovate credentials" in p for p in problems)


def test_account_config_validate_passes_when_configured():
    account = AccountConfig(
        name="acct_1", execution_mode="traderspost",
        traderspost_webhook_url="https://hooks.example/x")
    assert account.validate() == []


def test_settings_validate_prefixes_problems_with_account_name(tmp_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ACCOUNT_1_NAME", "broken")
    monkeypatch.setenv("ACCOUNT_1_EXECUTION_MODE", "tradovate")
    s = Settings.from_env()
    problems = s.validate()
    assert any("broken" in p and "Tradovate credentials" in p for p in problems)


def test_settings_validate_flags_duplicate_account_names():
    s = Settings(anthropic_api_key="k", accounts=(
        AccountConfig(name="dup", execution_mode="traderspost",
                     traderspost_webhook_url="https://hooks.example/x"),
        AccountConfig(name="dup", execution_mode="traderspost",
                     traderspost_webhook_url="https://hooks.example/y"),
    ))
    assert any("more than once" in p for p in s.validate())


def test_risk_limits_from_env_prefix_is_independent_of_unprefixed(tmp_env, monkeypatch):
    monkeypatch.setenv("ACCOUNT_EQUITY", "10000")
    monkeypatch.setenv("ACCOUNT_1_ACCOUNT_EQUITY", "99000")
    unprefixed = RiskLimits.from_env()
    prefixed = RiskLimits.from_env(prefix="ACCOUNT_1_")
    assert unprefixed.account_equity == 10_000.0
    assert prefixed.account_equity == 99_000.0
