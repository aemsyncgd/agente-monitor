# src/config.py
import os
import re
import yaml
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class MonitoringConfig:
    interval_seconds: int = 30
    threshold_events: int = 3
    threshold_window_minutes: int = 5

@dataclass
class VerificationConfig:
    ping_enabled: bool = True
    ping_timeout_seconds: int = 5
    snmp_enabled: bool = True
    ssh_enabled: bool = True

@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    cooldown_minutes: int = 15
    update_interval_minutes: int = 30

@dataclass
class CsvConfig:
    path: str = ""
    refresh_hours: int = 24

@dataclass
class MlConfig:
    model_path: str = ""
    retrain_hours: int = 24
    min_samples: int = 2
    eps: float = 0.3

@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/monitor.log"
    max_size_mb: int = 10
    backup_count: int = 3

@dataclass
class Config:
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    csv: CsvConfig = field(default_factory=CsvConfig)
    ml: MlConfig = field(default_factory=MlConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

def _resolve_env_vars(value: str) -> str:
    if not isinstance(value, str):
        return value
    pattern = r'\$\{(\w+)\}'
    def replace_env(match):
        env_var = match.group(1)
        return os.environ.get(env_var, match.group(0))
    return re.sub(pattern, replace_env, value)

def _apply_env_resolution(obj):
    if isinstance(obj, dict):
        return {k: _apply_env_resolution(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_apply_env_resolution(item) for item in obj]
    elif isinstance(obj, str):
        return _resolve_env_vars(obj)
    return obj

def load_config(path: str) -> Config:
    with open(path, 'r') as f:
        raw = yaml.safe_load(f) or {}
    
    raw = _apply_env_resolution(raw)
    
    config = Config()
    
    if "monitoring" in raw:
        m = raw["monitoring"]
        config.monitoring = MonitoringConfig(
            interval_seconds=m.get("interval_seconds", 30),
            threshold_events=m.get("threshold_events", 3),
            threshold_window_minutes=m.get("threshold_window_minutes", 5)
        )
    
    if "verification" in raw:
        v = raw["verification"]
        config.verification = VerificationConfig(
            ping_enabled=v.get("ping_enabled", True),
            ping_timeout_seconds=v.get("ping_timeout_seconds", 5),
            snmp_enabled=v.get("snmp_enabled", True),
            ssh_enabled=v.get("ssh_enabled", True)
        )
    
    if "telegram" in raw:
        t = raw["telegram"]
        config.telegram = TelegramConfig(
            bot_token=t.get("bot_token", os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            chat_id=t.get("chat_id", os.environ.get("TELEGRAM_CHAT_ID", "")),
            cooldown_minutes=t.get("cooldown_minutes", 15),
            update_interval_minutes=t.get("update_interval_minutes", 30)
        )
    
    if "csv" in raw:
        c = raw["csv"]
        config.csv = CsvConfig(
            path=c.get("path", ""),
            refresh_hours=c.get("refresh_hours", 24)
        )
    
    if "ml" in raw:
        ml = raw["ml"]
        config.ml = MlConfig(
            model_path=ml.get("model_path", ""),
            retrain_hours=ml.get("retrain_hours", 24),
            min_samples=ml.get("min_samples", 2),
            eps=ml.get("eps", 0.3)
        )
    
    if "logging" in raw:
        lg = raw["logging"]
        config.logging = LoggingConfig(
            level=lg.get("level", "INFO"),
            file=lg.get("file", "logs/monitor.log"),
            max_size_mb=lg.get("max_size_mb", 10),
            backup_count=lg.get("backup_count", 3)
        )
    
    return config
