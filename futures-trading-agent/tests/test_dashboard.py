import json
import urllib.error
import urllib.request

import pytest

from futures_agent.dashboard.server import SnapshotStore, make_handler, render_page, serve
from futures_agent.dashboard.state import build_snapshot
from futures_agent.database.db import Database
from futures_agent.risk.manager import AccountState


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "dash.db"))
    yield d
    d.close()


def clean_account() -> AccountState:
    return AccountState(equity=50_000.0, high_water_mark=50_000.0)


# --------------------------------------------------------------- build_snapshot

def test_snapshot_on_empty_database_has_sane_defaults(db):
    snap = build_snapshot(db, clean_account())
    assert snap["positions"] == []
    assert snap["stats"]["win_rate"] is None
    assert snap["account"]["equity"] == 50_000.0
    assert snap["running"] is True
    assert snap["account"]["kill_switch_active"] is False


def test_snapshot_filters_positions_to_open_only(db):
    db.record_trade(identifier="t1", symbol="MNQ", action="BUY", contracts=1,
                    entry_price=100.0)
    db.record_trade(identifier="t2", symbol="ES", action="SELL", contracts=1,
                    entry_price=5000.0)
    db.close_trade("t2", exit_price=4990.0, pnl=-50.0)
    snap = build_snapshot(db, clean_account())
    assert [p["identifier"] for p in snap["positions"]] == ["t1"]


def test_snapshot_reflects_account_fields(db):
    account = AccountState(equity=48_000.0, high_water_mark=50_000.0, daily_pnl=-200.0,
                           trades_today=3, open_positions=1)
    snap = build_snapshot(db, account)
    assert snap["account"]["daily_pnl"] == -200.0
    assert snap["account"]["trades_today"] == 3
    assert snap["account"]["open_positions"] == 1


def test_snapshot_kill_switch_true_from_either_source(db):
    assert build_snapshot(db, clean_account(), kill_switch_active=True)["account"]["kill_switch_active"]
    tripped = AccountState(equity=1, high_water_mark=1, kill_switch_tripped=True)
    assert build_snapshot(db, tripped)["account"]["kill_switch_active"]


def test_snapshot_includes_confidence_history_and_rejections(db):
    db.record_decision(symbol="MNQ", action="BUY", confidence=80.0, entry_reason="x",
                       stop_loss=20, take_profit=60)
    db.record_rejection(symbol="ES", reason="confidence_below_floor")
    snap = build_snapshot(db, clean_account())
    assert len(snap["confidence_history"]) == 1
    assert len(snap["recent_rejections"]) == 1


def test_snapshot_is_json_serializable(db):
    db.record_trade(identifier="t1", symbol="MNQ", action="BUY", contracts=1)
    snap = build_snapshot(db, clean_account())
    json.dumps(snap, default=str)   # datetimes etc. must not blow this up


# --------------------------------------------------------------- render_page

def test_render_page_injects_snapshot_json():
    html = render_page({"account": {"equity": 12345.0}, "running": True})
    assert "12345" in html
    assert "SNAPSHOT_INJECTION_POINT" not in html
    assert "window.__SNAPSHOT__" in html


def test_render_page_raises_when_marker_missing(tmp_path, monkeypatch):
    import futures_agent.dashboard.server as server_mod

    bad_template = tmp_path / "bad.html"
    bad_template.write_text("<html><body>no marker here</body></html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "TEMPLATE_PATH", bad_template)
    with pytest.raises(RuntimeError, match="missing"):
        render_page({})


# --------------------------------------------------------------- SnapshotStore

def test_snapshot_store_set_and_get_round_trip():
    store = SnapshotStore()
    store.set({"a": 1})
    assert store.get() == {"a": 1}


def test_snapshot_store_defaults_before_anything_is_set():
    store = SnapshotStore()
    snap = store.get()
    assert snap["running"] is False


# --------------------------------------------------------------- live server

def test_server_serves_index_and_api_snapshot():
    store = SnapshotStore()
    store.set({"account": {"equity": 99999.0}, "running": True})
    server = serve(store, host="127.0.0.1", port=0)
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "99999" in body

        with urllib.request.urlopen(f"http://{host}:{port}/api/snapshot", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["account"]["equity"] == 99999.0

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://{host}:{port}/nope", timeout=5)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
