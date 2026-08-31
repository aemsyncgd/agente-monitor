# src/config_ia.py
import os
import re
import yaml
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum


class MikroTikRole(str, Enum):
    PRINCIPAL = "principal"
    DISTRIBUCION = "distribucion"
    SECUNDARIO = "secundario"
    RESPALDO = "respaldo"


class NodeStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    WARNING = "warning"
    UNKNOWN = "unknown"


@dataclass
class InterfaceInfo:
    name: str = ""
    description: str = ""
    
    def to_dict(self) -> Dict[str, str]:
        return {"name": self.name, "description": self.description}


_DEFAULT_SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "")


@dataclass
class MikroTikDevice:
    ip: str = ""
    hostname: str = ""
    community: str = ""
    role: str = "principal"
    modelo: str = ""
    username: str = "admin"
    use_api: bool = False
    interfaces: List[InterfaceInfo] = field(default_factory=list)
    status: str = "unknown"
    last_seen: float = 0.0
    conectado_a: str = ""  # IP or hostname of connected device
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ip": self.ip,
            "hostname": self.hostname,
            "community": self.community,
            "role": self.role,
            "modelo": self.modelo,
            "username": self.username,
            "use_api": self.use_api,
            "interfaces": [i.to_dict() for i in self.interfaces],
            "status": self.status,
            "last_seen": self.last_seen,
            "conectado_a": self.conectado_a
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MikroTikDevice":
        interfaces = []
        for iface in data.get("interfaces", []):
            if isinstance(iface, dict):
                interfaces.append(InterfaceInfo(
                    name=iface.get("name", ""),
                    description=iface.get("description", "")
                ))
        
        return cls(
            ip=data.get("ip", ""),
            hostname=data.get("hostname", ""),
            community=data.get("community", _DEFAULT_SNMP_COMMUNITY),
            role=data.get("role", "principal"),
            modelo=data.get("modelo", ""),
            username=data.get("username", "admin"),
            use_api=data.get("use_api", False),
            interfaces=interfaces,
            status=data.get("status", "unknown"),
            last_seen=data.get("last_seen", 0.0),
            conectado_a=data.get("conectado_a", "")
        )


@dataclass
class OLTDevice:
    ip: str = ""
    hostname: str = ""
    community: str = ""
    modelo: str = ""
    pon_count: int = 8
    descripcion: str = ""
    status: str = "unknown"
    last_seen: float = 0.0
    conectado_a: str = ""  # IP or hostname of connected MikroTik
    capturar_mac: bool = True  # captura FDB (MAC de clientes) en el ciclo
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ip": self.ip,
            "hostname": self.hostname,
            "community": self.community,
            "modelo": self.modelo,
            "pon_count": self.pon_count,
            "descripcion": self.descripcion,
            "status": self.status,
            "last_seen": self.last_seen,
            "conectado_a": self.conectado_a,
            "capturar_mac": self.capturar_mac
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OLTDevice":
        return cls(
            ip=data.get("ip", ""),
            hostname=data.get("hostname", ""),
            community=data.get("community", _DEFAULT_SNMP_COMMUNITY),
            modelo=data.get("modelo", ""),
            pon_count=data.get("pon_count", 8),
            descripcion=data.get("descripcion", ""),
            status=data.get("status", "unknown"),
            last_seen=data.get("last_seen", 0.0),
            conectado_a=data.get("conectado_a", ""),
            capturar_mac=data.get("capturar_mac", True)
        )


@dataclass
class Node:
    name: str = ""
    description: str = ""
    mikrotiks: List[MikroTikDevice] = field(default_factory=list)
    olts: List[OLTDevice] = field(default_factory=list)
    status: str = "unknown"
    interval_seconds: int = 60
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "mikrotiks": [m.to_dict() for m in self.mikrotiks],
            "olts": [o.to_dict() for o in self.olts],
            "status": self.status,
            "interval_seconds": self.interval_seconds,
            "mikrotik_count": len(self.mikrotiks),
            "olt_count": len(self.olts)
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Node":
        mikrotiks = []
        for mt in data.get("mikrotiks", []):
            mikrotiks.append(MikroTikDevice.from_dict(mt))
        
        olts = []
        for olt in data.get("olts", []):
            olts.append(OLTDevice.from_dict(olt))
        
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            mikrotiks=mikrotiks,
            olts=olts,
            status=data.get("status", "unknown"),
            interval_seconds=data.get("interval_seconds", 60)
        )
    
    def get_mikrotik_by_ip(self, ip: str) -> Optional[MikroTikDevice]:
        for mt in self.mikrotiks:
            if mt.ip == ip:
                return mt
        return None
    
    def get_olt_by_ip(self, ip: str) -> Optional[OLTDevice]:
        for olt in self.olts:
            if olt.ip == ip:
                return olt
        return None
    
    def add_mikrotik(self, mikrotik: MikroTikDevice) -> bool:
        if self.get_mikrotik_by_ip(mikrotik.ip):
            return False
        self.mikrotiks.append(mikrotik)
        return True
    
    def add_olt(self, olt: OLTDevice) -> bool:
        if self.get_olt_by_ip(olt.ip):
            return False
        self.olts.append(olt)
        return True
    
    def remove_mikrotik(self, ip: str) -> bool:
        for i, mt in enumerate(self.mikrotiks):
            if mt.ip == ip:
                self.mikrotiks.pop(i)
                return True
        return False
    
    def remove_olt(self, ip: str) -> bool:
        for i, olt in enumerate(self.olts):
            if olt.ip == ip:
                self.olts.pop(i)
                return True
        return False


# === Configuración de InfluxDB ===

@dataclass
class InfluxDBConfig:
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "vidanet"
    bucket: str = "monitoreo"


# === Configuración de Collectors ===

@dataclass
class CollectorsConfig:
    interval_seconds: int = 60
    olt_timeout: int = 90
    olt_config_path: str = "/app/config/olts.conf"
    nodes_config_path: str = "/app/config/nodes.yaml"
    olt_rest_seconds: int = 60  # pausa entre pasadas del ciclo OLT (escaneo paralelo)


# === Configuración de AI ===

@dataclass
class AITrainingConfig:
    input_size: int = 1
    hidden_size: int = 64
    num_layers: int = 2
    dropout: float = 0.2
    learning_rate: float = 0.001
    batch_size: int = 32
    epochs: int = 50
    window_size: int = 1440
    validation_split: float = 0.2
    early_stopping_patience: int = 5


@dataclass
class AIConfig:
    enabled: bool = True
    model_path: str = "/app/models/optical_autoencoder.pt"
    training: AITrainingConfig = field(default_factory=AITrainingConfig)
    retrain_hours: int = 24
    min_samples: int = 1000


# === Configuración de API ===

@dataclass
class APIConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


# === Configuración de Telegram ===

@dataclass
class TelegramIAConfig:
    bot_token: str = ""
    chat_id: str = ""
    cooldown_minutes: int = 15
    enabled: bool = True


# === Configuración de Zabbix Bridge ===

@dataclass
class ZabbixConfig:
    enabled: bool = False
    server: str = "127.0.0.1"
    port: int = 10051


# === Configuración de Logging ===

@dataclass
class LoggingIAConfig:
    level: str = "INFO"
    file: str = "/app/logs/monitor_ia.log"
    max_size_mb: int = 10
    backup_count: int = 3


# === Configuración de CSV Clientes ===

@dataclass
class CSVConfig:
    path: str = "grid_servicios.csv"
    reload_hours: int = 24
    enabled: bool = True


# === Configuración de Agente IA ===

@dataclass
class AgentConfig:
    enabled: bool = True
    check_interval_seconds: int = 60
    instructions_path: str = "config/instructions.json"
    model_lightweight: bool = True  # Use lightweight model for low-resource servers


# === Configuración de Agente LLM ===

@dataclass
class LLMConfig:
    enabled: bool = False
    mode: str = "hybrid"              # autonomous | hybrid | analyst
    provider: str = "ollama"           # openrouter | ollama | anthropic
    model: str = "qwen2.5:3b-instruct"
    base_url: str = "http://localhost:11434"
    api_key_env: str = "OPENROUTER_API_KEY"
    check_interval_seconds: int = 900
    max_tokens: int = 512
    temperature: float = 0.0
    timeout_seconds: int = 300
    daily_budget_usd: float = 2.0
    analyst_report_hour: str = "08:00"
    system_prompt: str = ""
    keep_alive: str = "5m"
    num_ctx: int = 4096
    num_thread: int = 4
    log_file: str = "logs/llm_agent.log"
    events_file: str = "logs/llm_events.json"
    state_file: str = "logs/llm_state.json"
    runtime_config_file: str = "config/llm_config.json"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "check_interval_seconds": self.check_interval_seconds,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "daily_budget_usd": self.daily_budget_usd,
            "analyst_report_hour": self.analyst_report_hour,
            "system_prompt": self.system_prompt,
            "keep_alive": self.keep_alive,
            "num_ctx": self.num_ctx,
            "num_thread": self.num_thread,
            "log_file": self.log_file,
            "events_file": self.events_file,
            "state_file": self.state_file,
            "runtime_config_file": self.runtime_config_file,
        }


# === Configuración Principal ===

@dataclass
class ConfigIA:
    influxdb: InfluxDBConfig = field(default_factory=InfluxDBConfig)
    collectors: CollectorsConfig = field(default_factory=CollectorsConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    api: APIConfig = field(default_factory=APIConfig)
    telegram: TelegramIAConfig = field(default_factory=TelegramIAConfig)
    logging: LoggingIAConfig = field(default_factory=LoggingIAConfig)
    csv: CSVConfig = field(default_factory=CSVConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    zabbix: ZabbixConfig = field(default_factory=ZabbixConfig)
    nodes: List[Node] = field(default_factory=list)


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


def load_config_ia(path: str) -> ConfigIA:
    with open(path, 'r') as f:
        raw = yaml.safe_load(f) or {}
    
    raw = _apply_env_resolution(raw)
    
    config = ConfigIA()
    
    if "influxdb" in raw:
        idb = raw["influxdb"]
        config.influxdb = InfluxDBConfig(
            url=idb.get("url", "http://localhost:8086"),
            token=idb.get("token", os.environ.get("INFLUXDB_TOKEN", "")),
            org=idb.get("org", "vidanet"),
            bucket=idb.get("bucket", "monitoreo")
        )
    
    if "collectors" in raw:
        c = raw["collectors"]
        config.collectors = CollectorsConfig(
            interval_seconds=c.get("interval_seconds", 60),
            olt_timeout=c.get("olt_timeout", 90),
            olt_config_path=c.get("olt_config_path", "/app/config/olts.conf"),
            nodes_config_path=c.get("nodes_config_path", "/app/config/nodes.yaml"),
            olt_rest_seconds=c.get("olt_rest_seconds", 60)
        )
    
    if "ai" in raw:
        ai = raw["ai"]
        training = ai.get("training", {})
        config.ai = AIConfig(
            enabled=ai.get("enabled", True),
            model_path=ai.get("model_path", "/app/models/optical_autoencoder.pt"),
            training=AITrainingConfig(
                input_size=training.get("input_size", 1),
                hidden_size=training.get("hidden_size", 64),
                num_layers=training.get("num_layers", 2),
                dropout=training.get("dropout", 0.2),
                learning_rate=training.get("learning_rate", 0.001),
                batch_size=training.get("batch_size", 32),
                epochs=training.get("epochs", 50),
                window_size=training.get("window_size", 1440),
                validation_split=training.get("validation_split", 0.2),
                early_stopping_patience=training.get("early_stopping_patience", 5)
            ),
            retrain_hours=ai.get("retrain_hours", 24),
            min_samples=ai.get("min_samples", 1000)
        )
    
    if "api" in raw:
        api = raw["api"]
        config.api = APIConfig(
            host=api.get("host", "0.0.0.0"),
            port=api.get("port", 8000),
            debug=api.get("debug", False)
        )
    
    if "telegram" in raw:
        t = raw["telegram"]
        config.telegram = TelegramIAConfig(
            bot_token=t.get("bot_token", os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            chat_id=t.get("chat_id", os.environ.get("TELEGRAM_CHAT_ID", "")),
            cooldown_minutes=t.get("cooldown_minutes", 15),
            enabled=t.get("enabled", True)
        )
    
    if "logging" in raw:
        lg = raw["logging"]
        config.logging = LoggingIAConfig(
            level=lg.get("level", "INFO"),
            file=lg.get("file", "/app/logs/monitor_ia.log"),
            max_size_mb=lg.get("max_size_mb", 10),
            backup_count=lg.get("backup_count", 3)
        )
    
    if "csv" in raw:
        csv = raw["csv"]
        config.csv = CSVConfig(
            path=csv.get("path", "grid_servicios.csv"),
            reload_hours=csv.get("reload_hours", 24),
            enabled=csv.get("enabled", True)
        )
    
    if "agent" in raw:
        ag = raw["agent"]
        config.agent = AgentConfig(
            enabled=ag.get("enabled", True),
            check_interval_seconds=ag.get("check_interval_seconds", 60),
            instructions_path=ag.get("instructions_path", "config/instructions.json"),
            model_lightweight=ag.get("model_lightweight", True)
        )
    
    if "llm" in raw:
        llm = raw["llm"]
        config.llm = LLMConfig(
            enabled=llm.get("enabled", False),
            mode=llm.get("mode", "hybrid"),
            provider=llm.get("provider", "ollama"),
            model=llm.get("model", "qwen2.5:3b-instruct"),
            base_url=llm.get("base_url", "http://localhost:11434"),
            api_key_env=llm.get("api_key_env", "OPENROUTER_API_KEY"),
            check_interval_seconds=llm.get("check_interval_seconds", 900),
            max_tokens=llm.get("max_tokens", 512),
            temperature=llm.get("temperature", 0.0),
            timeout_seconds=llm.get("timeout_seconds", 300),
            daily_budget_usd=llm.get("daily_budget_usd", 2.0),
            analyst_report_hour=llm.get("analyst_report_hour", "08:00"),
            system_prompt=llm.get("system_prompt", ""),
            keep_alive=llm.get("keep_alive", "5m"),
            num_ctx=llm.get("num_ctx", 4096),
            num_thread=llm.get("num_thread", 4),
            log_file=llm.get("log_file", "logs/llm_agent.log"),
            events_file=llm.get("events_file", "logs/llm_events.json"),
            state_file=llm.get("state_file", "logs/llm_state.json"),
            runtime_config_file=llm.get("runtime_config_file", "config/llm_config.json"),
        )
    
    if "zabbix" in raw:
        zb = raw["zabbix"]
        config.zabbix = ZabbixConfig(
            enabled=zb.get("enabled", False),
            server=zb.get("server", "127.0.0.1"),
            port=zb.get("port", 10051)
        )
    
    return config


def load_nodes_config(path: str) -> List[Node]:
    """Load nodes configuration from nodes.yaml."""
    if not os.path.exists(path):
        return []
    
    with open(path, 'r') as f:
        raw = yaml.safe_load(f) or {}
    
    raw = _apply_env_resolution(raw)
    
    node_intervals = raw.get("monitoring", {}).get("node_intervals", {})
    
    nodes = []
    for node_data in raw.get("nodes", []):
        node = Node.from_dict(node_data)
        if node.interval_seconds == 60 and node.name in node_intervals:
            node.interval_seconds = node_intervals[node.name]
        nodes.append(node)
    
    return nodes
