# tests/test_verifier.py
import time
from unittest.mock import MagicMock, patch
from src.verifier import Verifier
from src.config import Config, VerificationConfig
from src.models import Event, FaultVerification

def create_test_verifier():
    config = Config()
    config.verification = VerificationConfig(
        ping_enabled=True,
        ping_timeout_seconds=1,
        snmp_enabled=False,
        ssh_enabled=False
    )
    client = MagicMock()
    return Verifier(config, client), client

def test_verify_zabbix_los():
    verifier, client = create_test_verifier()
    
    client.get_hosts.return_value = [
        {"hostid": "1001", "name": "OLT-TEST", "interfaces": [{"ip": "10.0.0.1"}]}
    ]
    client.get_items.return_value = [
        {"itemid": "2001", "name": "Status GPON0/1:2", "lastvalue": "1"}
    ]
    
    result = verifier._verify_zabbix("10.0.0.1", "GPON0/1")
    
    assert result.success is True
    assert result.status == "LOS"

def test_verify_ping_success():
    verifier, client = create_test_verifier()
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = verifier._verify_ping("10.0.0.1")
        
        assert result.success is True

def test_verify_ping_failure():
    verifier, client = create_test_verifier()
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        result = verifier._verify_ping("10.0.0.1")
        
        assert result.success is False

def test_verify_fault_combined():
    verifier, client = create_test_verifier()
    
    client.get_hosts.return_value = [
        {"hostid": "1001", "name": "OLT-TEST", "interfaces": [{"ip": "10.0.0.1"}]}
    ]
    client.get_items.return_value = [
        {"itemid": "2001", "name": "Status GPON0/1:2", "lastvalue": "1"}
    ]
    
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        
        event = Event(
            olt_name="OLT-TEST",
            olt_ip="10.0.0.1",
            puerto_pon="GPON0/1",
            timestamp=time.time(),
            tipo=1,
            item_id="2001"
        )
        
        result = verifier.verify_fault(event)
        
        assert result.confirmed is True
        assert result.ping_result.success is True
