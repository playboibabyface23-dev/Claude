from conftest import make_candles, uptrend_legs
from trading_system.models import (
    Direction,
    LiquiditySide,
    StructureEventKind,
    SwingKind,
)
from trading_system.structure import StructureEngine


def engine() -> StructureEngine:
    return StructureEngine(swing_lookback=2)


def test_detects_swing_high_and_low():
    rows = [
        (100, 101, 99, 100),
        (100, 102, 100, 101),
        (101, 105, 101, 104),   # swing high at 105
        (104, 104, 100, 101),
        (101, 102, 98, 99),
        (99, 100, 95, 96),      # swing low at 95
        (96, 98, 96, 97),
        (97, 99, 97, 98),
    ]
    swings = engine().detect_swings(make_candles(rows))
    kinds = {(s.kind, s.price) for s in swings}
    assert (SwingKind.HIGH, 105.0) in kinds
    assert (SwingKind.LOW, 95.0) in kinds


def test_uptrend_produces_bullish_bos(trending_up_candles):
    state = engine().analyze(trending_up_candles)
    assert state.trend == Direction.BULLISH
    bos = [e for e in state.events if e.kind == StructureEventKind.BOS]
    assert bos and all(e.direction == Direction.BULLISH for e in bos)


def test_choch_on_trend_reversal():
    # Uptrend, then a hard break below the last higher low.
    rows = uptrend_legs(n_legs=4)
    price = 100.0 + 4 * 1.4
    for _ in range(6):  # collapse
        rows.append((price, price + 0.2, price - 2.2, price - 2.0))
        price -= 2.0
    state = engine().analyze(make_candles(rows))
    reversals = [e for e in state.events
                 if e.kind in (StructureEventKind.CHOCH, StructureEventKind.MSS)
                 and e.direction == Direction.BEARISH]
    assert reversals, f"expected a bearish CHOCH/MSS, got {state.events}"
    assert state.trend == Direction.BEARISH


def test_bullish_fvg_detection_and_mitigation():
    rows = [
        (100, 101, 99, 100),
        (100, 108, 100, 107),   # displacement: candle 0 high (101) < candle 2 low (103)
        (105, 109, 103, 108),
        (108, 110, 107, 109),
    ]
    fvgs = engine().detect_fvgs(make_candles(rows))
    bullish = [f for f in fvgs if f.direction == Direction.BULLISH]
    assert bullish
    gap = bullish[0]
    assert gap.bottom == 101.0 and gap.top == 103.0
    assert not gap.mitigated

    # A later candle trading back into the gap mitigates it.
    rows_mit = rows + [(109, 109, 102, 103)]
    fvgs_mit = engine().detect_fvgs(make_candles(rows_mit))
    assert [f for f in fvgs_mit if f.direction == Direction.BULLISH][0].mitigated


def test_equal_highs_form_buy_side_pool_and_sweep():
    rows = [
        (100, 101, 99, 100),
        (100, 102, 100, 101),
        (101, 110, 101, 108),   # swing high 110
        (108, 109, 104, 105),
        (105, 106, 103, 104),
        (104, 106, 103, 105),
        (105, 110.02, 104, 108),  # equal high ~110
        (108, 109, 105, 106),
        (106, 107, 104, 105),
        # sweep: wick through 110.02, close back below
        (105, 111, 104, 106),
        (106, 107, 104, 105),
    ]
    candles = make_candles(rows)
    eng = engine()
    swings = eng.detect_swings(candles)
    pools = eng.detect_liquidity_pools(candles, swings)
    buy_side = [p for p in pools if p.side == LiquiditySide.BUY_SIDE]
    assert buy_side, f"no buy-side pool found; swings={swings}"
    assert buy_side[0].swept


def test_order_block_before_bullish_break(trending_up_candles):
    state = engine().analyze(trending_up_candles)
    bullish_obs = [b for b in state.order_blocks if b.direction == Direction.BULLISH]
    assert bullish_obs, "expected at least one bullish order block in an uptrend"


def test_premium_discount_classification(trending_up_candles):
    state = engine().analyze(trending_up_candles)
    assert state.dealing_range_high is not None
    assert state.dealing_range_low is not None
    mid = (state.dealing_range_high + state.dealing_range_low) / 2
    assert state.in_discount == (trending_up_candles[-1].close < mid)


def test_summary_is_json_safe(trending_up_candles):
    import json

    state = engine().analyze(trending_up_candles)
    payload = state.summary(trending_up_candles[-1].close)
    json.dumps(payload)  # must not raise
    assert payload["trend"] == "bullish"
