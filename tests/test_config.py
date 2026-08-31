# tests/test_config.py
import os
import tempfile
from src.config import load_config

def test_load_config_from_yaml():
    config_content = """
monitoring:
  interval_seconds: 30
  threshold_events: 3
  threshold_window_minutes: 5

verification:
  ping_enabled: true
  ssh_enabled: true

telegram:
  cooldown_minutes: 15

csv:
  path: /home/server/agente-monitor/grid_servicios.csv
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        f.flush()
        config = load_config(f.name)
    
    os.unlink(f.name)
    
    assert config.monitoring.interval_seconds == 30
    assert config.monitoring.threshold_events == 3
    assert config.verification.ping_enabled is True
    assert config.telegram.cooldown_minutes == 15
    assert "grid_servicios.csv" in config.csv.path

def test_load_config_env_override():
    os.environ["TELEGRAM_BOT_TOKEN"] = "test_token_123"
    config_content = """
monitoring:
  interval_seconds: 30
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"
csv:
  path: /home/server/agente-monitor/grid_servicios.csv
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        f.flush()
        config = load_config(f.name)
    
    os.unlink(f.name)
    del os.environ["TELEGRAM_BOT_TOKEN"]
    
    assert config.telegram.bot_token == "test_token_123"
