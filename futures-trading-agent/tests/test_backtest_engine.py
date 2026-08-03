"""Backtest engine correctness. The bias controls matter far more than the
metrics: a backtest that peeks ahead or fills at the wrong price reports an
edge that isn't there."""

import math
from datetime import datetime, timedelta, timezone

import pytest

import futures_agent.backtesting.engine as engine_mod
from futures_agent.ai.engine import AIAction, AIDecision
from futures_agent.backtesting.engine import Backtester, BacktestConfig
from futures_agent.backtesting.report import format_report
from futures_agent.market.models import Candle

UTC = timezone.utc


def flat_bar(i: int, o=100.0, h=101.0, l=99.0, c=100.0) -> Candle:
    return Candle(timestamp=datetime(2026, 8, 3, 13, 0, tzinfo=UTC) + timedelta(minutes=5 * i),
                 open=o, high=h, low=l, close=c, volume=1000)


def cfg(**kw) -> BacktestConfig:
    base = dict(symbol="MNQ", starting_equity=50_000.0, warmup_bars=1)
    base.update(kw)
    return BacktestConfig(**base)


BUY = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                 entry_reason="fixture", stop_loss=5.0, take_profit=50.0)


def signal_at(*target_indices, decision=BUY):
    """Fake strategy: fires `decision` only when the history passed in ends
    at one of `target_indices` (i.e. len(candles)-1 is a target), HOLD-like
    None otherwise so nothing else in the run can act."""
    targets = set(target_indices)

    def _fake(symbol, candles, indicators):
        if (len(candles) - 1) in targets:
            return decision
        return None
    return _fake


def patch_strategy(monkeypatch, fake) -> None:
    monkeypatch.setattr(engine_mod, "evaluate", fake)


# --------------------------------------------------------------- bias controls

def test_entry_fills_at_next_bars_open_not_the_signal_bars_close(monkeypatch):
    candles = [flat_bar(0), flat_bar(1, o=100.0, h=101.0, l=99.5, c=100.0),
              flat_bar(2, o=100.0, h=101.0, l=99.5, c=100.0), flat_bar(3)]
    patch_strategy(monkeypatch, signal_at(1))
    result = Backtester(cfg()).run(candles)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.entry_index == 2
    assert t.entry == candles[2].open


def test_no_lookahead_truncating_the_tail_does_not_change_earlier_trades(monkeypatch):
    candles = [flat_bar(0), flat_bar(1), flat_bar(2, l=90.0), flat_bar(3), flat_bar(4)]
    patch_strategy(monkeypatch, signal_at(1))
    full = Backtester(cfg()).run(candles)
    truncated = Backtester(cfg()).run(candles[:3])
    assert full.trades[0].entry == truncated.trades[0].entry
    assert full.trades[0].entry_index == truncated.trades[0].entry_index


def test_clean_target_hit():
    candles = [flat_bar(0), flat_bar(1),
              flat_bar(2, o=100.0, h=105.0, l=99.0, c=104.0)]   # target=150 too far — adjust below
    # target_points=50 -> target=150, unreachable in this fixture; use a
    # smaller-target decision to make the intent explicit and readable.
    small_target = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                              entry_reason="t", stop_loss=5.0, take_profit=4.0)
    import futures_agent.backtesting.engine as em
    em.evaluate = signal_at(1, decision=small_target)
    result = Backtester(cfg()).run(candles)
    t = result.trades[0]
    assert t.exit_reason == "target_hit"
    assert t.exit_price == pytest.approx(104.0)   # entry(100) + 4
    assert not t.ambiguous_exit


def test_clean_stop_hit(monkeypatch):
    small_stop = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                            entry_reason="t", stop_loss=5.0, take_profit=50.0)
    candles = [flat_bar(0), flat_bar(1),
              flat_bar(2, o=100.0, h=101.0, l=90.0, c=95.0)]
    patch_strategy(monkeypatch, signal_at(1, decision=small_stop))
    result = Backtester(cfg()).run(candles)
    t = result.trades[0]
    assert t.exit_reason == "stop_hit"
    assert t.exit_price == pytest.approx(95.0)   # entry(100) - 5
    assert not t.ambiguous_exit
    assert t.pnl < 0


def test_ambiguous_bar_resolves_as_a_loss(monkeypatch):
    wide_stop_and_target = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                                      entry_reason="t", stop_loss=5.0, take_profit=5.0)
    candles = [flat_bar(0), flat_bar(1),
              flat_bar(2, o=100.0, h=110.0, l=90.0, c=100.0)]   # spans both 95 and 105
    patch_strategy(monkeypatch, signal_at(1, decision=wide_stop_and_target))
    result = Backtester(cfg()).run(candles)
    t = result.trades[0]
    assert t.ambiguous_exit
    assert t.exit_reason == "stop_hit"
    assert t.exit_price == pytest.approx(95.0)


def test_position_still_open_at_end_is_not_counted_as_a_win(monkeypatch):
    candles = [flat_bar(0), flat_bar(1), flat_bar(2), flat_bar(3), flat_bar(4)]
    patch_strategy(monkeypatch, signal_at(1))   # take_profit=50, stop=5 — never hit here
    result = Backtester(cfg()).run(candles)
    t = result.trades[0]
    assert t.exit_price is None
    assert t.exit_reason == "still_open_at_end"
    assert t.pnl == 0.0
    assert result.metrics()["trades"] == 0   # only counts closed trades
    assert result.metrics()["still_open_at_end"] == 1


def test_scanning_resumes_after_the_exit_bar_not_immediately(monkeypatch):
    small = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                       entry_reason="t", stop_loss=5.0, take_profit=50.0)
    candles = [
        flat_bar(0),
        flat_bar(1),                                    # signal 1 -> entry at 2
        flat_bar(2, o=100.0, h=101.0, l=90.0, c=95.0),    # stop hit here (exit_index=2)
        flat_bar(3),                                    # signal 2 -> entry at 4
        flat_bar(4, o=100.0, h=101.0, l=90.0, c=95.0),    # stop hit here
        flat_bar(5),
    ]
    patch_strategy(monkeypatch, signal_at(1, 3, decision=small))
    result = Backtester(cfg()).run(candles)
    assert len(result.trades) == 2
    assert result.trades[0].exit_index == 2
    assert result.trades[1].entry_index == 4


# --------------------------------------------------------------- risk manager gating

def test_low_confidence_signal_is_rejected_and_no_trade_taken(monkeypatch):
    low_conf = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=10.0,
                          entry_reason="t", stop_loss=5.0, take_profit=50.0)
    candles = [flat_bar(i) for i in range(4)]
    patch_strategy(monkeypatch, signal_at(1, decision=low_conf))
    result = Backtester(cfg(min_ai_confidence=65.0)).run(candles)
    assert result.trades == []
    assert result.signals_rejected == 1
    assert result.gate_rejections.get("confidence") == 1


def test_stop_too_wide_for_budget_is_rejected_via_position_size(monkeypatch):
    huge_stop = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                           entry_reason="t", stop_loss=100_000.0, take_profit=200_000.0)
    candles = [flat_bar(i) for i in range(4)]
    patch_strategy(monkeypatch, signal_at(1, decision=huge_stop))
    result = Backtester(cfg()).run(candles)
    assert result.trades == []
    assert result.gate_rejections.get("position_size") == 1


def test_max_trades_per_day_caps_entries_within_the_same_day(monkeypatch):
    losing = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                        entry_reason="t", stop_loss=5.0, take_profit=500.0)
    # Repeating 2-bar cycle: a neutral signal bar, then an entry/exit bar
    # whose low blows through the 5-point stop immediately.
    candles = [flat_bar(0)]
    for cycle in range(4):
        candles.append(flat_bar(len(candles)))
        candles.append(flat_bar(len(candles), o=100.0, h=101.0, l=90.0, c=95.0))
    patch_strategy(monkeypatch, signal_at(*range(len(candles)), decision=losing))
    result = Backtester(cfg(max_trades_per_day=2, max_daily_loss_pct=99.0)).run(candles)
    assert len(result.trades) == 2
    assert result.gate_rejections.get("max_trades_per_day", 0) >= 1


def test_daily_loss_limit_halts_further_entries_same_day(monkeypatch):
    losing = AIDecision(symbol="MNQ", action=AIAction.BUY, confidence=90.0,
                        entry_reason="t", stop_loss=5.0, take_profit=500.0)
    candles = [flat_bar(0)]
    for cycle in range(4):
        candles.append(flat_bar(len(candles)))
        candles.append(flat_bar(len(candles), o=100.0, h=101.0, l=90.0, c=95.0))
    # 2 losses at $250 risk (25 contracts * 5pt * $2) = $500 total > $450 cap.
    patch_strategy(monkeypatch, signal_at(*range(len(candles)), decision=losing))
    result = Backtester(cfg(max_trades_per_day=100, max_daily_loss_pct=0.9)).run(candles)
    assert len(result.trades) == 2
    assert result.gate_rejections.get("daily_loss_limit", 0) >= 1


# --------------------------------------------------------------- plumbing

def test_insufficient_history_returns_empty_result():
    result = Backtester(cfg(warmup_bars=500)).run([flat_bar(i) for i in range(5)])
    assert result.trades == []
    assert result.bars_tested == 0


def test_metrics_handle_zero_trades_without_dividing_by_zero():
    result = Backtester(cfg(warmup_bars=500)).run([flat_bar(i) for i in range(5)])
    m = result.metrics()
    assert m["win_rate"] is None
    assert m["expectancy_r"] is None
    assert m["profit_factor"] is None
    assert not math.isnan(m["return_pct"])


def test_result_is_json_serializable(monkeypatch):
    candles = [flat_bar(0), flat_bar(1), flat_bar(2, l=90.0), flat_bar(3)]
    patch_strategy(monkeypatch, signal_at(1))
    result = Backtester(cfg()).run(candles)
    import json

    json.dumps(result.to_dict(), default=str)


def test_report_leads_with_limitations(monkeypatch):
    candles = [flat_bar(0), flat_bar(1), flat_bar(2, l=90.0), flat_bar(3)]
    patch_strategy(monkeypatch, signal_at(1))
    result = Backtester(cfg()).run(candles)
    text = format_report(result)
    assert "WHAT THIS DOES NOT TEST" in text
    assert text.index("WHAT THIS DOES NOT TEST") < text.index("RESULTS")


def test_report_warns_on_small_samples(monkeypatch):
    candles = [flat_bar(0), flat_bar(1), flat_bar(2, l=90.0), flat_bar(3)]
    patch_strategy(monkeypatch, signal_at(1))
    result = Backtester(cfg()).run(candles)
    assert "too small a sample" in format_report(result)


# --------------------------------------------------------------- real strategy smoke test

def test_real_strategy_runs_end_to_end_without_crashing():
    """No monkeypatch here — exercises the actual reference strategy over a
    real price series. Not asserting specific trade counts (that would be
    testing the strategy's opinions, not the engine's correctness)."""
    candles = []
    price = 100.0
    start = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    for i in range(120):
        price += 0.3 if i % 5 != 0 else -0.5   # gentle uptrend with pullbacks
        candles.append(Candle(timestamp=start + timedelta(minutes=5 * i),
                              open=price, high=price + 1, low=price - 1,
                              close=price + 0.2, volume=1000 + i))
    result = Backtester(cfg(warmup_bars=30)).run(candles)
    assert result.bars_tested > 0
    assert isinstance(result.metrics(), dict)
    format_report(result)   # must not raise
