"""Environment-driven configuration.

RiskLimits are hard caps enforced deterministically in risk/manager.py. The AI
decision engine can only ever be tightened by them, never loosen them — this
mirrors the trading-system project's design and is repeated here deliberately:
a risk cap that the model can talk its way around is not a cap.

Multi-account: the agent can trade N broker accounts at once, sharing one
market-data feed and one Claude decision per symbol per cycle, but sizing,
risk-gating, and executing independently into each account. Accounts are
discovered from indexed `ACCOUNT_1_*`, `ACCOUNT_2_*`, ... env vars (see
`Settings.accounts` / `AccountConfig.from_env`). If none are configured, a
single "default" account is built from the legacy top-level fields
(`TRADOVATE_*`, `EXECUTION_MODE`, `ACCOUNT_EQUITY`, ...) so existing `.env`
files and deployments keep working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MAX_ACCOUNTS = 20


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class RiskLimits:
    """Hard risk caps. Enforced deterministically — never by the model."""

    account_equity: float = 50_000.0
    risk_pct_per_trade: float = 0.5      # % of equity risked on one trade
    max_daily_loss_pct: float = 3.0      # kill switch trips past this
    max_trades_per_day: int = 6
    max_drawdown_pct: float = 8.0        # from equity high-water mark
    max_open_positions: int = 1
    min_ai_confidence: float = 65.0      # 0-100, reject decisions below this

    @classmethod
    def from_env(cls, prefix: str = "") -> "RiskLimits":
        return cls(
            account_equity=_env_float(f"{prefix}ACCOUNT_EQUITY", cls.account_equity),
            risk_pct_per_trade=_env_float(f"{prefix}RISK_PCT_PER_TRADE", cls.risk_pct_per_trade),
            max_daily_loss_pct=_env_float(f"{prefix}MAX_DAILY_LOSS_PCT", cls.max_daily_loss_pct),
            max_trades_per_day=_env_int(f"{prefix}MAX_TRADES_PER_DAY", cls.max_trades_per_day),
            max_drawdown_pct=_env_float(f"{prefix}MAX_DRAWDOWN_PCT", cls.max_drawdown_pct),
            max_open_positions=_env_int(f"{prefix}MAX_OPEN_POSITIONS", cls.max_open_positions),
            min_ai_confidence=_env_float(f"{prefix}MIN_AI_CONFIDENCE", cls.min_ai_confidence),
        )


@dataclass(frozen=True)
class TradovateCredentials:
    """Tradovate has fully separate demo and live environments — different
    base URLs, different accounts, different credentials. `environment`
    decides which host every request goes to; there is no runtime toggle
    inside a single session, by design, so a config mistake can't silently
    route a demo strategy at a live account."""

    environment: str = "demo"     # "demo" or "live"
    username: str = ""
    password: str = ""
    app_id: str = ""
    app_version: str = "1.0"
    cid: str = ""
    sec: str = ""
    device_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password and self.cid and self.sec)

    @property
    def base_url(self) -> str:
        # Trailing slash matters: httpx.URL.join() treats a base_url path
        # without one as replaceable, which silently drops "/v1" from every
        # request made with a relative (non-leading-slash) path below.
        if self.environment == "live":
            return "https://live.tradovateapi.com/v1/"
        return "https://demo.tradovateapi.com/v1/"

    @classmethod
    def from_env(cls, prefix: str = "") -> "TradovateCredentials":
        return cls(
            environment=_env_str(f"{prefix}TRADOVATE_ENV", cls.environment).strip().lower() or "demo",
            username=_env_str(f"{prefix}TRADOVATE_USERNAME"),
            password=_env_str(f"{prefix}TRADOVATE_PASSWORD"),
            app_id=_env_str(f"{prefix}TRADOVATE_APP_ID"),
            app_version=_env_str(f"{prefix}TRADOVATE_APP_VERSION", cls.app_version),
            cid=_env_str(f"{prefix}TRADOVATE_CID"),
            sec=_env_str(f"{prefix}TRADOVATE_SEC"),
            device_id=_env_str(f"{prefix}TRADOVATE_DEVICE_ID"),
        )


@dataclass(frozen=True)
class LucidEvalSettings:
    """Off by default -- enable only for a Lucid (or Lucid-shaped) prop-firm
    evaluation account. See risk/lucid_eval.py's module docstring for what
    is and isn't confirmed about these numbers."""

    enabled: bool = False
    starting_balance: float = 50_000.0
    trailing_drawdown_amount: float = 2_000.0
    profit_target: float = 3_000.0
    max_consistency_pct: float = 30.0
    min_trading_days: int = 0

    @classmethod
    def from_env(cls, prefix: str = "") -> "LucidEvalSettings":
        return cls(
            enabled=_env_bool(f"{prefix}LUCID_EVAL_ENABLED", cls.enabled),
            starting_balance=_env_float(
                f"{prefix}LUCID_STARTING_BALANCE", cls.starting_balance
            ),
            trailing_drawdown_amount=_env_float(
                f"{prefix}LUCID_TRAILING_DRAWDOWN_AMOUNT", cls.trailing_drawdown_amount
            ),
            profit_target=_env_float(f"{prefix}LUCID_PROFIT_TARGET", cls.profit_target),
            max_consistency_pct=_env_float(
                f"{prefix}LUCID_MAX_CONSISTENCY_PCT", cls.max_consistency_pct
            ),
            min_trading_days=_env_int(f"{prefix}LUCID_MIN_TRADING_DAYS", cls.min_trading_days),
        )


@dataclass(frozen=True)
class AccountConfig:
    """One tradable destination: its own broker credentials/webhook, its own
    risk limits, its own optional Lucid eval guard. Multiple of these can be
    active at once — see `Settings.accounts`. Every account is independently
    risk-gated and sized; nothing here lets one account's limits leak into
    another's, and Claude never sees or picks an account, only a symbol."""

    name: str = "default"
    execution_mode: str = "tradovate"     # "tradovate" or "traderspost"
    tradovate: TradovateCredentials = field(default_factory=TradovateCredentials)
    traderspost_webhook_url: str = ""
    risk: RiskLimits = field(default_factory=RiskLimits)
    lucid: LucidEvalSettings = field(default_factory=LucidEvalSettings)

    @property
    def configured(self) -> bool:
        if self.execution_mode == "tradovate":
            return self.tradovate.configured
        if self.execution_mode == "traderspost":
            return bool(self.traderspost_webhook_url)
        return False

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.execution_mode not in ("tradovate", "traderspost"):
            problems.append(
                f"account {self.name!r}: EXECUTION_MODE={self.execution_mode!r} is invalid — "
                "must be 'tradovate' or 'traderspost'"
            )
        elif self.execution_mode == "tradovate" and not self.tradovate.configured:
            problems.append(
                f"account {self.name!r}: EXECUTION_MODE=tradovate but Tradovate credentials "
                "are incomplete (need TRADOVATE_USERNAME/PASSWORD/CID/SEC)"
            )
        elif self.execution_mode == "traderspost" and not self.traderspost_webhook_url:
            problems.append(
                f"account {self.name!r}: EXECUTION_MODE=traderspost but "
                "TRADERSPOST_WEBHOOK_URL is not set"
            )
        return problems

    @classmethod
    def from_env(cls, name: str, prefix: str) -> "AccountConfig":
        return cls(
            name=name,
            execution_mode=_env_str(f"{prefix}EXECUTION_MODE", cls.execution_mode).strip().lower(),
            tradovate=TradovateCredentials.from_env(prefix=prefix),
            traderspost_webhook_url=_env_str(f"{prefix}TRADERSPOST_WEBHOOK_URL"),
            risk=RiskLimits.from_env(prefix=prefix),
            lucid=LucidEvalSettings.from_env(prefix=prefix),
        )


def _discover_accounts() -> tuple[AccountConfig, ...]:
    """Scans ACCOUNT_1_* .. ACCOUNT_{MAX_ACCOUNTS}_* for configured account
    slots. A slot counts as configured if any of its identifying fields is
    set — an index can be skipped (e.g. only ACCOUNT_1_ and ACCOUNT_3_ set)
    without breaking discovery of the others."""
    accounts: list[AccountConfig] = []
    for n in range(1, MAX_ACCOUNTS + 1):
        prefix = f"ACCOUNT_{n}_"
        marker_keys = (
            f"{prefix}NAME", f"{prefix}EXECUTION_MODE",
            f"{prefix}TRADERSPOST_WEBHOOK_URL", f"{prefix}TRADOVATE_USERNAME",
        )
        if not any(os.environ.get(k) for k in marker_keys):
            continue
        name = _env_str(f"{prefix}NAME", f"account_{n}")
        accounts.append(AccountConfig.from_env(name=name, prefix=prefix))
    return tuple(accounts)


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = ""
    claude_model: str = "claude-fable-5"
    claude_fallback_model: str = "claude-opus-4-8"

    # Legacy single-account fields. Kept as the source of the "default"
    # account (see `accounts` below) when no ACCOUNT_N_* env vars are set,
    # so existing single-account .env files and deployments are unaffected.
    tradovate: TradovateCredentials = field(default_factory=TradovateCredentials)
    traderspost_webhook_url: str = ""
    execution_mode: str = "tradovate"     # "tradovate" or "traderspost"
    risk: RiskLimits = field(default_factory=RiskLimits)
    lucid: LucidEvalSettings = field(default_factory=LucidEvalSettings)

    # Every account this agent trades into. Always non-empty after
    # from_env() — one legacy "default" entry when no ACCOUNT_N_* vars are
    # configured. Market data and the Claude decision are shared across all
    # of them; sizing, risk gating, and execution are fully independent.
    accounts: tuple[AccountConfig, ...] = field(default_factory=tuple)

    traded_symbols: tuple[str, ...] = ("MNQ", "NQ", "MES", "ES", "GC")
    timeframe_minutes: int = 5
    poll_interval_seconds: int = 30
    # Optional {symbol}-templated CSV path polled each cycle as a stand-in
    # market data feed (paper/demo use). Production needs a real live feed —
    # see market/tradovate.py's verification note on why one isn't bundled.
    historical_bars_csv_template: str = ""

    kill_switch_file: str = "logs/KILL_SWITCH"
    # Generic webhook (Slack/Discord-compatible {"text": ...} payload) for
    # kill-switch trips, execution failures, and crashes. Empty = no alerts
    # sent, only logged locally. See notifications/alerts.py.
    alert_webhook_url: str = ""

    # --- Scanner (scanner/) -- advisory only, never places an order ---
    # Finnhub API key for market headlines and the high-impact economic
    # calendar. Empty = the scanner still runs on price action alone (see
    # news/finnhub_news.py's fail-quiet posture).
    finnhub_api_key: str = ""
    # Symbols the scanner scans. Empty = every symbol in
    # config/symbols.SYMBOL_CATALOG ("scan everything"), independent of
    # traded_symbols above.
    scan_symbols: tuple[str, ...] = ()
    # Minimum confidence (0-100) before a scan result becomes a
    # notification. Below this it's still logged, just not alerted.
    scan_min_confidence: float = 70.0
    # How often the scanner re-scans every symbol, in seconds. Deliberately
    # much slower than poll_interval_seconds above -- one scan cycle is one
    # news fetch plus one Claude call per symbol, and there's no order to
    # place in a hurry.
    scan_poll_interval_seconds: int = 300
    # Minimum minutes between two alerts for the same symbol, so a
    # persistent setup doesn't re-notify every single cycle.
    scan_alert_cooldown_minutes: int = 60
    # Max headlines pulled per scan cycle.
    scan_news_limit: int = 15
    # Webhook the scanner posts trade-worth-a-look alerts to (same
    # Slack/Discord-compatible {"text": ...} format as alert_webhook_url).
    # Empty = falls back to alert_webhook_url, so a single webhook covers
    # both operational alerts and scan notifications unless you want them
    # split into separate channels.
    scan_alert_webhook_url: str = ""

    database_path: str = "database/futures_agent.db"

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8788

    log_dir: str = "logs"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        tradovate = TradovateCredentials.from_env()
        traderspost_webhook_url = _env_str("TRADERSPOST_WEBHOOK_URL")
        execution_mode = _env_str("EXECUTION_MODE", cls.execution_mode).strip().lower()
        risk = RiskLimits.from_env()
        lucid = LucidEvalSettings.from_env()

        accounts = _discover_accounts()
        if not accounts:
            accounts = (AccountConfig(
                name="default", execution_mode=execution_mode, tradovate=tradovate,
                traderspost_webhook_url=traderspost_webhook_url, risk=risk, lucid=lucid,
            ),)

        return cls(
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY"),
            claude_model=_env_str("CLAUDE_MODEL", cls.claude_model),
            claude_fallback_model=_env_str("CLAUDE_FALLBACK_MODEL", cls.claude_fallback_model),
            tradovate=tradovate,
            traderspost_webhook_url=traderspost_webhook_url,
            execution_mode=execution_mode,
            risk=risk,
            lucid=lucid,
            accounts=accounts,
            traded_symbols=_env_list("TRADED_SYMBOLS", cls.traded_symbols),
            timeframe_minutes=_env_int("TIMEFRAME_MINUTES", cls.timeframe_minutes),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds),
            historical_bars_csv_template=_env_str(
                "HISTORICAL_BARS_CSV_TEMPLATE", cls.historical_bars_csv_template
            ),
            kill_switch_file=_env_str("KILL_SWITCH_FILE", cls.kill_switch_file),
            alert_webhook_url=_env_str("ALERT_WEBHOOK_URL", cls.alert_webhook_url),
            finnhub_api_key=_env_str("FINNHUB_API_KEY", cls.finnhub_api_key),
            scan_symbols=_env_list("SCAN_SYMBOLS", cls.scan_symbols),
            scan_min_confidence=_env_float("SCAN_MIN_CONFIDENCE", cls.scan_min_confidence),
            scan_poll_interval_seconds=_env_int(
                "SCAN_POLL_INTERVAL_SECONDS", cls.scan_poll_interval_seconds
            ),
            scan_alert_cooldown_minutes=_env_int(
                "SCAN_ALERT_COOLDOWN_MINUTES", cls.scan_alert_cooldown_minutes
            ),
            scan_news_limit=_env_int("SCAN_NEWS_LIMIT", cls.scan_news_limit),
            scan_alert_webhook_url=_env_str("SCAN_ALERT_WEBHOOK_URL", cls.scan_alert_webhook_url),
            database_path=_env_str("DATABASE_PATH", cls.database_path),
            dashboard_host=_env_str("DASHBOARD_HOST", cls.dashboard_host),
            dashboard_port=_env_int("DASHBOARD_PORT", cls.dashboard_port),
            log_dir=_env_str("LOG_DIR", cls.log_dir),
            log_level=_env_str("LOG_LEVEL", cls.log_level),
        )

    def validate(self) -> list[str]:
        """Configuration problems worth surfacing before the agent runs.
        Returns an empty list when everything required is present.

        Validates `self.accounts` when set; when a `Settings` is built by
        hand (not via `from_env()`) with `accounts` left empty, falls back
        to synthesizing a single "default" account from the legacy
        top-level fields so direct construction keeps validating the same
        way it always has."""
        problems: list[str] = []
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set — the AI decision engine cannot run")
        if not self.traded_symbols:
            problems.append("TRADED_SYMBOLS is empty — nothing to trade")

        accounts = self.accounts or (AccountConfig(
            name="default", execution_mode=self.execution_mode, tradovate=self.tradovate,
            traderspost_webhook_url=self.traderspost_webhook_url, risk=self.risk, lucid=self.lucid,
        ),)
        seen_names: set[str] = set()
        for account in accounts:
            problems.extend(account.validate())
            if account.name in seen_names:
                problems.append(f"account name {account.name!r} is configured more than once")
            seen_names.add(account.name)
        return problems

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir)
