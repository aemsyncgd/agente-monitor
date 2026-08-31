# tests/test_notifier.py
import time
from unittest.mock import MagicMock, patch
from src.notifier import TelegramNotifier
from src.config import Config, TelegramConfig
from src.models import Alert, Client, FaultVerification, VerificationResult

def create_test_notifier():
    config = Config()
    config.telegram = TelegramConfig(
        bot_token="test_token",
        chat_id="-100123456",
        cooldown_minutes=15
    )
    return TelegramNotifier(config), config

def test_format_message():
    notifier, config = create_test_notifier()
    
    clientes = [
        Client(nombre="Cliente A", serial_onu="VSOL123", olt="OLT-NARANJILLOS-1", 
               nodo="NARANJILLOS-VIDANET", puerto_pon="GPON0/4:2", direccion="Calle Test 123"),
        Client(nombre="Cliente B", serial_onu="VSOL456", olt="OLT-NARANJILLOS-1",
               nodo="NARANJILLOS-VIDANET", puerto_pon="GPON0/4:2", direccion="Calle Test 456")
    ]
    
    verification = FaultVerification(
        olt_name="OLT-NARANJILLOS-1",
        olt_ip="10.0.0.1",
        puerto_pon="GPON0/4:2",
        confirmed=True,
        ping_result=VerificationResult(source="ping", success=True),
        timestamp=time.time()
    )
    
    message = notifier.format_message("Calle Test", clientes, verification)
    
    assert "Calle Test" in message
    assert "Cliente A" in message
    assert "Cliente B" in message
    assert "2" in message

def test_cooldown_prevents_duplicate():
    notifier, config = create_test_notifier()
    
    notifier._last_sent["Calle Test"] = time.time()
    
    result = notifier.send_alert(
        "Calle Test",
        [Client(nombre="A", serial_onu="S1", olt="O1", nodo="N1", puerto_pon="P1", direccion="D1")],
        MagicMock(confirmed=True)
    )
    
    assert result is False
