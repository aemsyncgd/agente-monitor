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
    password: str = ""
    api_port: int = 8728
    use_api: bool = False  # True = RouterOS API (leases PPPoE/DHCP), False = SNMP
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
            "password": self.password,
            "api_port": self.api_port,
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
            password=data.get("password", ""),
            api_port=int(data.get("api_port", 8728)),
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
class TrafficSamplerConfig:
    """Muestreo ligero de trafico por interfaz de OLT (measurement
    `olt_traffic_history`) con cadencia independiente del ciclo completo."""
    enabled: bool = True
    interval_seconds: int = 300
    walk_delay_seconds: int = 3  # pausa entre walks IF-MIB para no saturar VSOL


@dataclass
class CollectorsConfig:
    interval_seconds: int = 60
    olt_timeout: int = 90
    olt_config_path: str = "/app/config/olts.conf"
    nodes_config_path: str = "/app/config/nodes.yaml"
    olt_rest_seconds: int = 60  # pausa entre pasadas del ciclo OLT (escaneo paralelo)
    traffic_sampler: TrafficSamplerConfig = field(default_factory=TrafficSamplerConfig)


# === Configuración de Ping Monitor (ICMP) ===

@dataclass
class PingMonitorConfig:
    enabled: bool = True
    targets_path: str = "config/ping_targets.yaml"
    interval_seconds: int = 60
    alert_down_seconds: int = 120
    alert_on_recovery: bool = True
    status_window_minutes: int = 3
    high_latency_threshold_ms: int = 10


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
    state_file: str = "data/alert_state.json"


# === Configuración de Zabbix Bridge (DEPRECATED) ===

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


# === Configuración Redis ===

@dataclass
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    max_connections: int = 10
    socket_timeout: int = 5
    decode_responses: bool = True


# === Configuración Event Bus ===

@dataclass
class EventBusConfig:
    enabled: bool = True
    stream: str = "wisp:events"
    consumer_group: str = "wisp-agent"
    consumer_name: str = "agent-1"
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    dlq_stream: str = "wisp:events:dlq"
    legacy_direct_write: bool = False


# === Configuración RAG ===

@dataclass
class RAGConfig:
    enabled: bool = True
    collection_name: str = "wisp_knowledge"
    persist_directory: str = "data/chromadb"
    embedding_model: str = "all-MiniLM-L6-v2"
    chunk_size: int = 500
    chunk_overlap: int = 50
    reindex_interval_hours: int = 168
    docs_directory: str = "docs/"


# === Configuración Agent Loop ===

@dataclass
class AgentLoopConfig:
    enabled: bool = False
    heartbeat_interval_seconds: int = 30
    max_llm_failures: int = 5
    degraded_mode_enabled: bool = True
    event_timeout_seconds: int = 300
    stats_retention_hours: int = 168


# === Configuración Correlator ===

@dataclass
class CorrelatorConfig:
    enabled: bool = True
    cascade_window_seconds: int = 300
    cascade_min_nodes: int = 2
    mass_threshold: int = 3
    recurrence_window_hours: int = 24
    recurrence_min_count: int = 3
    degradation_window_hours: int = 6


# === Configuración Policy ===

@dataclass
class PolicyConfig:
    enabled: bool = True
    work_hours_start: int = 8
    work_hours_end: int = 18
    work_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    max_alerts_per_hour_per_zone: int = 5
    critical_always_immediate: bool = True
    quiet_hours_summary_interval_minutes: int = 240


# === Configuración Scheduler ===

@dataclass
class SchedulerConfig:
    enabled: bool = True
    daily_report_hour: str = "06:00"
    weekly_report_day: str = "monday"
    weekly_report_hour: str = "07:00"
    monthly_report_day: int = 1
    monthly_report_hour: str = "08:00"
    health_check_interval_minutes: int = 5
    rag_reindex_interval_hours: int = 168
    memory_cleanup_interval_hours: int = 24


# === Configuración de API czwisp (Fuente 3) ===

@dataclass
class WISPConfig:
    """Configuración de la API HTTP de czwisp (base de datos de clientes WISP).

    - `list_endpoint`: listado masivo (paginado). Se deja vacío si el ISP no
      lo expone; en ese caso se usan `document_endpoint` + semilla CSV.
    - `document_endpoint`: lookup por documento, formato
      ``GET {base_url}{document_endpoint.format(slug=isp_slug, documento=doc)}``
      con header ``x-api-key``.
    - `serial_endpoint`: lookup por serial ONU (servicio), formato
      ``GET {base_url}{serial_endpoint.format(slug=isp_slug, serial=serial)}``
      con header ``x-api-key``. Respuesta czwisp: ``{encontrado, total,
      servicios:[{cliente, servicio, plan, red, facturacion}]}``.
    """
    enabled: bool = True
    base_url: str = "https://czwisp.com"
    api_key_env: str = "WISP_API_KEY"
    isp_slug: str = ""
    list_endpoint: str = ""
    document_endpoint: str = "/api/webhooks/cliente/{slug}/cliente"
    serial_endpoint: str = "/api/webhooks/cliente/{slug}/servicio?onu={serial}"
    page_size: int = 100
    timeout_seconds: int = 30
    documents_csv: str = "data/wisp_documentos.csv"
    cache_hours: int = 24
    min_interval_ms: int = 250  # caución: espaciado mínimo entre consultas HTTP
    max_lookups_per_run: int = 400  # tope de seriales consultados por ciclo
    unknown_retry_hours: int = 168  # reconsultar seriales que WISP no conoce


# === Configuración de Enriquecimiento de Clientes ===

@dataclass
class EnrichmentConfig:
    enabled: bool = True
    interval_minutes: int = 15
    lookback_hours: int = 168
    state_file: str = "data/enrichment_state.json"


# === Configuración de Detección de Fallas NAP/Zona ===

@dataclass
class FaultDetectionConfig:
    enabled: bool = True
    interval_minutes: int = 5
    threshold_clients_los: int = 3
    broadcast_down_ratio: float = 0.6
    onu_lookback_minutes: int = 360
    status_down_values: List[int] = field(default_factory=lambda: [1, 4, 6])
    status_online_value: int = 3
    refresh_after_grouping: bool = True
    refresh_timeout_seconds: int = 30
    state_file: str = "data/fault_state.json"


# === Configuración Auditoría ===

@dataclass
class AuditConfig:
    enabled: bool = True
    log_file: str = "logs/audit.jsonl"
    retention_days: int = 90


# === Configuración Acciones ===

@dataclass
class ActionsConfig:
    approval_timeout_seconds: int = 300
    max_concurrent_actions: int = 3


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
    agent: AgentConfig = field(default_factory=AgentConfig)
    zabbix: ZabbixConfig = field(default_factory=ZabbixConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    agent_loop: AgentLoopConfig = field(default_factory=AgentLoopConfig)
    correlator: CorrelatorConfig = field(default_factory=CorrelatorConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    ping_monitor: PingMonitorConfig = field(default_factory=PingMonitorConfig)
    wisp: WISPConfig = field(default_factory=WISPConfig)
    enrichment: EnrichmentConfig = field(default_factory=EnrichmentConfig)
    fault_detection: FaultDetectionConfig = field(default_factory=FaultDetectionConfig)
    nodes: List[Node] = field(default_factory=list)
    live_refresh_seconds: int = 5  # polling de trafico en tiempo real (frontend)


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
        ts = c.get("traffic_sampler", {})
        config.collectors = CollectorsConfig(
            interval_seconds=c.get("interval_seconds", 60),
            olt_timeout=c.get("olt_timeout", 90),
            olt_config_path=c.get("olt_config_path", "/app/config/olts.conf"),
            nodes_config_path=c.get("nodes_config_path", "/app/config/nodes.yaml"),
            olt_rest_seconds=c.get("olt_rest_seconds", 60),
            traffic_sampler=TrafficSamplerConfig(
                enabled=ts.get("enabled", True),
                interval_seconds=ts.get("interval_seconds", 300),
                walk_delay_seconds=ts.get("walk_delay_seconds", 3),
            )
        )

    config.live_refresh_seconds = int(raw.get("live_refresh_seconds", 5))
    
    if "ping" in raw:
        p = raw["ping"]
        config.ping_monitor = PingMonitorConfig(
            enabled=p.get("enabled", True),
            targets_path=p.get("targets_path", "config/ping_targets.yaml"),
            interval_seconds=p.get("interval_seconds", 60),
            alert_down_seconds=p.get("alert_down_seconds", 120),
            alert_on_recovery=p.get("alert_on_recovery", True),
            status_window_minutes=p.get("status_window_minutes", 3),
            high_latency_threshold_ms=p.get("high_latency_threshold_ms", 10),
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
            enabled=t.get("enabled", True),
            state_file=t.get("state_file", "data/alert_state.json")
        )
    
    if "logging" in raw:
        lg = raw["logging"]
        config.logging = LoggingIAConfig(
            level=lg.get("level", "INFO"),
            file=lg.get("file", "/app/logs/monitor_ia.log"),
            max_size_mb=lg.get("max_size_mb", 10),
            backup_count=lg.get("backup_count", 3)
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

    if "redis" in raw:
        rd = raw["redis"]
        config.redis = RedisConfig(
            url=rd.get("url", "redis://localhost:6379/0"),
            max_connections=rd.get("max_connections", 10),
            socket_timeout=rd.get("socket_timeout", 5),
            decode_responses=rd.get("decode_responses", True)
        )

    if "event_bus" in raw:
        eb = raw["event_bus"]
        config.event_bus = EventBusConfig(
            enabled=eb.get("enabled", True),
            stream=eb.get("stream", "wisp:events"),
            consumer_group=eb.get("consumer_group", "wisp-agent"),
            consumer_name=eb.get("consumer_name", "agent-1"),
            max_retries=eb.get("max_retries", 3),
            retry_backoff_seconds=eb.get("retry_backoff_seconds", 1.0),
            dlq_stream=eb.get("dlq_stream", "wisp:events:dlq"),
            legacy_direct_write=eb.get("legacy_direct_write", False)
        )

    if "rag" in raw:
        rg = raw["rag"]
        config.rag = RAGConfig(
            enabled=rg.get("enabled", True),
            collection_name=rg.get("collection_name", "wisp_knowledge"),
            persist_directory=rg.get("persist_directory", "data/chromadb"),
            embedding_model=rg.get("embedding_model", "all-MiniLM-L6-v2"),
            chunk_size=rg.get("chunk_size", 500),
            chunk_overlap=rg.get("chunk_overlap", 50),
            reindex_interval_hours=rg.get("reindex_interval_hours", 168),
            docs_directory=rg.get("docs_directory", "docs/")
        )

    if "agent_loop" in raw:
        al = raw["agent_loop"]
        config.agent_loop = AgentLoopConfig(
            enabled=al.get("enabled", False),
            heartbeat_interval_seconds=al.get("heartbeat_interval_seconds", 30),
            max_llm_failures=al.get("max_llm_failures", 5),
            degraded_mode_enabled=al.get("degraded_mode_enabled", True),
            event_timeout_seconds=al.get("event_timeout_seconds", 300),
            stats_retention_hours=al.get("stats_retention_hours", 168)
        )

    if "correlator" in raw:
        cr = raw["correlator"]
        config.correlator = CorrelatorConfig(
            enabled=cr.get("enabled", True),
            cascade_window_seconds=cr.get("cascade_window_seconds", 300),
            cascade_min_nodes=cr.get("cascade_min_nodes", 2),
            mass_threshold=cr.get("mass_threshold", 3),
            recurrence_window_hours=cr.get("recurrence_window_hours", 24),
            recurrence_min_count=cr.get("recurrence_min_count", 3),
            degradation_window_hours=cr.get("degradation_window_hours", 6)
        )

    if "policy" in raw:
        pl = raw["policy"]
        config.policy = PolicyConfig(
            enabled=pl.get("enabled", True),
            work_hours_start=pl.get("work_hours_start", 8),
            work_hours_end=pl.get("work_hours_end", 18),
            work_days=pl.get("work_days", [0, 1, 2, 3, 4]),
            max_alerts_per_hour_per_zone=pl.get("max_alerts_per_hour_per_zone", 5),
            critical_always_immediate=pl.get("critical_always_immediate", True),
            quiet_hours_summary_interval_minutes=pl.get("quiet_hours_summary_interval_minutes", 240)
        )

    if "scheduler" in raw:
        sc = raw["scheduler"]
        config.scheduler = SchedulerConfig(
            enabled=sc.get("enabled", True),
            daily_report_hour=sc.get("daily_report_hour", "06:00"),
            weekly_report_day=sc.get("weekly_report_day", "monday"),
            weekly_report_hour=sc.get("weekly_report_hour", "07:00"),
            monthly_report_day=sc.get("monthly_report_day", 1),
            monthly_report_hour=sc.get("monthly_report_hour", "08:00"),
            health_check_interval_minutes=sc.get("health_check_interval_minutes", 5),
            rag_reindex_interval_hours=sc.get("rag_reindex_interval_hours", 168),
            memory_cleanup_interval_hours=sc.get("memory_cleanup_interval_hours", 24)
        )

    if "audit" in raw:
        au = raw["audit"]
        config.audit = AuditConfig(
            enabled=au.get("enabled", True),
            log_file=au.get("log_file", "logs/audit.jsonl"),
            retention_days=au.get("retention_days", 90)
        )

    if "actions" in raw:
        ac = raw["actions"]
        config.actions = ActionsConfig(
            approval_timeout_seconds=ac.get("approval_timeout_seconds", 300),
            max_concurrent_actions=ac.get("max_concurrent_actions", 3)
        )

    if "wisp" in raw:
        w = raw["wisp"]
        config.wisp = WISPConfig(
            enabled=w.get("enabled", True),
            base_url=w.get("base_url", "https://czwisp.com"),
            api_key_env=w.get("api_key_env", "WISP_API_KEY"),
            isp_slug=w.get("isp_slug") or os.environ.get("WISP_ISP_SLUG", ""),
            list_endpoint=w.get("list_endpoint") or os.environ.get("WISP_LIST_ENDPOINT", ""),
            document_endpoint=w.get("document_endpoint") or os.environ.get(
                "WISP_DOCUMENT_ENDPOINT",
                "/api/webhooks/cliente/{slug}/cliente?documento={documento}"),
            serial_endpoint=w.get("serial_endpoint") or os.environ.get(
                "WISP_SERIAL_ENDPOINT",
                "/api/webhooks/cliente/{slug}/servicio?onu={serial}"),
            page_size=w.get("page_size", 100),
            timeout_seconds=w.get("timeout_seconds", 30),
            documents_csv=w.get("documents_csv", "data/wisp_documentos.csv"),
            cache_hours=w.get("cache_hours", 24),
            min_interval_ms=w.get("min_interval_ms", 250),
            max_lookups_per_run=w.get("max_lookups_per_run", 400),
            unknown_retry_hours=w.get("unknown_retry_hours", 168),
        )

    if "enrichment" in raw:
        en = raw["enrichment"]
        config.enrichment = EnrichmentConfig(
            enabled=en.get("enabled", True),
            interval_minutes=en.get("interval_minutes", 15),
            lookback_hours=en.get("lookback_hours", 168),
            state_file=en.get("state_file", "data/enrichment_state.json")
        )

    if "fault_detection" in raw:
        fd = raw["fault_detection"]
        config.fault_detection = FaultDetectionConfig(
            enabled=fd.get("enabled", True),
            interval_minutes=fd.get("interval_minutes", 5),
            threshold_clients_los=fd.get("threshold_clients_los", 3),
            broadcast_down_ratio=fd.get("broadcast_down_ratio", 0.6),
            onu_lookback_minutes=fd.get("onu_lookback_minutes", 360),
            status_down_values=fd.get("status_down_values", [1, 4, 6]),
            status_online_value=fd.get("status_online_value", 3),
            refresh_after_grouping=fd.get("refresh_after_grouping", True),
            refresh_timeout_seconds=fd.get("refresh_timeout_seconds", 30),
            state_file=fd.get("state_file", "data/fault_state.json")
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
