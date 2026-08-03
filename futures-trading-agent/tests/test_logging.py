import json

import pytest

from futures_agent import logging_setup as ls


@pytest.fixture(autouse=True)
def reset_logging_state():
    """Each test gets a fresh configuration against its own tmp_path dir —
    logging module state is a process-wide singleton otherwise."""
    ls._configured = False
    yield
    ls._configured = False


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_setup_creates_every_category_file(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    for name in ls.CATEGORIES:
        assert (tmp_path / f"{name}.log").exists()
    assert (tmp_path / "app.log").exists()


def test_setup_is_idempotent_within_one_configuration(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    root = ls.get_logger()
    handler_count = len(root.handlers)
    ls.setup_logging(log_dir=str(tmp_path))   # second call, _configured still True
    assert len(root.handlers) == handler_count


def test_log_decision_writes_valid_json(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_decision({"symbol": "MNQ", "action": "BUY", "confidence": 86})
    lines = read_lines(tmp_path / "decisions.log")
    assert len(lines) == 1
    assert lines[0]["symbol"] == "MNQ"
    assert lines[0]["event"] == "ai_decision"
    assert "at" in lines[0]


def test_log_trade_records_event_type(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_trade("submitted", {"symbol": "ES", "contracts": 1})
    lines = read_lines(tmp_path / "trades.log")
    assert lines[0]["event"] == "submitted"
    assert lines[0]["symbol"] == "ES"


def test_log_rejection_records_reason(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_rejection("confidence_below_floor", {"confidence": 40})
    lines = read_lines(tmp_path / "rejections.log")
    assert lines[0]["reason"] == "confidence_below_floor"
    assert lines[0]["confidence"] == 40


def test_log_error_records_exception_type(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_error("tradovate_auth", ValueError("bad credentials"))
    lines = read_lines(tmp_path / "errors.log")
    assert lines[0]["error_type"] == "ValueError"
    assert "bad credentials" in lines[0]["error"]


def test_log_restart_records_pid(tmp_path):
    import os

    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_restart("watchdog_relaunch")
    lines = read_lines(tmp_path / "restarts.log")
    assert lines[0]["reason"] == "watchdog_relaunch"
    assert lines[0]["pid"] == os.getpid()


def test_category_events_propagate_to_app_log(tmp_path):
    ls.setup_logging(log_dir=str(tmp_path))
    ls.log_decision({"symbol": "GC", "action": "SELL", "confidence": 70})
    app_log = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "GC" in app_log
