"""Environment-driven configuration.

RiskLimits are the deterministic hard caps of the system: the AI risk agent can
tighten them per-trade but can never exceed them — the safety layer and
execution validator enforce them in code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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


@dataclass(frozen=True)
class RiskLimits:
    """Hard risk caps. Enforced deterministically — never by the model."""

    account_equity: float = 25_000.0
    max_risk_per_trade_pct: float = 1.0     # % of equity risked on one trade
    max_daily_loss_pct: float = 3.0         # stop trading for the day past this
    max_weekly_loss_pct: float = 6.0
    max_drawdown_pct: float = 10.0          # from equity high-water mark
    max_open_positions: int = 2
    min_risk_reward: float = 1.5
    min_probability: float = 0.55           # reject setups the model scores below this
    max_spread_pct: float = 0.05            # spread as % of price
    max_correlated_positions: int = 1       # per correlation group
    atr_volatility_ceiling: float = 4.0     # reject if ATR% of price exceeds normal x this
    require_quote: bool = True              # block when no usable bid/ask is available
    max_quote_age_seconds: float = 60.0     # older than this is treated as no quote
    min_slippage_headroom: float = 4.0      # stop distance must exceed spread by this factor

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            account_equity=_env_float("ACCOUNT_EQUITY", cls.account_equity),
            max_risk_per_trade_pct=_env_float("MAX_RISK_PER_TRADE_PCT", cls.max_risk_per_trade_pct),
            max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", cls.max_daily_loss_pct),
            max_weekly_loss_pct=_env_float("MAX_WEEKLY_LOSS_PCT", cls.max_weekly_loss_pct),
            max_drawdown_pct=_env_float("MAX_DRAWDOWN_PCT", cls.max_drawdown_pct),
            max_open_positions=_env_int("MAX_OPEN_POSITIONS", cls.max_open_positions),
            min_risk_reward=_env_float("MIN_RISK_REWARD", cls.min_risk_reward),
            min_probability=_env_float("MIN_PROBABILITY", cls.min_probability),
            max_spread_pct=_env_float("MAX_SPREAD_PCT", cls.max_spread_pct),
            require_quote=os.environ.get("REQUIRE_QUOTE", "true").lower()
            not in ("false", "0", "no"),
            max_quote_age_seconds=_env_float(
                "MAX_QUOTE_AGE_SECONDS", cls.max_quote_age_seconds
            ),
        )


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = ""
    polygon_api_key: str = ""
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    finnhub_api_key: str = ""
    alpaca_paper: bool = True   # trading host: paper-api vs api.alpaca.markets
    traderspost_webhook_url: str = ""
    journal_db_path: str = "trades.db"
    risk: RiskLimits = field(default_factory=RiskLimits)
    # Symbols considered correlated for the safety layer's correlation check.
    correlation_groups: tuple[tuple[str, ...], ...] = (
        ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"),
        ("USDJPY", "USDCHF", "USDCAD"),
        ("SPY", "QQQ", "ES", "NQ"),
        ("GC", "XAUUSD", "GLD"),
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            polygon_api_key=os.environ.get("POLYGON_API_KEY", ""),
            alpaca_key_id=os.environ.get("ALPACA_API_KEY_ID", ""),
            alpaca_secret_key=os.environ.get("ALPACA_API_SECRET_KEY", ""),
            finnhub_api_key=os.environ.get("FINNHUB_API_KEY", ""),
            alpaca_paper=os.environ.get("ALPACA_PAPER", "true").lower()
            not in ("false", "0", "no"),
            traderspost_webhook_url=os.environ.get("TRADERSPOST_WEBHOOK_URL", ""),
            journal_db_path=os.environ.get("JOURNAL_DB_PATH", "trades.db"),
            risk=RiskLimits.from_env(),
        )
