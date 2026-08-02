"""Backtest engine correctness.

The metrics matter far less than the bias controls: a backtest that peeks at
the future or fills at the signal bar's close will report an edge that does not
exist. Those are the properties under test here.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_candles, uptrend_legs
from trading_system.backtest import Backtester, BacktestConfig, format_report
from trading_system.backtest.engine import SKIPPED_GATES, _synthetic_quote
from trading_system.models import Candle, Direction


def cfg(**kw) -> BacktestConfig:
    base = dict(symbol="SPY", timeframe="5m", starting_equity=100_000.0,
                warmup_bars=60, lookback_bars=200, apply_safety_layer=False)
    base.update(kw)
    return BacktestConfig(**base)


def trending_series(legs: int = 60) -> list[Candle]:
    """Long clean uptrend — produces structure the reference strategy reacts to."""
    return make_candles(uptrend_legs(n_legs=legs),
                        start=datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc))


def flat_series(n: int = 300) -> list[Candle]:
    return make_candles([(100, 100.2, 99.8, 100)] * n,
                        start=datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc))


# ----------------------------------------------------------------- bias controls

def test_entry_never_fills_on_the_signal_bar():
    """The close that produced the signal is not tradable once it has closed."""
    candles = trending_series()
    result = Backtester(cfg()).run(candles)
    for t in result.trades:
        # entry_index is the bar AFTER the one the decision was made on, and
        # its timestamp must match that bar.
        assert candles[t.entry_index].timestamp == t.entry_time
        assert t.entry_index >= cfg().warmup_bars + 1


def test_entry_price_is_derived_from_the_next_bar_open():
    candles = trending_series()
    result = Backtester(cfg(spread_bps=0, slippage_bps=0)).run(candles)
    if not result.trades:
        pytest.skip("no signals in this fixture")
    for t in result.trades:
        assert t.entry == pytest.approx(candles[t.entry_index].open)


def test_no_lookahead_truncating_future_bars_keeps_earlier_trades():
    """Decisions must depend only on prior bars: cutting the tail off the data
    must not change trades that were already opened before the cut."""
    candles = trending_series()
    full = Backtester(cfg()).run(candles)
    if len(full.trades) < 2:
        pytest.skip("need at least two trades to compare")

    cut_at = full.trades[-1].entry_index
    truncated = Backtester(cfg()).run(candles[:cut_at])
    for a, b in zip(truncated.trades, full.trades):
        assert a.entry_index == b.entry_index
        assert a.entry == pytest.approx(b.entry)


def test_ambiguous_bar_resolves_against_the_trader():
    """A bar spanning stop and target is unresolvable from OHLC, so the loss
    is assumed and the trade is flagged."""
    # Uptrend to open a long, then one violent bar covering both levels.
    candles = list(trending_series(40))
    last = candles[-1]
    wide = Candle(timestamp=last.timestamp + timedelta(minutes=5),
                  open=last.close, high=last.close + 50,
                  low=last.close - 50, close=last.close, volume=5000)
    candles.extend([wide, Candle(timestamp=wide.timestamp + timedelta(minutes=5),
                                 open=last.close, high=last.close + 1,
                                 low=last.close - 1, close=last.close, volume=1000)])
    result = Backtester(cfg()).run(candles)
    ambiguous = [t for t in result.closed if t.ambiguous_exit]
    if ambiguous:
        assert all(t.exit_reason == "stop_hit" for t in ambiguous)
        assert result.ambiguous_bars >= len(ambiguous)


def test_costs_are_charged_on_both_sides():
    candles = trending_series()
    free = Backtester(cfg(spread_bps=0, slippage_bps=0)).run(candles)
    costly = Backtester(cfg(spread_bps=20, slippage_bps=10)).run(candles)
    if not free.closed or not costly.closed:
        pytest.skip("no closed trades")
    assert costly.metrics()["total_costs"] > free.metrics()["total_costs"]
    assert costly.metrics()["net_pnl"] < free.metrics()["net_pnl"]


def test_open_position_at_end_is_marked_not_counted_as_a_win():
    candles = trending_series()
    result = Backtester(cfg()).run(candles)
    dangling = [t for t in result.trades if t.exit_price is None]
    for t in dangling:
        assert t.exit_reason == "still_open_at_end"
        assert t.pnl == 0.0
    # Metrics count only closed trades.
    assert result.metrics()["trades"] == len(result.closed)


# ----------------------------------------------------------------- behaviour

def test_flat_market_produces_no_trades():
    """No structure, no signals — a strategy that trades noise is broken."""
    result = Backtester(cfg()).run(flat_series())
    assert result.trades == []
    assert result.metrics()["trades"] == 0


def test_insufficient_history_returns_empty_result():
    result = Backtester(cfg(warmup_bars=500)).run(trending_series(10))
    assert result.trades == []
    assert result.bars_tested == 0


def test_r_multiple_is_consistent_with_pnl_sign():
    result = Backtester(cfg()).run(trending_series())
    for t in result.closed:
        if t.pnl > 0:
            assert t.r_multiple > 0
        elif t.pnl < 0:
            assert t.r_multiple < 0


def test_equity_curve_tracks_closed_pnl():
    result = Backtester(cfg()).run(trending_series())
    if not result.closed:
        pytest.skip("no closed trades")
    expected = result.config.starting_equity + sum(t.pnl for t in result.closed)
    assert result.equity_curve[-1] == pytest.approx(expected, abs=0.01)


def test_safety_layer_can_only_reduce_trade_count():
    candles = trending_series()
    ungated = Backtester(cfg(apply_safety_layer=False)).run(candles)
    gated = Backtester(cfg(apply_safety_layer=True)).run(candles)
    assert len(gated.trades) <= len(ungated.trades)


def test_gate_rejections_are_attributed_by_name():
    candles = trending_series()
    result = Backtester(cfg(apply_safety_layer=True)).run(candles)
    if result.gate_rejections:
        assert all(isinstance(v, int) and v > 0 for v in result.gate_rejections.values())


def test_cooldown_prevents_immediate_re_entry():
    result = Backtester(cfg(cooldown_bars=20)).run(trending_series())
    closed = sorted(result.closed, key=lambda t: t.entry_index)
    for prev, nxt in zip(closed, closed[1:]):
        if prev.exit_index is not None:
            assert nxt.entry_index > prev.exit_index


# ----------------------------------------------------------------- plumbing

def test_synthetic_quote_is_labeled_and_centred_on_close():
    candle = make_candles([(100, 101, 99, 100.0)])[0]
    q = _synthetic_quote(candle, spread_bps=10)
    assert q.provider == "synthetic"      # never mistakable for observed data
    assert q.mid == pytest.approx(100.0)
    assert q.spread == pytest.approx(0.1)
    assert q.is_valid


def test_report_leads_with_limitations():
    result = Backtester(cfg()).run(trending_series())
    text = format_report(result)
    assert "WHAT THIS DOES NOT TEST" in text
    # Every skipped gate must be named, not summarized away.
    for gate in SKIPPED_GATES:
        assert gate in text
    assert text.index("WHAT THIS DOES NOT TEST") < text.index("RESULTS")


def test_report_warns_on_small_samples():
    result = Backtester(cfg()).run(trending_series())
    if len(result.closed) < 30:
        assert "too small a sample" in format_report(result)


def test_result_is_json_serializable():
    import json

    result = Backtester(cfg()).run(trending_series())
    json.dumps(result.to_dict(), default=str)


def test_metrics_handle_zero_trades_without_dividing_by_zero():
    m = Backtester(cfg()).run(flat_series()).metrics()
    assert m["win_rate"] is None
    assert m["expectancy_r"] is None
    assert m["profit_factor"] is None
    assert not math.isnan(m["return_pct"])
