# src/models.py
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

@dataclass
class Event:
    olt_name: str          # Host en Zabbix (ej: OLT-NARANJILLOS-1)
    olt_ip: str
    puerto_pon: str
    timestamp: float
    tipo: int  # 1=LOS, 3=Online, 4=DyingGaps, 6=Offline
    item_id: str
    serial_onu: str = ""
    modelo_onu: str = ""
    nodo: str = ""         # Nodo del CSV (ej: NARANJILLOS-VIDANET)

@dataclass
class VerificationResult:
    source: str  # "zabbix", "ping", "snmp", "ssh"
    success: bool
    status: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

@dataclass
class FaultVerification:
    olt_name: str          # Host en Zabbix
    olt_ip: str
    puerto_pon: str
    confirmed: bool
    zabbix_result: Optional[VerificationResult] = None
    ping_result: Optional[VerificationResult] = None
    snmp_result: Optional[VerificationResult] = None
    ssh_result: Optional[VerificationResult] = None
    timestamp: float = 0.0
    nodo: str = ""         # Nodo del CSV

@dataclass
class Client:
    nombre: str
    serial_onu: str
    olt: str               # OLT en Zabbix (ej: OLT-NARANJILLOS-1)
    nodo: str              # Nodo del CSV (ej: NARANJILLOS-VIDANET) - columna "Router"
    puerto_pon: str
    direccion: str
    documento: str = ""
    ip_servicio: str = ""
    estado: str = ""
    zona: str = ""

@dataclass
class Alert:
    zona: str
    clientes: List[Client]
    verification: FaultVerification
    timestamp: float = 0.0
    sent: bool = False
