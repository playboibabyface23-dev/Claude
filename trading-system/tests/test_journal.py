from datetime import datetime, timedelta, timezone

import pytest

from trading_system.decision import TradeAction, TradeDecision
from trading_system.memory import TradeJournal, TradeRecord


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(str(tmp_path / "trades.db"))
    yield j
    j.close()


def sample_decision() -> TradeDecision:
    return TradeDecision(
        symbol="SPY", action=TradeAction.BUY, entry=100.0, stop=99.0, target=103.0,
        risk_pct=1.0, quantity=250.0, probability=0.7, reasoning="bos + fvg + kill zone",
        timeframe="5m", session="new_york",
    )


def test_record_and_close_trade(journal):
    trade_id = journal.record_trade(
        TradeRecord.from_decision(sample_decision(), {"trend": "bullish"}, {})
    )
    assert trade_id > 0

    journal.close_trade(trade_id, exit_price=103.0, pnl=750.0, outcome="win")
    stats = journal.stats()
    assert stats["wins"] == 1
    assert stats["net_pnl"] == pytest.approx(750.0)
    assert stats["win_rate"] == pytest.approx(1.0)
    assert stats["avg_realized_rr"] == pytest.approx(3.0)


def test_realized_pnl_since_window(journal):
    now = datetime.now(timezone.utc)
    tid = journal.record_trade(TradeRecord.from_decision(sample_decision()))
    journal.close_trade(tid, 98.0, -250.0, "loss", closed_at=now)

    assert journal.realized_pnl_since(now - timedelta(hours=1)) == pytest.approx(-250.0)
    assert journal.realized_pnl_since(now + timedelta(hours=1)) == 0.0


def test_consecutive_losses_streak(journal):
    now = datetime.now(timezone.utc)
    for i in range(3):
        tid = journal.record_trade(TradeRecord.from_decision(sample_decision()))
        journal.close_trade(tid, 98.0, -250.0, "loss",
                            closed_at=now - timedelta(hours=3 - i))
    streak, last_loss = journal.consecutive_losses()
    assert streak == 3
    assert last_loss is not None

    # A win resets the streak.
    tid = journal.record_trade(TradeRecord.from_decision(sample_decision()))
    journal.close_trade(tid, 103.0, 750.0, "win", closed_at=now)
    streak, _ = journal.consecutive_losses()
    assert streak == 0


def test_gate_decisions_are_recorded(journal):
    journal.record_gate_decision("SPY", {"approved": False, "breaker": "halted",
                                         "checks": []})
    rows = journal._conn.execute("SELECT * FROM gate_decisions").fetchall()
    assert len(rows) == 1 and rows[0]["approved"] == 0


def test_trade_record_captures_full_context(journal):
    rec = TradeRecord.from_decision(
        sample_decision(),
        structure={"trend": "bullish", "bos": True},
        market={"news": "none", "atr_pct": 1.1},
        chart_ref="charts/spy_20260731.png",
    )
    rec.emotion = "calm"
    trade_id = journal.record_trade(rec)
    row = journal._conn.execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    assert row["chart_ref"].endswith(".png")
    assert row["emotion"] == "calm"
    assert "bullish" in row["structure"]
    assert row["rr_planned"] == pytest.approx(3.0)
    assert row["probability"] == pytest.approx(0.7)


def test_trades_on_day_filter(journal):
    d = sample_decision()
    journal.record_trade(TradeRecord.from_decision(d))
    today = datetime.now(timezone.utc).date()
    assert len(journal.trades_on(today)) == 1
    assert journal.trades_on(today - timedelta(days=5)) == []
