# tests/test_alert_state.py
import json
from src.alert_state import LastStateStore


def test_should_send_when_no_state(tmp_path):
    store = LastStateStore(str(tmp_path / "state.json"))
    assert store.should_send("ping:HOST", {"status": "down"}) is True


def test_duplicate_state_not_sent_again(tmp_path):
    store = LastStateStore(str(tmp_path / "state.json"))
    store.record("ping:HOST", {"status": "down"}, down_since=123)
    assert store.should_send("ping:HOST", {"status": "down"}) is False


def test_changed_state_is_sent(tmp_path):
    store = LastStateStore(str(tmp_path / "state.json"))
    store.record("ping:HOST", {"status": "down"})
    assert store.should_send("ping:HOST", {"status": "up"}) is True


def test_persistence_across_restart(tmp_path):
    path = str(tmp_path / "state.json")
    first = LastStateStore(path)
    first.record("ping:HOST", {"status": "down"})

    second = LastStateStore(path)
    assert second.state("ping:HOST") == {"status": "down"}
    assert second.should_send("ping:HOST", {"status": "down"}) is False


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json]")

    store = LastStateStore(str(path))
    assert store.should_send("ping:HOST", {"status": "down"}) is True


def test_extra_fields_preserved(tmp_path):
    store = LastStateStore(str(tmp_path / "state.json"))
    store.record("ping:HOST", {"status": "down"}, down_since=456)

    entry = store.get("ping:HOST")
    assert entry["state"] == {"status": "down"}
    assert entry["down_since"] == 456
    assert "ts" in entry


def test_clear_resets_state(tmp_path):
    store = LastStateStore(str(tmp_path / "state.json"))
    store.record("ping:HOST", {"status": "down"})
    store.clear("ping:HOST")
    assert store.should_send("ping:HOST", {"status": "down"}) is True