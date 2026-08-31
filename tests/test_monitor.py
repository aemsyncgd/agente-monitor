# tests/test_monitor.py
import time
from unittest.mock import MagicMock, patch
from src.monitor import Monitor
from src.buffer import EventBuffer
from src.config import Config, MonitoringConfig

def create_test_monitor():
    config = Config()
    config.monitoring = MonitoringConfig(
        interval_seconds=1,
        threshold_events=1,
        threshold_window_minutes=5
    )
    client = MagicMock()
    buffer = EventBuffer(window_seconds=300)
    return Monitor(config, client, buffer), client, buffer

def test_check_events_detects_los():
    monitor, client, buffer = create_test_monitor()
    
    client.get_hosts.return_value = [
        {"hostid": "1001", "name": "OLT-NARANJILLOS-1", "interfaces": [{"ip": "10.0.0.1"}]}
    ]
    
    now = int(time.time())
    client.get_items.return_value = [
        {"itemid": "2001", "name": "Status GPON0/4:2 - VSOL00B70D72 - EG8141A5", 
         "lastvalue": "1", "lastclock": str(now)}
    ]
    
    client.get_problems.return_value = [
        {"eventid": "3001", "name": "Status GPON0/4:2 - VSOL00B70D72 - EG8141A5: LOS",
         "severity": "3", "clock": str(now)}
    ]
    
    client.get_event_hosts.return_value = [
        {"name": "OLT-NARANJILLOS-1", "interfaces": [{"ip": "10.0.0.1"}]}
    ]
    
    triggered = monitor.check_events()
    
    assert triggered is not None
    assert len(buffer.get_events("OLT-NARANJILLOS-1", "GPON0/4")) > 0

def test_threshold_not_reached():
    monitor, client, buffer = create_test_monitor()
    
    client.get_hosts.return_value = [
        {"hostid": "1001", "name": "OLT-NARANJILLOS-1", "interfaces": [{"ip": "10.0.0.1"}]}
    ]
    
    now = int(time.time())
    client.get_items.return_value = [
        {"itemid": "2001", "name": "Status GPON0/4:2 - VSOL00B70D72 - EG8141A5",
         "lastvalue": "1", "lastclock": str(now)}
    ]
    
    client.get_problems.return_value = []
    client._call.return_value = []
    
    triggered = monitor.check_events()
    
    assert triggered is None
