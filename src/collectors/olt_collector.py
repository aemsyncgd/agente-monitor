# src/collectors/olt_collector.py
import re
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from .base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

# Cache persistente de contadores para derivar tasas rx/tx de las interfaces
# de la OLT (sobrevive al recreado de collectors por recarga de config).
# Clave: (olt_ip, ifIndex). Las OLTs se escriben cada ~15-20 min, por lo que
# se aceptan deltas de hasta 3600s (1h) entre lecturas.
_RATE_CACHE: Dict[Tuple[str, str], Tuple[float, int, int]] = {}
_RATE_MAX_DELTA = 3600.0

# ifName de ONU en la OLT (con o sin descripcion): 'GPON05ONU13',
# 'GPON05ONU13 MARIA_PILAR_ARIAS' o subinterfaz 'GPON05ONU13.1'.
MAC_IFACE_RE = re.compile(r"^GPON0*(\d+)ONU0*(\d+)(?:[.\s].*)?$", re.IGNORECASE)
GE_IFACE_RE = re.compile(r"^GE\d+/\d+$", re.IGNORECASE)
# Puerto de la tabla FDB privada VSOL: 'PON5:ONU16' (tambien 'GE3', 'CPU').
VSOL_PORT_RE = re.compile(r"^PON(\d+):ONU(\d+)$", re.IGNORECASE)
# Puerto GE tal como lo reporta VSOL: 'GE3' (dot1d usa 'GE0/3').
VSOL_GE_RE = re.compile(r"^GE\d+(?:/\d+)?$", re.IGNORECASE)


def normalize_mac(raw: str) -> Optional[str]:
    """'00 EB D8 17 E9 91' o '00:eb:...' -> '00:EB:D8:17:E9:91'."""
    hex_pairs = re.findall(r"[0-9A-Fa-f]{2}", str(raw))
    if len(hex_pairs) != 6:
        return None
    return ":".join(p.upper() for p in hex_pairs)


def classify_fdb_interface(name: str) -> str:
    """Clasifica el ifName aprendido en FDB: 'onu' | 'ge' | 'otro'."""
    if MAC_IFACE_RE.match(name):
        return "onu"
    if GE_IFACE_RE.match(name):
        return "ge"
    return "otro"


def classify_interface_name(name: str) -> Optional[str]:
    """Clasifica el ifName de la tabla IF-MIB: 'GE' | 'PON' | 'ONU' | None.

    'GPON05ONU17' -> ONU; 'GPON0/5' -> PON; 'VLANxxx' -> PON; 'GE0/1' -> GE.
    """
    name_upper = str(name).upper()
    if GE_IFACE_RE.match(name_upper):
        return "GE"
    if re.match(r'^GPON\d+/\d+$', name_upper) or name_upper.startswith("VLAN"):
        return "PON"
    if re.search(r'\dONU\d', name_upper):
        return "ONU"
    return None


def parse_onu_iface(name: str) -> Optional[Tuple[int, int]]:
    """'GPON05ONU17' -> (5, 17); soporta sufijo '.N' de subinterfaz."""
    m = MAC_IFACE_RE.match(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_vsol_port(port: str) -> Optional[Tuple[int, int]]:
    """'PON5:ONU16' -> (5, 16). Devuelve None para 'GE3', 'CPU', etc."""
    m = VSOL_PORT_RE.match(str(port).strip().strip('"'))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def build_mac_metrics(
    fdb_macs: Dict[str, str],
    fdb_ports: Dict[str, str],
    if_desc: Dict[str, str],
    olt_name: str,
    olt_ip: str,
    nodo: str,
    onu_status: Optional[Dict[str, str]] = None,
    onu_serial: Optional[Dict[str, str]] = None,
    onu_model: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Construye puntos onu_mac (log) y onu_location (solo interfaz ONU).

    fdb_macs/fdb_ports/if_desc vienen de _subprocess_bulkwalk: {suffix: value}.
    La FDB indexa por los bytes de la MAC (suffix compartido entre col1 y col2);
    el valor de col2 es el ifIndex, que se resuelve con if_desc (ifName).
    """
    onu_status = onu_status or {}
    onu_serial = onu_serial or {}
    onu_model = onu_model or {}
    metrics: List[Dict[str, Any]] = []

    for suffix, mac_raw in fdb_macs.items():
        mac = normalize_mac(mac_raw)
        if not mac:
            continue
        port = fdb_ports.get(suffix)
        if port is None:
            continue
        if_name = if_desc.get(port, "")

        interface_type = classify_fdb_interface(if_name)
        tags = {
            "olt_name": olt_name,
            "olt_ip": olt_ip,
            "nodo": nodo,
            "mac": mac,
            "interface_name": if_name if if_name else f"ifIndex-{port}",
            "interface_type": interface_type,
        }
        fields = {"learned": 1}

        parsed = parse_onu_iface(if_name) if if_name else None
        if interface_type == "onu" and parsed:
            pon, onu = parsed
            idx_dot = f"{pon}.{onu}"
            location = f"GPON{pon:02d}_{onu}"
            tags["location"] = location
            fields.update({
                "pon_port": f"GPON0/{pon}",
                "onu_index": f"{pon}_{onu}",
            })
            serial = onu_serial.get(idx_dot)
            modelo = onu_model.get(idx_dot)
            estado = onu_status.get(idx_dot)
            if serial:
                fields["onu_serial"] = str(serial).strip('"')
            if modelo:
                fields["onu_model"] = str(modelo).strip('"')
            if estado is not None:
                try:
                    fields["estado_onu"] = int(estado)
                except (ValueError, TypeError):
                    pass
        elif interface_type == "onu":
            # ifName parece ONU pero el regex no lo parseo: log como otro
            tags["interface_type"] = "otro"

        metrics.append({"measurement": "onu_mac", "tags": dict(tags), "fields": dict(fields)})

        if tags["interface_type"] == "onu":
            loc_fields = {k: v for k, v in fields.items() if k != "learned"}
            metrics.append({
                "measurement": "onu_location",
                "tags": {k: tags[k] for k in
                         ("olt_name", "olt_ip", "nodo", "mac", "location")},
                "fields": loc_fields,
            })

    return metrics


def build_vsol_mac_metrics(
    macs: Dict[str, str],
    ports: Dict[str, str],
    olt_name: str,
    olt_ip: str,
    nodo: str,
    vlans: Optional[Dict[str, str]] = None,
    onu_status: Optional[Dict[str, str]] = None,
    onu_serial: Optional[Dict[str, str]] = None,
    onu_model: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Construye puntos onu_mac (log) y onu_location desde la FDB privada VSOL.

    macs/ports/vlans vienen de _subprocess_bulkwalk sobre OID_VSOL_MAC,
    OID_VSOL_PORT y OID_VSOL_VLAN: {suffix: value}. A diferencia de dot1d
    (que colapsa toda MAC al uplink GE), la columna de puerto ya trae la
    atribucion por ONU ('PON5:ONU16'), por lo que la MAC del CPE/router se
    asocia al puerto donde esta instalado el cliente.
    """
    vlans = vlans or {}
    onu_status = onu_status or {}
    onu_serial = onu_serial or {}
    onu_model = onu_model or {}
    metrics: List[Dict[str, Any]] = []

    for suffix, mac_raw in macs.items():
        mac = normalize_mac(mac_raw)
        if not mac:
            continue
        port = ports.get(suffix)
        if port is None:
            continue
        port_name = str(port).strip().strip('"')

        parsed = parse_vsol_port(port_name)
        if parsed:
            interface_type = "onu"
        elif VSOL_GE_RE.match(port_name):
            interface_type = "ge"
        else:
            interface_type = "otro"

        tags = {
            "olt_name": olt_name,
            "olt_ip": olt_ip,
            "nodo": nodo,
            "mac": mac,
            "interface_name": port_name if port_name else "desconocido",
            "interface_type": interface_type,
        }
        fields: Dict[str, Any] = {"learned": 1}

        vlan_raw = vlans.get(suffix)
        if vlan_raw is not None:
            try:
                fields["vlan"] = int(str(vlan_raw).strip().strip('"'))
            except (ValueError, TypeError):
                pass

        if interface_type == "onu" and parsed:
            pon, onu = parsed
            idx_dot = f"{pon}.{onu}"
            tags["location"] = f"GPON{pon:02d}_{onu}"
            fields.update({
                "pon_port": f"GPON0/{pon}",
                "onu_index": f"{pon}_{onu}",
            })
            serial = onu_serial.get(idx_dot)
            modelo = onu_model.get(idx_dot)
            estado = onu_status.get(idx_dot)
            if serial:
                fields["onu_serial"] = str(serial).strip('"')
            if modelo:
                fields["onu_model"] = str(modelo).strip('"')
            if estado is not None:
                try:
                    fields["estado_onu"] = int(estado)
                except (ValueError, TypeError):
                    pass

        metrics.append({"measurement": "onu_mac", "tags": dict(tags), "fields": dict(fields)})

        if interface_type == "onu":
            loc_fields = {k: v for k, v in fields.items() if k != "learned"}
            metrics.append({
                "measurement": "onu_location",
                "tags": {k: tags[k] for k in
                         ("olt_name", "olt_ip", "nodo", "mac", "location")},
                "fields": loc_fields,
            })

    return metrics


@dataclass
class OltConfig:
    ip: str
    community: str
    hostname: str  # Zabbix hostname
    modelo: str = ""  # V1600G0B, V1600G1, V1600G2B
    nodo: str = ""  # Nodo del CSV
    capturar_mac: bool = True  # captura FDB (MAC de clientes)
    
    @classmethod
    def from_line(cls, line: str) -> Optional["OltConfig"]:
        parts = line.strip().split()
        if len(parts) >= 3:
            return cls(ip=parts[0], community=parts[1], hostname=parts[2])
        return None


@dataclass
class OnuData:
    pon_port: str
    onu_index: str
    location: str
    description: str
    serial: str
    modelo: str
    status: int
    rx_power: Optional[float] = None
    tx_power: Optional[float] = None


@dataclass
class InterfaceData:
    name: str
    index: str
    interface_type: str  # "GE", "PON", "ONU"
    ifHCInOctets: Optional[int] = None
    ifHCOutOctets: Optional[int] = None
    ifOperStatus: Optional[int] = None
    rx_bps: Optional[float] = None
    tx_bps: Optional[float] = None


class OltCollector(BaseCollector):
    # OIDs de vsol_daemon.sh V11
    OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
    OID_SYS_DESC = "1.3.6.1.2.1.1.1.0"
    OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
    OID_SYS_CPU = "1.3.6.1.4.1.37950.1.1.5.10.12.3.0"
    OID_SYS_MEM = "1.3.6.1.4.1.37950.1.1.5.10.12.4.0"
    OID_SYS_TEMP = "1.3.6.1.4.1.37950.1.1.5.10.12.5.9.0"
    
    OID_IF_DESC = "1.3.6.1.2.1.31.1.1.1.1"
    OID_IF_HCIN = "1.3.6.1.2.1.31.1.1.1.6"
    OID_IF_HCOUT = "1.3.6.1.2.1.31.1.1.1.10"
    OID_IF_STATUS = "1.3.6.1.2.1.2.2.1.8"
    
    OID_ONU_STATUS = "1.3.6.1.4.1.37950.1.1.6.1.1.1.1.5"
    OID_ONU_RX = "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7"
    OID_ONU_TX = "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.6"
    OID_ONU_IDENT = "1.3.6.1.4.1.37950.1.1.6.1.1.2.1"

    OID_FDB_MAC = "1.3.6.1.2.1.17.4.3.1.1"   # dot1qTpFdbTable.1 = MAC (col1)
    OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"  # dot1qTpFdbTable.2 = puerto (col2)

    # FDB privada VSOL: atribuye la MAC del CPE/router al puerto PON:ONU donde
    # esta instalado el cliente (dot1d solo ve el uplink GE). Ver
    # docs/olt_mac_snmp.md. Columnas: .2 VLAN, .3 MAC, .4 tipo, .5 puerto.
    OID_VSOL_MAC_TABLE = "1.3.6.1.4.1.37950.1.1.5.10.3.2.1"
    OID_VSOL_VLAN = "1.3.6.1.4.1.37950.1.1.5.10.3.2.1.2"
    OID_VSOL_MAC = "1.3.6.1.4.1.37950.1.1.5.10.3.2.1.3"
    OID_VSOL_PORT = "1.3.6.1.4.1.37950.1.1.5.10.3.2.1.5"
    
    def __init__(self, config: OltConfig, timeout: int = 60):
        super().__init__(f"olt-{config.hostname}")
        self.config = config
        self.timeout = timeout
        self._batch_size = 8
        self._g2b_mode = False
        self._g2b_rxtx_offset = 0
        self.mac_walk_delay = 3  # segundos entre walks de FDB (no saturar OLT)
        # Dedupe de onu_mac/onu_location: estas mediciones describen el estado
        # FDB (learned=1) que el dashboard solo consulta con last() por MAC.
        # Reescribir toda la FDB en cada ciclo (~8 min) creaba ~14k pts/ciclo
        # y saturaba InfluxDB; solo se emiten series cuyo estado cambio.
        self._mac_seen: Dict[str, str] = {}
    
    def collect(self) -> CollectorResult:
        start = time.time()
        metrics = []
        errors = []
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            result = loop.run_until_complete(self._collect_async())
            metrics = result["metrics"]
            errors = result["errors"]
            
            loop.close()
            
        except Exception as e:
            errors.append(f"Collection failed: {e}")
            logger.error(f"{self.config.hostname}: {e}")
        
        duration = time.time() - start
        
        if errors:
            self._record_error()
        else:
            self._record_success(duration)
        
        return CollectorResult(
            success=len(errors) == 0,
            metrics=metrics,
            errors=errors,
            duration_seconds=duration,
            device_name=self.config.hostname,
            device_ip=self.config.ip
        )

    def capture_macs(self) -> CollectorResult:
        """Runner manual: captura FDB (MAC de clientes) sin el ciclo completo."""
        start = time.time()
        metrics: List[Dict[str, Any]] = []
        errors: List[str] = []

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self._capture_macs_async())
            metrics = result["metrics"]
            errors = result["errors"]
            loop.close()
        except Exception as e:
            errors.append(f"MAC capture failed: {e}")
            logger.error(f"{self.config.hostname}: {e}")

        duration = time.time() - start

        if errors:
            self._record_error()
        else:
            self._record_success(duration)

        return CollectorResult(
            success=len(errors) == 0,
            metrics=metrics,
            errors=errors,
            duration_seconds=duration,
            device_name=self.config.hostname,
            device_ip=self.config.ip
        )

    def sample_traffic(
        self,
        if_desc: Optional[Dict[str, str]] = None,
        if_status: Optional[Dict[str, str]] = None,
        if_in: Optional[Dict[str, str]] = None,
        if_out: Optional[Dict[str, str]] = None,
        now: Optional[float] = None,
    ) -> CollectorResult:
        """Muestreo ligero de trafico por interfaz (measurement
        `olt_traffic_history`) sin el ciclo completo (no escanea ONUs ni FDB).

        Los parametros `if_*` permiten inyectar tablas SNMP en tests; si no se
        entregan, se ejecutan los 4 walks IF-MIB con pausas para no saturar la
        VSOL. `now` tambien se inyecta en tests para tasas deterministas.
        """
        start = time.time()
        metrics: List[Dict[str, Any]] = []
        errors: List[str] = []

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self._sample_traffic_async(
                if_desc=if_desc, if_status=if_status,
                if_in=if_in, if_out=if_out, now=now,
            ))
            metrics = result["metrics"]
            errors = result["errors"]
            loop.close()
        except Exception as e:
            errors.append(f"Traffic sample failed: {e}")
            logger.error(f"{self.config.hostname}: {e}")

        duration = time.time() - start
        if errors:
            self._record_error()
        else:
            self._record_success(duration)

        return CollectorResult(
            success=len(errors) == 0,
            metrics=metrics,
            errors=errors,
            duration_seconds=duration,
            device_name=self.config.hostname,
            device_ip=self.config.ip
        )

    async def _sample_traffic_async(
        self,
        if_desc: Optional[Dict[str, str]] = None,
        if_status: Optional[Dict[str, str]] = None,
        if_in: Optional[Dict[str, str]] = None,
        if_out: Optional[Dict[str, str]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Cuerpo async del muestreo: 4 walks IF-MIB secuenciales y emision de
        puntos `olt_traffic_history` (tasa rx/tx, contadores y estado)."""
        metrics: List[Dict[str, Any]] = []
        errors: List[str] = []
        now_val = now if now is not None else time.time()
        try:
            if if_desc is None:
                if_desc = await self._subprocess_bulkwalk(self.OID_IF_DESC, timeout=30)
                await asyncio.sleep(self.mac_walk_delay)
            if if_status is None:
                if_status = await self._subprocess_bulkwalk(self.OID_IF_STATUS, timeout=30)
                await asyncio.sleep(self.mac_walk_delay)
            if if_in is None:
                if_in = await self._subprocess_bulkwalk(self.OID_IF_HCIN, timeout=30)
                await asyncio.sleep(self.mac_walk_delay)
            if if_out is None:
                if_out = await self._subprocess_bulkwalk(self.OID_IF_HCOUT, timeout=30)

            if if_status is None:
                if_status = {}

            for idx, name_raw in (if_desc or {}).items():
                iface_type = classify_interface_name(str(name_raw))
                if iface_type is None:
                    continue

                rx_octets = self._parse_int(if_in.get(idx) if if_in else None)
                tx_octets = self._parse_int(if_out.get(idx) if if_out else None)
                status = self._parse_int(if_status.get(idx))
                rx_bps, tx_bps = self._compute_rates(idx, rx_octets, tx_octets, now_val)

                tags = {
                    "olt_name": self.config.hostname,
                    "olt_ip": self.config.ip,
                    "interface_name": str(name_raw),
                    "interface_type": iface_type,
                    "interface_index": str(idx),
                }
                parsed = parse_onu_iface(str(name_raw))
                if iface_type == "ONU" and parsed:
                    pon, onu = parsed
                    tags["pon_port"] = f"GPON0/{pon}"
                    tags["onu_index"] = f"{pon}_{onu}"
                elif iface_type == "PON":
                    m = re.match(r'^GPON(\d+)/(\d+)$', str(name_raw).upper())
                    if m:
                        tags["pon_port"] = f"GPON0/{m.group(2)}"

                fields = {}
                if rx_bps is not None:
                    fields["rx_bps"] = rx_bps
                if tx_bps is not None:
                    fields["tx_bps"] = tx_bps
                if rx_octets is not None:
                    fields["rx_octets"] = rx_octets
                if tx_octets is not None:
                    fields["tx_octets"] = tx_octets
                if status is not None:
                    fields["status"] = status

                if fields:
                    metrics.append({
                        "measurement": "olt_traffic_history",
                        "tags": dict(tags),
                        "fields": dict(fields),
                    })

            if not metrics:
                errors.append(f"Sin interfaces clasificables en {self.config.hostname}")
        except Exception as e:
            errors.append(f"Traffic sample failed: {e}")
            logger.error(f"{self.config.hostname}: {e}")

        return {"metrics": metrics, "errors": errors}

    async def _collect_async(self) -> Dict[str, Any]:
        metrics = []
        errors = []
        
        try:
            from pysnmp.hlapi.v3arch.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget,
                ContextData, ObjectType, ObjectIdentity
            )
        except ImportError:
            errors.append("pysnmp not installed")
            return {"metrics": metrics, "errors": errors}
        
        snmp_engine = SnmpEngine()
        community = CommunityData(self.config.community)
        transport = await UdpTransportTarget.create(
            (self.config.ip, 161),
            timeout=8,
            retries=1
        )
        context = ContextData()
        
        # 1. Health check + system data via pysnmp (fast scalar gets).
        # Con el escaneo en paralelo (9 OLTs a la vez), las OLTs lentas (SISAL)
        # a veces no responden al primer intento; reintentar antes de declarar
        # la OLT caida para no perder la pasada completa.
        sys_desc = None
        for attempt in range(3):
            sys_desc = await self._snmp_get(
                snmp_engine, community, transport, context,
                self.OID_SYS_DESC
            )
            if sys_desc is not None:
                break
            if attempt < 2:
                await asyncio.sleep(3)
        if sys_desc is None:
            errors.append(f"OLT {self.config.ip} not responding")
            snmp_engine.close_dispatcher()
            return {"metrics": metrics, "errors": errors}
        
        if "G2B" in str(sys_desc):
            self._g2b_mode = True
            self._batch_size = 16
        
        sys_data = await self._collect_system_data(
            snmp_engine, community, transport, context
        )
        snmp_engine.close_dispatcher()
        
        if sys_data:
            metrics.append({
                "measurement": "olt_system",
                "tags": {
                    "olt_name": self.config.hostname,
                    "olt_ip": self.config.ip,
                    "modelo": sys_data.get("modelo", "")
                },
                "fields": {
                    "cpu": sys_data.get("cpu", 0.0),
                    "memory": sys_data.get("memory", 0.0),
                    "temperature": sys_data.get("temperature", 0.0),
                    "uptime": sys_data.get("uptime", 0),
                    "total_onus": 0,
                    "online_onus": 0,
                    "sys_name": sys_data.get("name", ""),
                    "sys_desc": sys_data.get("description", "")
                }
            })
        
        # 2. Walks via subprocess - sequential with cooldown to avoid overwhelming OLT
        onu_status = await self._subprocess_bulkwalk(self.OID_ONU_STATUS)
        await asyncio.sleep(3)
        
        onu_serial = await self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.5")
        await asyncio.sleep(3)
        
        onu_model = await self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.6")
        await asyncio.sleep(3)
        
        # Interface walks (optional, skip if OLT is slow)
        if_desc = await self._subprocess_bulkwalk(self.OID_IF_DESC, timeout=30)
        await asyncio.sleep(2)
        
        if if_desc:
            if_status = await self._subprocess_bulkwalk(self.OID_IF_STATUS, timeout=30)
            await asyncio.sleep(2)
            if_in = await self._subprocess_bulkwalk(self.OID_IF_HCIN, timeout=30)
            await asyncio.sleep(2)
            if_out = await self._subprocess_bulkwalk(self.OID_IF_HCOUT, timeout=30)
        else:
            if_status = {}
            if_in = {}
            if_out = {}
        
        # Parse interfaces
        now = time.time()
        for idx, name in if_desc.items():
            iface_type = self._classify_interface(name)
            if iface_type is None:
                continue
            
            fields = {}
            ifHCIn = self._parse_int(if_in.get(idx))
            ifHCOut = self._parse_int(if_out.get(idx))
            ifOperStatus = self._parse_int(if_status.get(idx))
            
            # Tasas rx/tx (bps) derivadas de contadores 64 bits entre ciclos
            rx_bps, tx_bps = self._compute_rates(idx, ifHCIn, ifHCOut, now)
            
            if ifHCIn is not None:
                fields["ifHCInOctets"] = ifHCIn
            if ifHCOut is not None:
                fields["ifHCOutOctets"] = ifHCOut
            if ifOperStatus is not None:
                fields["ifOperStatus"] = ifOperStatus
            if rx_bps is not None:
                fields["rx_bps"] = rx_bps
            if tx_bps is not None:
                fields["tx_bps"] = tx_bps
            
            if fields:
                metrics.append({
                    "measurement": "interface_traffic",
                    "tags": {
                        "device_name": self.config.hostname,
                        "device_ip": self.config.ip,
                        "interface_name": name,
                        "interface_type": iface_type,
                        "interface_index": str(idx)
                    },
                    "fields": fields
                })
        
        # Parse ONUs
        online_indices = []
        for idx_dot, status_str in onu_status.items():
            status_val = self._parse_int(status_str)
            if status_val == 3:
                online_indices.append(idx_dot)
        
        # RX/TX for online ONUs via subprocess snmpget (sequential batches)
        rxtx_data = {}
        if online_indices:
            await asyncio.sleep(5)  # Cooldown after walks before RX/TX
            rxtx_data = await self._get_rxtx_batch_subprocess(online_indices)
        
        online_count = 0
        total_count = len(onu_status)
        
        for idx_dot, status_str in onu_status.items():
            status_val = self._parse_int(status_str)
            if status_val is None:
                continue
            
            if status_val == 3:
                online_count += 1
            
            parts = idx_dot.split(".")
            if len(parts) < 2:
                continue
            
            pon_id = parts[0]
            onu_id = parts[1]
            pon_port = f"GPON0/{pon_id}"
            onu_index = f"{pon_id}_{onu_id}"
            
            serial = onu_serial.get(idx_dot, f"ONU_{onu_index}")
            modelo = onu_model.get(idx_dot, "unknown")
            
            rx_power = None
            tx_power = None
            if idx_dot in rxtx_data:
                rx_power = rxtx_data[idx_dot].get("rx")
                tx_power = rxtx_data[idx_dot].get("tx")
            
            metrics.append({
                "measurement": "optical_power",
                "tags": {
                    "olt_name": self.config.hostname,
                    "olt_ip": self.config.ip,
                    "pon_port": pon_port,
                    "onu_index": onu_index,
                    "onu_serial": str(serial).strip('"'),
                    "onu_model": str(modelo).strip('"'),
                    "nodo": self.config.nodo
                },
                "fields": {
                    "status": status_val,
                    **({"rx_power": rx_power} if rx_power is not None else {}),
                    **({"tx_power": tx_power} if tx_power is not None else {})
                }
            })
        
        # 3. FDB: MAC de equipos de clientes (opcional, flag capturar_mac)
        if self.config.capturar_mac:
            await asyncio.sleep(2)
            mac_result = await self._capture_macs_async(
                if_desc=if_desc,
                onu_status=onu_status,
                onu_serial=onu_serial,
                onu_model=onu_model,
            )
            metrics.extend(mac_result["metrics"])
            errors.extend(mac_result["errors"])
        
        # Update OLT system with ONU counts
        for m in metrics:
            if m["measurement"] == "olt_system":
                m["fields"]["total_onus"] = total_count
                m["fields"]["online_onus"] = online_count
        
        return {"metrics": metrics, "errors": errors}
    
    async def _collect_system_data(
        self, snmp_engine, community, transport, context
    ) -> Optional[Dict[str, Any]]:
        results = await asyncio.gather(
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_NAME),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_DESC),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_UPTIME),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_CPU),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_MEM),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_TEMP),
            return_exceptions=True
        )
        
        sys_name = str(results[0] or "")
        sys_desc = str(results[1] or "")
        uptime_raw = self._parse_int(results[2])
        cpu = self._parse_float(results[3])
        mem = self._parse_float(results[4])
        temp = self._parse_float(results[5])
        
        uptime = uptime_raw // 100 if uptime_raw else 0
        
        modelo = ""
        if "G2B" in sys_desc:
            modelo = "V1600G2B"
        elif "G1" in sys_desc:
            modelo = "V1600G1"
        elif "G0B" in sys_desc:
            modelo = "V1600G0B"
        
        return {
            "name": sys_name,
            "description": sys_desc,
            "uptime": uptime,
            "cpu": cpu,
            "memory": mem,
            "temperature": temp,
            "modelo": modelo
        }
    
    async def _collect_interfaces(
        self, snmp_engine, community, transport, context
    ) -> List[InterfaceData]:
        interfaces = []
        
        # Walk IF_DESC
        if_desc = await self._snmp_walk(
            snmp_engine, community, transport, context,
            self.OID_IF_DESC
        )
        
        if_in = await self._snmp_walk(
            snmp_engine, community, transport, context,
            self.OID_IF_HCIN
        )
        
        if_out = await self._snmp_walk(
            snmp_engine, community, transport, context,
            self.OID_IF_HCOUT
        )
        
        if_status = await self._snmp_walk(
            snmp_engine, community, transport, context,
            self.OID_IF_STATUS
        )
        
        # Parse interfaces
        for idx, name in if_desc.items():
            iface_type = self._classify_interface(name)
            if iface_type is None:
                continue
            
            iface = InterfaceData(
                name=name,
                index=idx,
                interface_type=iface_type,
                ifHCInOctets=if_in.get(idx),
                ifHCOutOctets=if_out.get(idx),
                ifOperStatus=if_status.get(idx)
            )
            interfaces.append(iface)
        
        return interfaces
    
    async def _subprocess_bulkwalk(self, oid: str, timeout: int = 90, retries: int = 1) -> Dict[str, str]:
        """Run snmpbulkwalk via subprocess, return {suffix: value}."""
        import tempfile
        for attempt in range(retries + 1):
            result = {}
            tmp = None
            try:
                tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.snmp', delete=False)
                tmp.close()
                
                proc = await asyncio.create_subprocess_exec(
                    "snmpbulkwalk", "-v2c", "-c", self.config.community,
                    "-Cr100", "-t", "30", "-r", "0",
                    "-On",
                    self.config.ip, oid,
                    stdout=open(tmp.name, 'w'),
                    stderr=asyncio.subprocess.DEVNULL
                )
                
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                
                with open(tmp.name, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or "=" not in line:
                            continue
                        parts = line.split("=", 1)
                        if len(parts) != 2:
                            continue
                        oid_part = parts[0].strip().lstrip(".")
                        val_part = parts[1].strip()
                        if not oid_part.startswith(oid):
                            continue
                        suffix = oid_part[len(oid)+1:]
                        val = val_part.split(":", 1)[-1].strip().strip('"') if ":" in val_part else val_part.strip('"')
                        result[suffix] = val
                
                if result:
                    return result
            except Exception as e:
                logger.debug(f"subprocess bulkwalk failed for {oid}: {e}")
            finally:
                if tmp:
                    try:
                        import os
                        os.unlink(tmp.name)
                    except Exception:
                        pass
            
            if attempt < retries:
                await asyncio.sleep(2)
        
        return result
    
    async def _collect_interfaces_subprocess(self) -> List[InterfaceData]:
        interfaces = []
        try:
            desc_data = await self._subprocess_bulkwalk(self.OID_IF_DESC)
            if not desc_data:
                return interfaces
            
            in_data = await self._subprocess_bulkwalk(self.OID_IF_HCIN)
            out_data = await self._subprocess_bulkwalk(self.OID_IF_HCOUT)
            status_data = await self._subprocess_bulkwalk(self.OID_IF_STATUS)
            
            for idx, name in desc_data.items():
                iface_type = self._classify_interface(name)
                if iface_type is None:
                    continue
                
                iface = InterfaceData(
                    name=name,
                    index=idx,
                    interface_type=iface_type,
                    ifHCInOctets=self._parse_int(in_data.get(idx)),
                    ifHCOutOctets=self._parse_int(out_data.get(idx)),
                    ifOperStatus=self._parse_int(status_data.get(idx))
                )
                interfaces.append(iface)
        except Exception as e:
            logger.debug(f"Interface collection failed: {e}")
        
        return interfaces
    
    async def _collect_onus_subprocess(self) -> List[OnuData]:
        onus = []
        
        # Walk ONU status
        status_data = await self._subprocess_bulkwalk(self.OID_ONU_STATUS)
        
        if not status_data:
            return onus
        
        # Walk serial and model in parallel
        serial_data, model_data = await asyncio.gather(
            self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.5"),
            self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.6")
        )
        
        # Collect online indices
        online_indices = []
        for idx_dot, status_str in status_data.items():
            status_val = self._parse_int(status_str)
            if status_val == 3:
                online_indices.append(idx_dot)
        
        # Get RX/TX for online ONUs via subprocess snmpget (sequential batches)
        rxtx_data = {}
        if online_indices:
            await asyncio.sleep(5)
            rxtx_data = await self._get_rxtx_batch_subprocess(online_indices)
        
        # Build ONU list
        for idx_dot, status_str in status_data.items():
            status_val = self._parse_int(status_str)
            if status_val is None:
                continue
            
            parts = idx_dot.split(".")
            if len(parts) < 2:
                continue
            
            pon_id = parts[0]
            onu_id = parts[1]
            pon_port = f"GPON0/{pon_id}"
            onu_index = f"{pon_id}_{onu_id}"
            location = f"GPON0/{pon_id}:{onu_id}"
            
            serial = serial_data.get(idx_dot, f"ONU_{onu_index}")
            modelo = model_data.get(idx_dot, "unknown")
            
            rx_power = None
            tx_power = None
            if idx_dot in rxtx_data:
                rx_power = rxtx_data[idx_dot].get("rx")
                tx_power = rxtx_data[idx_dot].get("tx")
            
            onu = OnuData(
                pon_port=pon_port,
                onu_index=onu_index,
                location=location,
                description=f"ONU_{onu_index}",
                serial=str(serial).strip('"'),
                modelo=str(modelo).strip('"'),
                status=status_val,
                rx_power=rx_power,
                tx_power=tx_power
            )
            onus.append(onu)
        
        return onus

    async def _ensure_onu_meta(
        self,
        onu_status: Optional[Dict[str, str]],
        onu_serial: Optional[Dict[str, str]],
        onu_model: Optional[Dict[str, str]],
    ) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        """Completa los diccionarios de metadata ONU (status/serial/modelo)."""
        if onu_status is None or not onu_status:
            onu_status = await self._subprocess_bulkwalk(self.OID_ONU_STATUS)
            await asyncio.sleep(self.mac_walk_delay)
        if onu_serial is None or not onu_serial:
            onu_serial = await self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.5")
            await asyncio.sleep(self.mac_walk_delay)
        if onu_model is None or not onu_model:
            onu_model = await self._subprocess_bulkwalk(f"{self.OID_ONU_IDENT}.6")
        return onu_status, onu_serial, onu_model

    async def _capture_vsol_macs_async(
        self,
        onu_status: Optional[Dict[str, str]],
        onu_serial: Optional[Dict[str, str]],
        onu_model: Optional[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """FDB privada VSOL (MAC -> PON:ONU). Devuelve [] si no esta soportada."""
        macs = await self._subprocess_bulkwalk(self.OID_VSOL_MAC, timeout=90)
        if not macs:
            return []
        await asyncio.sleep(self.mac_walk_delay)
        ports = await self._subprocess_bulkwalk(self.OID_VSOL_PORT, timeout=90)
        if not ports:
            return []
        await asyncio.sleep(self.mac_walk_delay)
        vlans = await self._subprocess_bulkwalk(self.OID_VSOL_VLAN, timeout=90)
        return build_vsol_mac_metrics(
            macs, ports,
            olt_name=self.config.hostname,
            olt_ip=self.config.ip,
            nodo=self.config.nodo,
            vlans=vlans,
            onu_status=onu_status,
            onu_serial=onu_serial,
            onu_model=onu_model,
        )

    async def _capture_web_macs_async(
        self,
        onu_status: Optional[Dict[str, str]],
        onu_serial: Optional[Dict[str, str]],
        onu_model: Optional[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        """Respaldo: tabla MAC del panel web VSOL cuando SNMP no la expone."""
        try:
            from .olt_web_mac import fetch_web_macs, web_mac_enabled
        except Exception as e:
            logger.debug(f"{self.config.hostname}: web MAC fallback no disponible: {e}")
            return []
        if not web_mac_enabled():
            return []
        try:
            rows = await asyncio.to_thread(fetch_web_macs, self.config.ip)
        except Exception as e:
            logger.warning(f"{self.config.hostname}: web MAC fallback fallo: {e}")
            return []
        if not rows:
            return []
        macs = {str(i): r.get("mac") for i, r in enumerate(rows)}
        ports = {str(i): r.get("port") for i, r in enumerate(rows)}
        vlans = {
            str(i): r.get("vlan")
            for i, r in enumerate(rows) if r.get("vlan") is not None
        }
        return build_vsol_mac_metrics(
            macs, ports,
            olt_name=self.config.hostname,
            olt_ip=self.config.ip,
            nodo=self.config.nodo,
            vlans=vlans,
            onu_status=onu_status,
            onu_serial=onu_serial,
            onu_model=onu_model,
        )

    def _dedupe_mac_metrics(self, metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filtra onu_mac cuyo estado no cambio desde el ultimo ciclo (misma
        serie = tags + campos). El primer ciclo siempre emite.

        IMPORTANTE: se excluye onu_location de este filtro: el enricher barre
        esa medicion en una ventana corta (lookback_hours) con last(); si una
        serie estable deja de reescribirse sale de la ventana y la MAC se
        pierde del dashboard y de la ficha del cliente. onu_mac en cambio solo
        se consulta con last() por MAC en -168h, asi que el dedupe es seguro.
        """
        out = []
        for m in metrics:
            if m.get("measurement") != "onu_mac":
                out.append(m)
                continue
            tags = m.get("tags", {})
            fields = m.get("fields", {})
            key = "|".join(
                f"{k}={v}" for k, v in sorted(tags.items())
            ) + "||" + "|".join(
                f"{k}={v}" for k, v in sorted(fields.items())
            )
            if self._mac_seen.get(key) == m.get("measurement"):
                continue  # sin cambios: no saturar InfluxDB
            self._mac_seen[key] = m.get("measurement")
            out.append(m)
        return out

    async def _capture_macs_async(
        self,
        if_desc: Optional[Dict[str, str]] = None,
        onu_status: Optional[Dict[str, str]] = None,
        onu_serial: Optional[Dict[str, str]] = None,
        onu_model: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Reune FDB + metadata ONU y emite onu_mac/onu_location.

        Orden de fuentes: (1) FDB privada VSOL (atribuye MAC por ONU),
        (2) respaldo web del panel VSOL, (3) respaldo dot1d/dot1q (sin
        atribucion por ONU, colapsa todo al uplink).
        """
        errors: List[str] = []
        try:
            onu_status, onu_serial, onu_model = await self._ensure_onu_meta(
                onu_status, onu_serial, onu_model
            )

            def _deduped(m):
                return self._dedupe_mac_metrics(m)

            # 1) FDB privada VSOL: MAC -> 'PON5:ONU16'
            vsol = await self._capture_vsol_macs_async(onu_status, onu_serial, onu_model)
            if vsol:
                return {"metrics": _deduped(vsol), "errors": errors}

            # 2) Respaldo web (login al panel VSOL)
            web = await self._capture_web_macs_async(onu_status, onu_serial, onu_model)
            if web:
                return {"metrics": _deduped(web), "errors": errors}

            # 3) Respaldo dot1d/dot1q (dot1dTpFdbTable): sin atribucion por ONU
            if if_desc is None or not if_desc:
                if_desc = await self._subprocess_bulkwalk(self.OID_IF_DESC, timeout=30)
                await asyncio.sleep(self.mac_walk_delay)

            fdb_macs = await self._subprocess_bulkwalk(self.OID_FDB_MAC, timeout=90)
            if not fdb_macs:
                return {"metrics": [], "errors": [f"FDB sin datos en {self.config.hostname}"]}
            await asyncio.sleep(self.mac_walk_delay)

            fdb_ports = await self._subprocess_bulkwalk(self.OID_FDB_PORT, timeout=90)
            if not fdb_ports:
                return {"metrics": [], "errors": [f"FDB port sin datos en {self.config.hostname}"]}

            metrics = build_mac_metrics(
                fdb_macs, fdb_ports, if_desc,
                olt_name=self.config.hostname,
                olt_ip=self.config.ip,
                nodo=self.config.nodo,
                onu_status=onu_status,
                onu_serial=onu_serial,
                onu_model=onu_model,
            )
            if not metrics:
                errors.append(f"Sin MACs procesables en FDB de {self.config.hostname}")
            return {"metrics": self._dedupe_mac_metrics(metrics), "errors": errors}
        except Exception as e:
            logger.error(f"{self.config.hostname} MAC capture failed: {e}")
            return {"metrics": [], "errors": [f"MAC capture failed: {e}"]}
    
    async def _get_rxtx_batch_subprocess(
        self, indices: List[str]
    ) -> Dict[str, Dict]:
        if self._g2b_mode:
            return await self._get_rxtx_gentle_rotating(indices)

        rxtx = {}
        batch_size = self._batch_size
        inter_batch_delay = 0.5
        
        async def get_single(idx_dot: str):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "snmpget", "-v2c", "-c", self.config.community,
                    "-Ovq", "-t", "3", "-r", "0",
                    self.config.ip,
                    f"{self.OID_ONU_RX}.{idx_dot}",
                    f"{self.OID_ONU_TX}.{idx_dot}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                output = stdout.decode("utf-8", errors="ignore").strip()
                lines = output.split("\n")
                
                rx_val = None
                tx_val = None
                if len(lines) >= 1:
                    rx_val = self._parse_float(lines[0])
                if len(lines) >= 2:
                    tx_val = self._parse_float(lines[1])
                
                if rx_val is not None or tx_val is not None:
                    rxtx[idx_dot] = {"rx": rx_val, "tx": tx_val}
            except Exception:
                pass
        
        for i in range(0, len(indices), batch_size):
            batch = indices[i:i+batch_size]
            await asyncio.gather(*[get_single(idx) for idx in batch])
            if i + batch_size < len(indices):
                await asyncio.sleep(inter_batch_delay)
        
        return rxtx
    
    async def _get_rxtx_gentle_rotating(
        self, indices: List[str]
    ) -> Dict[str, Dict]:
        """Rx/Tx suave y rotativo para VSOL V1600G2B.

        El agente SNMP del G2B es frágil: una ráfaga de gets (16 en paralelo
        cada 0.5s, el patrón de G1/G0B) lo deja sin responder durante muchos
        minutos, por eso solo el primer PON llegaba a escribirse con power y el
        health-check de pasadas siguientes fallaba. Aquí se pide de a pocos y
        con pausa, arrancando cada pasada en un offset distinto: en ~4-5 pasadas
        los 16 PONs acumulan Rx/Tx (la ventana de 45 min del modal los muestra).
        """
        rxtx: Dict[str, Dict] = {}
        if not indices:
            return rxtx

        offset = self._g2b_rxtx_offset % len(indices)
        self._g2b_rxtx_offset = (self._g2b_rxtx_offset + 280) % 1000000
        ordered = indices[offset:] + indices[:offset]

        budget = 200.0
        batch_size = 2
        inter_batch_delay = 1.0
        start = time.time()

        async def get_single(idx_dot: str):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "snmpget", "-v2c", "-c", self.config.community,
                    "-Ovq", "-t", "2", "-r", "0",
                    self.config.ip,
                    f"{self.OID_ONU_RX}.{idx_dot}",
                    f"{self.OID_ONU_TX}.{idx_dot}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4)
                lines = stdout.decode("utf-8", errors="ignore").strip().split("\n")
                rx_val = self._parse_float(lines[0]) if lines else None
                tx_val = self._parse_float(lines[1]) if len(lines) >= 2 else None
                if rx_val is not None or tx_val is not None:
                    rxtx[idx_dot] = {"rx": rx_val, "tx": tx_val}
            except Exception:
                pass

        for i in range(0, len(ordered), batch_size):
            if time.time() - start > budget:
                logger.warning(
                    f"{self.config.hostname}: fase power G2B superó presupuesto "
                    f"({int(budget)}s) con {len(rxtx)}/{len(indices)} ONUs"
                )
                break
            batch = ordered[i:i + batch_size]
            await asyncio.gather(*[get_single(idx) for idx in batch])
            await asyncio.sleep(inter_batch_delay)

        if rxtx:
            logger.info(
                f"{self.config.hostname}: G2B power {len(rxtx)}/{len(indices)} "
                f"ONUs en {time.time()-start:.0f}s (rotación {offset})"
            )
        return rxtx
    
    async def _collect_onus(
        self, snmp_engine, community, transport, context
    ) -> List[OnuData]:
        onus = []
        
        # Walk ONU status, serial, and model in parallel
        onu_status, onu_serial, onu_model = await asyncio.gather(
            self._snmp_bulk_walk(
                snmp_engine, community, transport, context,
                self.OID_ONU_STATUS
            ),
            self._snmp_bulk_walk(
                snmp_engine, community, transport, context,
                f"{self.OID_ONU_IDENT}.5"
            ),
            self._snmp_bulk_walk(
                snmp_engine, community, transport, context,
                f"{self.OID_ONU_IDENT}.6"
            ),
            return_exceptions=True
        )
        
        if isinstance(onu_status, Exception):
            logger.warning(f"ONU status walk failed: {onu_status}")
            onu_status = {}
        if isinstance(onu_serial, Exception):
            onu_serial = {}
        if isinstance(onu_model, Exception):
            onu_model = {}
        
        # Collect RX/TX for online ONUs
        online_indices = []
        for idx_dot, status in onu_status.items():
            if status == 3:  # Online
                online_indices.append(idx_dot)
        
        # Get RX/TX in parallel batches
        rxtx_data = {}
        if online_indices:
            rxtx_data = await self._get_rxtx_batch(
                snmp_engine, community, transport, context,
                online_indices
            )
        
        # Build ONU list
        for idx_dot, status in onu_status.items():
            parts = idx_dot.split(".")
            if len(parts) < 2:
                continue
            
            pon_id = parts[0]
            onu_id = parts[1]
            pon_port = f"GPON0/{pon_id}"
            onu_index = f"{pon_id}_{onu_id}"
            location = f"GPON0/{pon_id}:{onu_id}"
            
            serial = onu_serial.get(idx_dot, f"ONU_{onu_index}")
            modelo = onu_model.get(idx_dot, "unknown")
            
            rx_power = None
            tx_power = None
            if idx_dot in rxtx_data:
                rx_power = rxtx_data[idx_dot].get("rx")
                tx_power = rxtx_data[idx_dot].get("tx")
            
            onu = OnuData(
                pon_port=pon_port,
                onu_index=onu_index,
                location=location,
                description=f"ONU_{onu_index}",
                serial=str(serial).strip('"'),
                modelo=str(modelo).strip('"'),
                status=status,
                rx_power=rx_power,
                tx_power=tx_power
            )
            onus.append(onu)
        
        return onus
    
    async def _get_rxtx_batch(
        self, snmp_engine, community, transport, context,
        indices: List[str]
    ) -> Dict[str, Dict]:
        rxtx = {}
        batch_size = self._batch_size
        
        async def get_single(idx_dot: str):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "snmpget", "-v2c", "-c", self.config.community,
                    "-Ovq", "-t", "5", "-r", "0",
                    self.config.ip,
                    f"{self.OID_ONU_RX}.{idx_dot}",
                    f"{self.OID_ONU_TX}.{idx_dot}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
                output = stdout.decode("utf-8", errors="ignore").strip()
                lines = output.split("\n")
                
                rx_val = None
                tx_val = None
                if len(lines) >= 1:
                    rx_val = self._parse_float(lines[0])
                if len(lines) >= 2:
                    tx_val = self._parse_float(lines[1])
                
                if rx_val is not None or tx_val is not None:
                    rxtx[idx_dot] = {"rx": rx_val, "tx": tx_val}
            except Exception:
                pass
        
        for i in range(0, len(indices), batch_size):
            batch = indices[i:i+batch_size]
            await asyncio.gather(*[get_single(idx) for idx in batch])
            if i + batch_size < len(indices):
                await asyncio.sleep(1)
        
        return rxtx
    
    async def _snmp_get(
        self, snmp_engine, community, transport, context,
        oid: str
    ) -> Any:
        try:
            from pysnmp.hlapi.v3arch.asyncio import get_cmd, ObjectType, ObjectIdentity
            
            error_indication, error_status, error_index, var_binds = await get_cmd(
                snmp_engine,
                community,
                transport,
                context,
                ObjectType(ObjectIdentity(oid))
            )
            
            if error_indication or error_status:
                return None
            
            if var_binds:
                return var_binds[0][1]
            return None
        except Exception as e:
            logger.debug(f"SNMP get failed for {oid}: {e}")
            return None
    
    async def _snmp_walk(
        self, snmp_engine, community, transport, context,
        oid: str
    ) -> Dict[str, Any]:
        result = {}
        max_iterations = 200
        iteration = 0
        try:
            from pysnmp.hlapi.v3arch.asyncio import walk_cmd, ObjectType, ObjectIdentity
            
            current_oid = oid
            while iteration < max_iterations:
                iteration += 1
                error_indication, error_status, error_index, var_binds = await walk_cmd(
                    snmp_engine,
                    community,
                    transport,
                    context,
                    ObjectType(ObjectIdentity(current_oid))
                )
                
                if error_indication or error_status:
                    break
                
                if not var_binds:
                    break
                
                end_of_mib = False
                for o, v in var_binds:
                    oid_str = str(o)
                    if not oid_str.startswith(oid):
                        end_of_mib = True
                        break
                    idx = oid_str[len(oid)+1:]  # full suffix: "1.5" for pon1.onu5
                    result[idx] = v
                    current_oid = oid_str
                
                if end_of_mib:
                    break
            
        except Exception as e:
            logger.debug(f"SNMP walk failed for {oid}: {e}")
        
        return result
    
    async def _snmp_bulk_walk(
        self, snmp_engine, community, transport, context,
        oid: str
    ) -> Dict[str, Any]:
        result = {}
        max_iterations = 200
        iteration = 0
        try:
            from pysnmp.hlapi.v3arch.asyncio import bulk_cmd, ObjectType, ObjectIdentity
            
            current_oid = oid
            while iteration < max_iterations:
                iteration += 1
                error_indication, error_status, error_index, var_binds = await bulk_cmd(
                    snmp_engine,
                    community,
                    transport,
                    context,
                    0,  # non-repeaters
                    50,  # max-repetitions
                    ObjectType(ObjectIdentity(current_oid))
                )
                
                if error_indication or error_status:
                    break
                
                if not var_binds:
                    break
                
                end_of_mib = False
                for o, v in var_binds:
                    oid_str = str(o)
                    if not oid_str.startswith(oid):
                        end_of_mib = True
                        break
                    idx = oid_str[len(oid)+1:]  # full suffix: "1.5" for pon1.onu5
                    result[idx] = v
                    current_oid = oid_str
                
                if end_of_mib:
                    break
            
        except Exception as e:
            logger.debug(f"SNMP bulk walk failed for {oid}: {e}")
        
        return result
    
    def _classify_interface(self, name: str) -> Optional[str]:
        return classify_interface_name(name)
    
    def _parse_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            val_str = str(value).strip().strip('"')
            val_str = re.sub(r'[^0-9.\-]', '', val_str)
            if val_str:
                return float(val_str)
        except (ValueError, TypeError):
            pass
        return None
    
    def _parse_int(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            val_str = str(value).strip().strip('"')
            val_str = re.sub(r'[^0-9\-]', '', val_str)
            if val_str:
                return int(val_str)
        except (ValueError, TypeError):
            pass
        return None

    def _compute_rates(
        self, idx: str, in_oct: Optional[int], out_oct: Optional[int], now: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """Deriva rx/tx en bps entre el ciclo actual y el anterior.

        La clave usa la IP de la OLT + el ifIndex; el ifIndex puede ser un
        entero de 32 bits (p. ej. 15.7M en SISAL), por lo que se conserva
        como string. Se aceptan deltas de hasta _RATE_MAX_DELTA (1h) porque
        la cadencia de escritura de las OLTs es de ~15-20 min por interfaz.
        """
        key = (self.config.ip, str(idx))
        rx_bps = tx_bps = None
        prev = _RATE_CACHE.get(key)
        if prev is not None and in_oct is not None and out_oct is not None:
            p_ts, p_in, p_out = prev
            dt = now - p_ts
            if 0 < dt <= _RATE_MAX_DELTA:
                d_in = in_oct - p_in
                d_out = out_oct - p_out
                if d_in >= 0:
                    rx_bps = d_in * 8 / dt
                if d_out >= 0:
                    tx_bps = d_out * 8 / dt
        if in_oct is not None and out_oct is not None:
            _RATE_CACHE[key] = (now, in_oct, out_oct)
        return rx_bps, tx_bps
