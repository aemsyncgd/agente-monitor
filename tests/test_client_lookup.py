# tests/test_client_lookup.py
import os
from src.client_lookup import ClientLookup

CSV_PATH = "/home/server/agente-monitor/grid_servicios.csv"


def test_load_csv():
    lookup = ClientLookup()
    count = lookup.load_from_csv(CSV_PATH)
    assert count > 100
    assert lookup.count > 100


def test_lookup_by_serial():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    # Get a known serial from the CSV
    all_clients = lookup.get_all()
    assert len(all_clients) > 0

    known_serial = all_clients[0].serial_onu
    found = lookup.lookup_by_serial(known_serial)
    assert found is not None
    assert found.nombre != ""


def test_lookup_case_insensitive():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    all_clients = lookup.get_all()
    known_serial = all_clients[0].serial_onu

    # Test with different case
    found = lookup.lookup_by_serial(known_serial.lower())
    assert found is not None

    found = lookup.lookup_by_serial(known_serial.upper())
    assert found is not None


def test_lookup_not_found():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    found = lookup.lookup_by_serial("NONEXISTENT_SERIAL_999")
    assert found is None


def test_search_by_name():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    all_clients = lookup.get_all()
    known_name = all_clients[0].nombre.split()[0]  # First word of name

    results = lookup.search_by_name(known_name)
    assert len(results) > 0


def test_stats():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    stats = lookup.stats()
    assert stats["total_clients"] > 100
    assert stats["active_clients"] > 0
    assert stats["last_load"] > 0
    assert stats["source"] == CSV_PATH


def test_get_all():
    lookup = ClientLookup()
    lookup.load_from_csv(CSV_PATH)

    all_clients = lookup.get_all()
    assert len(all_clients) > 100
    assert all(c.serial_onu != "" for c in all_clients)


def test_needs_reload():
    lookup = ClientLookup()
    assert lookup.needs_reload() is True  # Never loaded

    lookup.load_from_csv(CSV_PATH)
    assert lookup.needs_reload(check_interval_hours=24) is False  # Just loaded
