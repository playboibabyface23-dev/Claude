"""Broker position query, journal fallback, and drift reconciliation.

Alpaca responses are served by an httpx MockTransport, so these run offline.
The payload shapes mirror the real API: every numeric field is a string.
"""

import asyncio

import httpx
import pytest

from trading_system.broker import (
    AlpacaBroker,
    JournalPositionProvider,
    PositionSide,
    ReconcilingPositionProvider,
)
from trading_system.decision import TradeAction, TradeDecision
from trading_system.memory import TradeJournal, TradeRecord


def alpaca_broker(handler) -> AlpacaBroker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://paper-api.alpaca.markets")
    return AlpacaBroker("key", "secret", client=client)


POSITIONS_JSON = [
    {
        "symbol": "AAPL", "qty": "10", "side": "long",
        "avg_entry_price": "150.25", "market_value": "1550.00",
        "unrealized_pl": "47.50", "cost_basis": "1502.50",
    },
    {
        "symbol": "TSLA", "qty": "-5", "side": "short",
        "avg_entry_price": "240.00", "market_value": "-1180.00",
        "unrealized_pl": "20.00", "cost_basis": "-1200.00",
    },
]


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(str(tmp_path / "trades.db"))
    yield j
    j.close()


def open_trade(journal: TradeJournal, symbol: str,
               action: TradeAction = TradeAction.BUY) -> int:
    entry, stop, target = (100.0, 99.0, 103.0) if action == TradeAction.BUY \
        else (100.0, 101.0, 97.0)
    d = TradeDecision(symbol=symbol, action=action, entry=entry, stop=stop,
                      target=target, risk_pct=1.0, quantity=25.0, probability=0.7)
    return journal.record_trade(TradeRecord.from_decision(d))


# --------------------------------------------------------------- Alpaca

def test_alpaca_parses_positions_including_string_numerics():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/positions"
        assert request.headers["APCA-API-KEY-ID"] == "key"
        return httpx.Response(200, json=POSITIONS_JSON)

    positions = asyncio.run(alpaca_broker(handler).positions())
    assert len(positions) == 2

    aapl = next(p for p in positions if p.symbol == "AAPL")
    assert aapl.side == PositionSide.LONG
    assert aapl.quantity == pytest.approx(10.0)
    assert aapl.avg_entry_price == pytest.approx(150.25)
    assert aapl.unrealized_pnl == pytest.approx(47.50)
    assert aapl.source == "broker"

    tsla = next(p for p in positions if p.symbol == "TSLA")
    assert tsla.side == PositionSide.SHORT
    assert tsla.quantity == pytest.approx(5.0)   # magnitude, sign lives in side


def test_alpaca_infers_short_from_negative_qty_when_side_missing():
    def handler(request):
        return httpx.Response(200, json=[
            {"symbol": "NVDA", "qty": "-3", "avg_entry_price": "800.00"}
        ])

    positions = asyncio.run(alpaca_broker(handler).positions())
    assert positions[0].side == PositionSide.SHORT
    assert positions[0].quantity == pytest.approx(3.0)


def test_alpaca_empty_book():
    positions = asyncio.run(
        alpaca_broker(lambda r: httpx.Response(200, json=[])).positions()
    )
    assert positions == []


def test_alpaca_account_equity():
    def handler(request):
        assert request.url.path == "/v2/account"
        return httpx.Response(200, json={
            "equity": "101250.75", "last_equity": "100000.00",
            "cash": "50000.00", "buying_power": "200000.00",
        })

    account = asyncio.run(alpaca_broker(handler).account())
    assert account.equity == pytest.approx(101250.75)
    assert account.intraday_pnl == pytest.approx(1250.75)


def test_alpaca_http_error_propagates():
    def handler(request):
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(alpaca_broker(handler).positions())


# --------------------------------------------------------------- journal source

def test_journal_provider_reports_open_trades(journal):
    open_trade(journal, "SPY")
    open_trade(journal, "EURUSD", TradeAction.SELL)

    positions = asyncio.run(JournalPositionProvider(journal).positions())
    by_symbol = {p.symbol: p for p in positions}
    assert by_symbol["SPY"].side == PositionSide.LONG
    assert by_symbol["EURUSD"].side == PositionSide.SHORT
    assert by_symbol["SPY"].source == "journal"


def test_journal_provider_excludes_closed_trades(journal):
    tid = open_trade(journal, "SPY")
    journal.close_trade(tid, exit_price=103.0, pnl=75.0, outcome="win")
    assert asyncio.run(JournalPositionProvider(journal).positions()) == []


# --------------------------------------------------------------- reconciliation

def test_reconcile_without_broker_uses_journal(journal):
    open_trade(journal, "SPY")
    report = asyncio.run(ReconcilingPositionProvider(journal).reconcile())
    assert [p.symbol for p in report.positions] == ["SPY"]
    assert not report.degraded


def test_reconcile_prefers_broker_truth(journal):
    open_trade(journal, "AAPL")
    broker = alpaca_broker(lambda r: httpx.Response(200, json=POSITIONS_JSON))
    report = asyncio.run(ReconcilingPositionProvider(journal, broker).reconcile())

    assert {p.symbol for p in report.positions} == {"AAPL", "TSLA"}
    assert all(p.source == "broker" for p in report.positions)


def test_stale_journal_entry_is_healed(journal):
    """Journal thinks SPY is open, broker does not hold it -> heal, or it
    blocks every future trade forever."""
    tid = open_trade(journal, "SPY")
    broker = alpaca_broker(lambda r: httpx.Response(200, json=POSITIONS_JSON))

    report = asyncio.run(ReconcilingPositionProvider(journal, broker).reconcile())
    assert report.stale_journal_symbols == ["SPY"]
    assert tid in report.healed_trade_ids
    assert journal.open_trades() == []

    row = journal._conn.execute(
        "SELECT outcome, pnl, closed_at FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert row["outcome"] == "unknown"
    assert row["pnl"] is None          # never fabricate a PnL we did not observe
    assert row["closed_at"] is not None


def test_healing_can_be_disabled(journal):
    tid = open_trade(journal, "SPY")
    broker = alpaca_broker(lambda r: httpx.Response(200, json=POSITIONS_JSON))
    report = asyncio.run(
        ReconcilingPositionProvider(journal, broker, heal_stale_entries=False).reconcile()
    )
    assert report.stale_journal_symbols == ["SPY"]
    assert report.healed_trade_ids == []
    assert len(journal.open_trades()) == 1


def test_untracked_broker_positions_are_surfaced_not_healed(journal):
    broker = alpaca_broker(lambda r: httpx.Response(200, json=POSITIONS_JSON))
    report = asyncio.run(ReconcilingPositionProvider(journal, broker).reconcile())
    assert report.untracked_broker_symbols == ["AAPL", "TSLA"]
    # They still count against risk limits.
    assert len(report.positions) == 2


def test_broker_failure_degrades_to_journal_not_to_empty(journal):
    """Fail safe: an unreachable broker must not look like a flat book."""
    open_trade(journal, "SPY")

    def boom(request):
        raise httpx.ConnectError("connection refused")

    broker = alpaca_broker(boom)
    report = asyncio.run(ReconcilingPositionProvider(journal, broker).reconcile())

    assert report.degraded
    assert report.error
    assert [p.symbol for p in report.positions] == ["SPY"]
    assert report.healed_trade_ids == []   # never heal on a failed query


def test_drift_report_is_json_serializable(journal):
    import json

    open_trade(journal, "SPY")
    broker = alpaca_broker(lambda r: httpx.Response(200, json=POSITIONS_JSON))
    report = asyncio.run(ReconcilingPositionProvider(journal, broker).reconcile())
    json.dumps(report.to_dict())
