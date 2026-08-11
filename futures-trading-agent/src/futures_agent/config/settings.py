"""Environment-driven configuration.

RiskLimits are hard caps enforced deterministically in risk/manager.py. The AI
decision engine can only ever be tightened by them, never loosen them — this
mirrors the trading-system project's design and is repeated here deliberately:
a risk cap that the model can talk its way around is not a cap.
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
    def from_env(cls) -> "RiskLimits":
        return cls(
            account_equity=_env_float("ACCOUNT_EQUITY", cls.account_equity),
            risk_pct_per_trade=_env_float("RISK_PCT_PER_TRADE", cls.risk_pct_per_trade),
            max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", cls.max_daily_loss_pct),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", cls.max_trades_per_day),
            max_drawdown_pct=_env_float("MAX_DRAWDOWN_PCT", cls.max_drawdown_pct),
            max_open_positions=_env_int("MAX_OPEN_POSITIONS", cls.max_open_positions),
            min_ai_confidence=_env_float("MIN_AI_CONFIDENCE", cls.min_ai_confidence),
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
    def from_env(cls) -> "TradovateCredentials":
        return cls(
            environment=_env_str("TRADOVATE_ENV", cls.environment).strip().lower() or "demo",
            username=_env_str("TRADOVATE_USERNAME"),
            password=_env_str("TRADOVATE_PASSWORD"),
            app_id=_env_str("TRADOVATE_APP_ID"),
            app_version=_env_str("TRADOVATE_APP_VERSION", cls.app_version),
            cid=_env_str("TRADOVATE_CID"),
            sec=_env_str("TRADOVATE_SEC"),
            device_id=_env_str("TRADOVATE_DEVICE_ID"),
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
    def from_env(cls) -> "LucidEvalSettings":
        return cls(
            enabled=_env_bool("LUCID_EVAL_ENABLED", cls.enabled),
            starting_balance=_env_float("LUCID_STARTING_BALANCE", cls.starting_balance),
            trailing_drawdown_amount=_env_float(
                "LUCID_TRAILING_DRAWDOWN_AMOUNT", cls.trailing_drawdown_amount
            ),
            profit_target=_env_float("LUCID_PROFIT_TARGET", cls.profit_target),
            max_consistency_pct=_env_float(
                "LUCID_MAX_CONSISTENCY_PCT", cls.max_consistency_pct
            ),
            min_trading_days=_env_int("LUCID_MIN_TRADING_DAYS", cls.min_trading_days),
        )


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = ""
    claude_model: str = "claude-fable-5"
    claude_fallback_model: str = "claude-opus-4-8"

    tradovate: TradovateCredentials = field(default_factory=TradovateCredentials)
    traderspost_webhook_url: str = ""
    execution_mode: str = "tradovate"     # "tradovate" or "traderspost"

    traded_symbols: tuple[str, ...] = ("MNQ", "NQ", "MES", "ES", "GC")
    timeframe_minutes: int = 5
    poll_interval_seconds: int = 30
    # Optional {symbol}-templated CSV path polled each cycle as a stand-in
    # market data feed (paper/demo use). Production needs a real live feed —
    # see market/tradovate.py's verification note on why one isn't bundled.
    historical_bars_csv_template: str = ""

    risk: RiskLimits = field(default_factory=RiskLimits)
    lucid: LucidEvalSettings = field(default_factory=LucidEvalSettings)
    kill_switch_file: str = "logs/KILL_SWITCH"

    database_path: str = "database/futures_agent.db"

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8788

    log_dir: str = "logs"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY"),
            claude_model=_env_str("CLAUDE_MODEL", cls.claude_model),
            claude_fallback_model=_env_str("CLAUDE_FALLBACK_MODEL", cls.claude_fallback_model),
            tradovate=TradovateCredentials.from_env(),
            traderspost_webhook_url=_env_str("TRADERSPOST_WEBHOOK_URL"),
            execution_mode=_env_str("EXECUTION_MODE", cls.execution_mode).strip().lower(),
            traded_symbols=_env_list("TRADED_SYMBOLS", cls.traded_symbols),
            timeframe_minutes=_env_int("TIMEFRAME_MINUTES", cls.timeframe_minutes),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds),
            historical_bars_csv_template=_env_str(
                "HISTORICAL_BARS_CSV_TEMPLATE", cls.historical_bars_csv_template
            ),
            risk=RiskLimits.from_env(),
            lucid=LucidEvalSettings.from_env(),
            kill_switch_file=_env_str("KILL_SWITCH_FILE", cls.kill_switch_file),
            database_path=_env_str("DATABASE_PATH", cls.database_path),
            dashboard_host=_env_str("DASHBOARD_HOST", cls.dashboard_host),
            dashboard_port=_env_int("DASHBOARD_PORT", cls.dashboard_port),
            log_dir=_env_str("LOG_DIR", cls.log_dir),
            log_level=_env_str("LOG_LEVEL", cls.log_level),
        )

    def validate(self) -> list[str]:
        """Configuration problems worth surfacing before the agent runs.
        Returns an empty list when everything required is present."""
        problems: list[str] = []
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set — the AI decision engine cannot run")
        if self.execution_mode not in ("tradovate", "traderspost"):
            problems.append(
                f"EXECUTION_MODE={self.execution_mode!r} is invalid — "
                "must be 'tradovate' or 'traderspost'"
            )
        if self.execution_mode == "tradovate" and not self.tradovate.configured:
            problems.append(
                "EXECUTION_MODE=tradovate but Tradovate credentials are incomplete "
                "(need TRADOVATE_USERNAME/PASSWORD/CID/SEC)"
            )
        if self.execution_mode == "traderspost" and not self.traderspost_webhook_url:
            problems.append(
                "EXECUTION_MODE=traderspost but TRADERSPOST_WEBHOOK_URL is not set"
            )
        if not self.traded_symbols:
            problems.append("TRADED_SYMBOLS is empty — nothing to trade")
        return problems

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir)
