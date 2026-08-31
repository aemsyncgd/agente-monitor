# tests/test_buffer.py
import time
from unittest.mock import MagicMock
from src.buffer import EventBuffer
from src.models import Event
from src.collectors.influx_writer import CollectorInfluxWriter
from src.collectors.base import CollectorResult

def test_add_event():
    buffer = EventBuffer(window_seconds=300)
    event = Event(
        olt_name="OLT-NARANJILLOS-1",
        olt_ip="10.0.0.1",
        puerto_pon="GPON0/4:2",
        timestamp=time.time(),
        tipo=1,
        item_id="12345"
    )
    buffer.add_event(event)
    events = buffer.get_events("OLT-NARANJILLOS-1", "GPON0/4:2")
    assert len(events) == 1
    assert events[0].tipo == 1

def test_group_by_pon():
    buffer = EventBuffer(window_seconds=300)
    now = time.time()
    
    for i in range(5):
        buffer.add_event(Event(
            olt_name="OLT-NARANJILLOS-1",
            olt_ip="10.0.0.1",
            puerto_pon="GPON0/4:2",
            timestamp=now - i * 10,
            tipo=1,
            item_id=f"item_{i}"
        ))
    
    buffer.add_event(Event(
        olt_name="OLT-NARANJILLOS-1",
        olt_ip="10.0.0.1",
        puerto_pon="GPON0/5:1",
        timestamp=now,
        tipo=1,
        item_id="item_99"
    ))
    
    groups = buffer.group_by_pon()
    assert ("OLT-NARANJILLOS-1", "GPON0/4:2") in groups
    assert len(groups[("OLT-NARANJILLOS-1", "GPON0/4:2")]) == 5
    assert len(groups[("OLT-NARANJILLOS-1", "GPON0/5:1")]) == 1

def test_cleanup_old_events():
    buffer = EventBuffer(window_seconds=5)
    
    old_event = Event(
        olt_name="OLT-1",
        olt_ip="10.0.0.1",
        puerto_pon="GPON0/1",
        timestamp=time.time() - 10,
        tipo=1,
        item_id="old"
    )
    buffer.add_event(old_event)
    
    buffer.cleanup()
    events = buffer.get_events("OLT-1", "GPON0/1")
    assert len(events) == 0

def test_dedup_events():
    buffer = EventBuffer(window_seconds=300)
    now = time.time()
    
    for _ in range(5):
        buffer.add_event(Event(
            olt_name="OLT-1",
            olt_ip="10.0.0.1",
            puerto_pon="GPON0/1",
            timestamp=now,
            tipo=1,
            item_id="same_item"
        ))
    
    events = buffer.get_events("OLT-1", "GPON0/1")
    assert len(events) == 1

def test_influx_writer_buffers_on_failure_and_flushes_on_recovery():
    # 1. Simular fallo de conexión (write_points devuelve 0)
    mock_influx = MagicMock()
    mock_influx.write_points.return_value = 0

    writer = CollectorInfluxWriter(mock_influx)

    result1 = CollectorResult(
        success=True,
        metrics=[
            {"measurement": "optical_power", "tags": {"pon": "GPON0/1"}, "fields": {"rx_power": -19.5}},
            {"measurement": "optical_power", "tags": {"pon": "GPON0/2"}, "fields": {"rx_power": -21.0}},
        ],
        errors=[],
        duration_seconds=1.2,
        device_name="OLT-TEST-1",
        device_ip="10.0.0.1"
    )

    # Intentar escribir durante fallo de conexión
    written = writer.write_collector_result(result1)
    assert written == 0
    stats = writer.get_stats()
    assert stats["buffered_points"] == 2
    assert stats["error_count"] == 1
    assert stats["write_count"] == 0

    # 2. Simular recuperación de conexión (write_points acepta puntos)
    mock_influx.write_points.side_effect = lambda pts: len(pts)

    result2 = CollectorResult(
        success=True,
        metrics=[
            {"measurement": "optical_power", "tags": {"pon": "GPON0/3"}, "fields": {"rx_power": -18.2}},
        ],
        errors=[],
        duration_seconds=0.8,
        device_name="OLT-TEST-1",
        device_ip="10.0.0.1"
    )

    written_new = writer.write_collector_result(result2)

    assert written_new == 1
    stats_recovered = writer.get_stats()
    assert stats_recovered["buffered_points"] == 0
    assert stats_recovered["write_count"] == 3

def test_influx_writer_single_metric_buffer_and_recovery():
    mock_influx = MagicMock()
    mock_influx.write_point.return_value = False
    mock_influx.write_points.side_effect = lambda pts: len(pts)

    writer = CollectorInfluxWriter(mock_influx)

    # Escritura individual falla
    success = writer.write_metrics("ping_check", {"host": "10.0.0.1"}, {"latency": 1.2})
    assert success is False
    assert writer.get_stats()["buffered_points"] == 1

    # Al recuperarse la conexión, write_point funciona
    mock_influx.write_point.return_value = True

    # Siguiente escritura vacía el buffer y escribe el nuevo punto
    success2 = writer.write_metrics("ping_check", {"host": "10.0.0.1"}, {"latency": 1.1})
    assert success2 is True
    assert writer.get_stats()["buffered_points"] == 0
    assert writer.get_stats()["write_count"] == 1
