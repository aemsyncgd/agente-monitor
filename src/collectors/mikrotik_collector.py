# src/collectors/mikrotik_collector.py
import re
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from .base import BaseCollector, CollectorResult
from .mikrotik_templates import (
    resolve_mikrotik_template,
    HR_PROCESSOR_LOAD,
    HR_STORAGE_DESCR,
    HR_STORAGE_UNITS,
    HR_STORAGE_TOTAL,
    HR_STORAGE_USED,
    MTXR_SENSOR_NAME,
    MTXR_SENSOR_VALUE,
    MTXR_CPU_TEMP,
    MTXR_DEVICE_TEMP,
)

logger = logging.getLogger(__name__)

# Cache persistente de contadores para derivar tasas rx/tx (sobrevive al
# recreado de collectors por recarga de config).
_RATE_CACHE: Dict[Tuple[str, str], Tuple[float, int, int]] = {}


@dataclass
class MikroTikConfig:
    ip: str
    community: str
    hostname: str
    modelo: str = ""
    username: str = "admin"
    password: str = ""
    api_port: int = 8728
    use_api: bool = False  # True = use RouterOS API, False = use SNMP only


class MikroTikCollector(BaseCollector):
    # RouterOS MIB OIDs (classic, supported by older RouterOS versions)
    OID_CPU_LOAD = "1.3.6.1.4.1.14988.1.1.3.1.0"
    OID_TOTAL_MEM = "1.3.6.1.4.1.14988.1.1.3.2.0"
    OID_FREE_MEM = "1.3.6.1.4.1.14988.1.1.3.3.0"
    OID_HDD_SPACE = "1.3.6.1.4.1.14988.1.1.3.4.0"

    # System info
    OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
    OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
    OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"

    # Hardware uptime
    OID_HR_SYSTEM_UPTIME = "1.3.6.1.2.1.25.1.3.0"

    # RouterOS version (mtxrSystemGroup) - presente en ROS 6 y 7
    OID_MTXR_VERSION = "1.3.6.1.4.1.14988.1.1.7.7.0"

    # Firmware / identity / model / serial (classic)
    OID_FIRMWARE = "1.3.6.1.4.1.14988.1.1.3.1.0"
    OID_IDENTITY = "1.3.6.1.4.1.14988.1.1.3.5.0"
    OID_MODEL = "1.3.6.1.4.1.14988.1.1.3.3.1.6.1"
    OID_SERIAL = "1.3.6.1.4.1.14988.1.1.3.3.1.5.1"

    # Temperature (classic RouterOS MIB)
    OID_CPU_TEMP = "1.3.6.1.4.1.14988.1.1.10.1.1.2"
    OID_DEVICE_TEMP = "1.3.6.1.4.1.14988.1.1.10.1.2.1.3"

    # HOST-RESOURCES fallbacks (always available on RouterOS)
    OID_HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
    OID_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
    OID_HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
    OID_HR_STORAGE_TOTAL = "1.3.6.1.2.1.25.2.3.1.5"
    OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"

    # RouterOS v7 sensor table (newer devices, under mtxrSystemGroup v3)
    OID_MTXR_SENSOR_NAME = "1.3.6.1.4.1.14988.1.1.3.100.1.2"
    OID_MTXR_SENSOR_VALUE = "1.3.6.1.4.1.14988.1.1.3.100.1.3"

    # IF-MIB OIDs (ifTable / ifXTable). Se camina con snmpbulkwalk (GETBULK):
    # con pysnmp el GETNEXT es serial (~1 varbind/response), lo que hace un
    # walk de una tabla con miles de interfaces ~10x mas lento.
    OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
    OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
    OID_IF_MTU = "1.3.6.1.2.1.2.2.1.4"
    OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
    OID_IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"
    OID_IF_STATUS = "1.3.6.1.2.1.2.2.1.8"
    OID_IF_LASTCHANGE = "1.3.6.1.2.1.2.2.1.9"
    OID_IF_IN_UCAST = "1.3.6.1.2.1.2.2.1.11"
    OID_IF_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"
    OID_IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
    OID_IF_OUT_UCAST = "1.3.6.1.2.1.2.2.1.17"
    OID_IF_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"
    OID_IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"
    OID_IF_HCIN = "1.3.6.1.2.1.31.1.1.1.6"
    OID_IF_HCOUT = "1.3.6.1.2.1.31.1.1.1.10"

    # ifTable / ifXTable bases para snmpbulkwalk de la tabla completa
    OID_IF_TABLE = "1.3.6.1.2.1.2.2.1"
    OID_IF_X_TABLE = "1.3.6.1.2.1.31.1.1.1"

    # ifTable columns (para parsear el walk de tabla completa)
    IF_COL_DESCR = 2
    IF_COL_TYPE = 3
    IF_COL_MTU = 4
    IF_COL_SPEED = 5
    IF_COL_ADMIN = 7
    IF_COL_OPER = 8
    IF_COL_LASTCHANGE = 9
    IF_COL_IN_ERRORS = 14
    IF_COL_OUT_ERRORS = 20
    IF_COL_IN_DISCARDS = 13
    IF_COL_OUT_DISCARDS = 19
    IF_COL_IN_UCAST_PKTS = 11
    IF_COL_OUT_UCAST_PKTS = 17

    # ifXTable columns (contadores de 64 bits)
    IFX_COL_HC_IN_OCTETS = 6
    IFX_COL_HC_OUT_OCTETS = 10

    def __init__(self, config: MikroTikConfig, timeout: int = 30):
        super().__init__(f"mikrotik-{config.hostname}")
        self.config = config
        self.timeout = timeout
        self.template = resolve_mikrotik_template(config.modelo)
        import shutil
        self._use_bulkwalk = shutil.which("snmpbulkwalk") is not None
        patterns = self.template.get("iface_exclude_patterns", [])
        if patterns:
            self._iface_exclude = re.compile(
                "|".join(f"(?:{p})" for p in patterns), re.IGNORECASE
            )
        else:
            self._iface_exclude = None

    def _is_skipped_interface(self, name: str) -> bool:
        """Interfaces a omitir: dinamicas segun plantilla + virtuales clasicas."""
        low = name.lower()
        if low in ("loopback", "dummy", "lte", "wlan"):
            return True
        if self._iface_exclude is not None and self._iface_exclude.search(low):
            return True
        return False

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

    async def _collect_async(self) -> Dict[str, Any]:
        metrics = []
        errors = []

        try:
            from pysnmp.hlapi.v3arch.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget,
                ContextData, ObjectType, ObjectIdentity,
                get_cmd, walk_cmd
            )
        except ImportError:
            errors.append("pysnmp not installed")
            return {"metrics": metrics, "errors": errors}

        snmp_engine = SnmpEngine()
        community = CommunityData(self.config.community)
        transport = await UdpTransportTarget.create(
            (self.config.ip, 161),
            timeout=10,
            retries=1
        )
        context = ContextData()

        # 1. Health check
        sys_name = await self._snmp_get(
            snmp_engine, community, transport, context,
            self.OID_SYS_NAME
        )
        if sys_name is None:
            errors.append(f"Router {self.config.ip} not responding")
            snmp_engine.close_dispatcher()
            return {"metrics": metrics, "errors": errors}

        # 2. System metrics (CPU, Memory, temps, uptime, identity)
        system_data = await self._collect_system(
            snmp_engine, community, transport, context
        )

        if system_data:
            total_mem = system_data.get("total_memory", 0)
            free_mem = system_data.get("free_memory", 0)
            used_mem = total_mem - free_mem if total_mem > 0 else 0
            mem_percent = (used_mem / total_mem * 100) if total_mem > 0 else 0

            system_metric = {
                "measurement": "mikrotik_system",
                "tags": {
                    "device_name": self.config.hostname,
                    "device_ip": self.config.ip,
                    "device_type": "mikrotik"
                },
                "fields": {
                    "cpu_load": system_data.get("cpu_load", 0),
                    "total_memory": total_mem,
                    "free_memory": free_mem,
                    "used_memory": used_mem,
                    "memory_percent": round(mem_percent, 2),
                    "hdd_space": system_data.get("hdd_space", 0),
                    "sys_descr": system_data.get("sys_descr", ""),
                    "uptime_hw": system_data.get("uptime_hw", 0),
                    "uptime_net": system_data.get("uptime_net", 0),
                    "firmware": system_data.get("firmware", ""),
                    "model": system_data.get("model", ""),
                    "serial": system_data.get("serial", ""),
                    "cpu_source": system_data.get("cpu_source", "none"),
                    "mem_source": system_data.get("mem_source", "none")
                }
            }
            metrics.append(system_metric)

        # 3. CPU per-core metrics
        cpu_cores = await self._collect_cpu_cores(
            snmp_engine, community, transport, context
        )

        for core_idx, load in cpu_cores.items():
            metrics.append({
                "measurement": "mikrotik_cpu_core",
                "tags": {
                    "device_name": self.config.hostname,
                    "device_ip": self.config.ip,
                    "core_index": core_idx
                },
                "fields": {
                    "utilization": load
                }
            })

        # 4. Temperature metrics
        temperatures = await self._collect_temperatures(
            snmp_engine, community, transport, context
        )

        for temp in temperatures:
            metrics.append({
                "measurement": "mikrotik_temperature",
                "tags": {
                    "device_name": self.config.hostname,
                    "device_ip": self.config.ip,
                    "sensor_name": temp.get("name", ""),
                    "sensor_type": temp.get("type", "")
                },
                "fields": {
                    "temperature_c": temp.get("value", 0.0)
                }
            })

        # 5. Interface metrics (including errors and discards)
        iface_result = await self._collect_interfaces(
            snmp_engine, community, transport, context
        )
        interfaces = iface_result.get("interfaces", [])

        # Conteo de sesiones PPPoE (activas vs totales) -> sistema, para el panel
        if iface_result.get("pppoe_total") and system_data:
            system_metric["fields"]["pppoe_online"] = iface_result.get("pppoe_online", 0)
            system_metric["fields"]["pppoe_total"] = iface_result.get("pppoe_total", 0)

        for iface in interfaces:
            fields = {}
            if iface.get("ifHCInOctets") is not None:
                fields["ifHCInOctets"] = iface["ifHCInOctets"]
            if iface.get("ifHCOutOctets") is not None:
                fields["ifHCOutOctets"] = iface["ifHCOutOctets"]
            if iface.get("ifOperStatus") is not None:
                fields["ifOperStatus"] = iface["ifOperStatus"]
            if iface.get("ifAdminStatus") is not None:
                fields["ifAdminStatus"] = iface["ifAdminStatus"]
            if iface.get("ifType") is not None:
                fields["ifType"] = iface["ifType"]
            if iface.get("ifMTU") is not None:
                fields["ifMTU"] = iface["ifMTU"]
            if iface.get("ifSpeed") is not None:
                fields["ifSpeed"] = iface["ifSpeed"]
            if iface.get("ifLastChange") is not None:
                fields["ifLastChange"] = iface["ifLastChange"]
            if iface.get("ifInErrors") is not None:
                fields["ifInErrors"] = iface["ifInErrors"]
            if iface.get("ifOutErrors") is not None:
                fields["ifOutErrors"] = iface["ifOutErrors"]
            if iface.get("ifInDiscards") is not None:
                fields["ifInDiscards"] = iface["ifInDiscards"]
            if iface.get("ifOutDiscards") is not None:
                fields["ifOutDiscards"] = iface["ifOutDiscards"]
            if iface.get("ifInUcastPkts") is not None:
                fields["ifInUcastPkts"] = iface["ifInUcastPkts"]
            if iface.get("ifOutUcastPkts") is not None:
                fields["ifOutUcastPkts"] = iface["ifOutUcastPkts"]
            if iface.get("rx_bps") is not None:
                fields["rx_bps"] = iface["rx_bps"]
            if iface.get("tx_bps") is not None:
                fields["tx_bps"] = iface["tx_bps"]

            if fields:
                metrics.append({
                    "measurement": "mikrotik_interface",
                    "tags": {
                        "device_name": self.config.hostname,
                        "device_ip": self.config.ip,
                        "interface_name": iface.get("name", ""),
                        "interface_index": iface.get("index", "")
                    },
                    "fields": fields
                })

        snmp_engine.close_dispatcher()

        return {"metrics": metrics, "errors": errors}

    async def _collect_system(
        self, snmp_engine, community, transport, context
    ) -> Optional[Dict[str, Any]]:
        # 1. Get basic system info (always works)
        basic = await asyncio.gather(
            self._snmp_get(snmp_engine, community, transport, context, self.OID_HDD_SPACE),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_DESCR),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SYS_UPTIME),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_HR_SYSTEM_UPTIME),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_MODEL),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_SERIAL),
            self._snmp_get(snmp_engine, community, transport, context, self.OID_MTXR_VERSION),
            return_exceptions=True
        )

        hdd = self._parse_int(basic[0])
        sys_descr = self._parse_str(basic[1])
        uptime_net = self._parse_ticks(basic[2])
        uptime_hw = self._parse_ticks(basic[3])
        model = self._parse_str(basic[4])
        serial = self._parse_str(basic[5])
        firmware = self._parse_str(basic[6]) or ""

        # 2. CPU via plantilla (matriz de estrategias en orden de prioridad)
        cpu, cpu_source = await self._resolve_cpu(
            snmp_engine, community, transport, context
        )

        # 3. Memoria via plantilla (normalizada a KB para el frontend)
        total_mem_kb, free_mem_kb, mem_source = await self._resolve_memory(
            snmp_engine, community, transport, context
        )

        # 4. Parse model from sysDescr if not available via OIDs
        if not model and sys_descr:
            # "RouterOS CCR2116-12G-4S+ 7.23.1 (stable)" -> "CCR2116-12G-4S+"
            m = re.search(r'RouterOS\s+(\S+)', sys_descr)
            if m:
                model = m.group(1)

        # 5. Firmware from mtxrSystemGroup version OID, fallback sysDescr
        if not firmware and sys_descr:
            m = re.search(r'(\d+\.\d+(?:\.\d+)?)\s*\(', sys_descr)
            if m:
                firmware = m.group(1)

        # Always return system data (even with partial data)
        return {
            "cpu_load": cpu or 0,
            "total_memory": total_mem_kb or 0,
            "free_memory": free_mem_kb or 0,
            "hdd_space": hdd or 0,
            "sys_descr": sys_descr or "",
            "uptime_net": uptime_net or 0,
            "uptime_hw": uptime_hw or 0,
            "firmware": firmware or "",
            "model": model or "",
            "serial": serial or "",
            "cpu_source": cpu_source or "none",
            "mem_source": mem_source or "none"
        }

    async def _resolve_cpu(
        self, snmp_engine, community, transport, context
    ) -> Tuple[Optional[int], str]:
        """Resuelve CPU siguiendo la matriz de estrategias de la plantilla."""
        for strategy in self.template.get("cpu", []):
            kind = strategy.get("strategy")
            if kind == "mtxr_get":
                val = await self._snmp_get(
                    snmp_engine, community, transport, context, strategy["oid"]
                )
                parsed = self._parse_int(val)
                if parsed is not None:
                    return parsed, "mtxr_get"
            elif kind == "hr_avg":
                cores = await self._snmp_walk(
                    snmp_engine, community, transport, context, HR_PROCESSOR_LOAD
                )
                loads = [self._parse_int(v) for v in cores.values()]
                loads = [l for l in loads if l is not None]
                if loads:
                    return sum(loads) // len(loads), "hr_avg"
        return None, "none"

    async def _resolve_memory(
        self, snmp_engine, community, transport, context
    ) -> Tuple[Optional[int], Optional[int], str]:
        """Resuelve memoria (total, libre en KB) siguiendo la plantilla."""
        for strategy in self.template.get("memory", []):
            kind = strategy.get("strategy")
            if kind == "mtxr_get":
                t = await self._snmp_get(
                    snmp_engine, community, transport, context, strategy["oid_total"]
                )
                f = await self._snmp_get(
                    snmp_engine, community, transport, context, strategy["oid_free"]
                )
                t = self._parse_int(t)
                f = self._parse_int(f)
                if t is not None:
                    # RouterOS reporta bytes -> normalizar a KB
                    total_kb = max(t // 1024, 1)
                    free_kb = (f // 1024) if f is not None else None
                    return total_kb, free_kb, "mtxr_get"
            elif kind == "hr_storage":
                descr = await self._snmp_walk(
                    snmp_engine, community, transport, context, HR_STORAGE_DESCR
                )
                total = await self._snmp_walk(
                    snmp_engine, community, transport, context, HR_STORAGE_TOTAL
                )
                used = await self._snmp_walk(
                    snmp_engine, community, transport, context, HR_STORAGE_USED
                )
                units = await self._snmp_walk(
                    snmp_engine, community, transport, context, HR_STORAGE_UNITS
                )
                main_idx = None
                for idx, d in descr.items():
                    if "main memory" in str(d).lower():
                        main_idx = idx
                        break
                if main_idx is None:
                    for idx, v in total.items():
                        if self._parse_int(v):
                            main_idx = idx
                            break
                if main_idx is not None:
                    t = self._parse_int(total.get(main_idx))
                    u = self._parse_int(units.get(main_idx)) or 1024
                    u_val = self._parse_int(used.get(main_idx))
                    if t and t > 0:
                        total_kb = (t * u) // 1024 or 1
                        free_kb = None
                        if u_val is not None and t >= u_val:
                            free_kb = ((t - u_val) * u) // 1024
                        return total_kb, free_kb, "hr_storage"
        return None, None, "none"

    async def _collect_cpu_cores(
        self, snmp_engine, community, transport, context
    ) -> Dict[str, int]:
        cores = {}
        try:
            walk_result = await self._snmp_walk(
                snmp_engine, community, transport, context,
                self.OID_HR_PROCESSOR_LOAD
            )
            for idx, val in walk_result.items():
                parsed = self._parse_int(val)
                if parsed is not None:
                    cores[idx] = parsed
        except Exception as e:
            logger.debug(f"CPU cores walk failed for {self.config.hostname}: {e}")
        return cores

    async def _collect_temperatures(
        self, snmp_engine, community, transport, context
    ) -> List[Dict[str, Any]]:
        temperatures = []

        for strategy in self.template.get("temperature", []):
            kind = strategy.get("strategy")
            if kind == "mtxr_health":
                names = await self._snmp_walk(
                    snmp_engine, community, transport, context, MTXR_SENSOR_NAME
                )
                values = await self._snmp_walk(
                    snmp_engine, community, transport, context, MTXR_SENSOR_VALUE
                )
                for idx, v in names.items():
                    name = str(v).strip().strip('"')
                    if "temp" not in name.lower():
                        continue
                    parsed = self._parse_float(values.get(idx))
                    if parsed is None:
                        continue
                    temperatures.append({
                        "name": name,
                        "type": "cpu" if "cpu" in name.lower() else "device",
                        "value": parsed
                    })
            elif kind == "mtxr_legacy":
                cpu_temps = await self._snmp_walk(
                    snmp_engine, community, transport, context, MTXR_CPU_TEMP
                )
                for idx, val in cpu_temps.items():
                    parsed = self._parse_float(val)
                    if parsed is not None:
                        temperatures.append({
                            "name": f"cpu-{idx}",
                            "type": "cpu",
                            "value": parsed
                        })

                device_temps = await self._snmp_walk(
                    snmp_engine, community, transport, context, MTXR_DEVICE_TEMP
                )
                for idx, val in device_temps.items():
                    parsed = self._parse_float(val)
                    if parsed is not None:
                        temperatures.append({
                            "name": f"sensor-{idx}",
                            "type": "device",
                            "value": parsed
                        })

            if temperatures:
                break

        return temperatures

    async def _collect_interfaces(
        self, snmp_engine, community, transport, context
    ) -> Dict[str, Any]:
        if not self._use_bulkwalk:
            return await self._collect_interfaces_snmp(
                snmp_engine, community, transport, context
            )

        # GETBULK via snmpbulkwalk (subprocess): ~10x mas rapido que los walks
        # de pysnmp (GETNEXT serial) en equipos con miles de interfaces.
        if_rows = await self._subprocess_walk_table(self.OID_IF_TABLE)
        x_rows = await self._subprocess_walk_table(self.OID_IF_X_TABLE)

        if not self._table_has_descr(if_rows):
            # Walk truncado o vacio (rate-limit del RouterOS): reintenta una vez
            # y si sigue fallando omite el ciclo en vez del fallback lento.
            logger.warning(
                "%s: ifTable truncado (%d filas), reintentando...",
                self.config.hostname, len(if_rows),
            )
            await asyncio.sleep(3)
            if_rows = await self._subprocess_walk_table(self.OID_IF_TABLE)
            x_rows = await self._subprocess_walk_table(self.OID_IF_X_TABLE)
            if not self._table_has_descr(if_rows):
                logger.warning(
                    "%s: ifTable sigue truncado, omitiendo interfaces este ciclo",
                    self.config.hostname,
                )
                return {"interfaces": [], "pppoe_online": 0, "pppoe_total": 0}

        now = time.time()
        interfaces = []
        pppoe_online, pppoe_total = 0, 0
        for idx, row in if_rows.items():
            name_val = row.get(self.IF_COL_DESCR)
            if name_val is None:
                continue
            name_str = str(name_val).strip().strip('"')

            # Conteo de sesiones PPPoE (activas = operStatus up), util para
            # comparar conectados vs clientes activos. Se cuenta sobre la tabla
            # completa ANTES de filtrar las dinamicas de la salida.
            if "pppoe" in name_str.lower():
                pppoe_total += 1
                if self._to_native(row.get(self.IF_COL_OPER)) == 1:
                    pppoe_online += 1

            # Skip loopback, virtuales y dinamicas segun plantilla
            if self._is_skipped_interface(name_str):
                continue

            x_row = x_rows.get(idx, {})
            in_oct = self._to_native(x_row.get(self.IFX_COL_HC_IN_OCTETS))
            out_oct = self._to_native(x_row.get(self.IFX_COL_HC_OUT_OCTETS))

            # Tasas rx/tx (bps) derivadas de contadores 64 bits entre ciclos
            rx_bps, tx_bps = self._compute_rates(idx, in_oct, out_oct, now)

            interfaces.append({
                "name": name_str,
                "index": idx,
                "ifType": self._to_native(row.get(self.IF_COL_TYPE)),
                "ifMTU": self._to_native(row.get(self.IF_COL_MTU)),
                "ifSpeed": self._to_native(row.get(self.IF_COL_SPEED)),
                "ifAdminStatus": self._to_native(row.get(self.IF_COL_ADMIN)),
                "ifOperStatus": self._to_native(row.get(self.IF_COL_OPER)),
                "ifLastChange": self._parse_ticks(row.get(self.IF_COL_LASTCHANGE)),
                "ifHCInOctets": in_oct,
                "ifHCOutOctets": out_oct,
                "ifInErrors": self._to_native(row.get(self.IF_COL_IN_ERRORS)),
                "ifOutErrors": self._to_native(row.get(self.IF_COL_OUT_ERRORS)),
                "ifInDiscards": self._to_native(row.get(self.IF_COL_IN_DISCARDS)),
                "ifOutDiscards": self._to_native(row.get(self.IF_COL_OUT_DISCARDS)),
                "ifInUcastPkts": self._to_native(row.get(self.IF_COL_IN_UCAST_PKTS)),
                "ifOutUcastPkts": self._to_native(row.get(self.IF_COL_OUT_UCAST_PKTS)),
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
            })

        return {
            "interfaces": interfaces,
            "pppoe_online": pppoe_online,
            "pppoe_total": pppoe_total,
        }

    @staticmethod
    def _table_has_descr(rows: Dict[str, Dict[int, str]]) -> bool:
        """True si el walk de ifTable trajo la columna de nombres completa."""
        if not rows:
            return False
        total = len(rows)
        with_descr = sum(1 for row in rows.values() if row.get(2) is not None)
        return with_descr >= total * 0.9

    async def _collect_interfaces_snmp(
        self, snmp_engine, community, transport, context
    ) -> Dict[str, Any]:
        """Fallback: walks por columna con pysnmp (lento en tablas grandes)."""
        interfaces = []
        descr_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_DESCR)
        type_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_TYPE)
        mtu_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_MTU)
        speed_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_SPEED)
        admin_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_ADMIN)
        oper_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_STATUS)
        last_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_LASTCHANGE)
        in_ucast_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_IN_UCAST)
        out_ucast_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_OUT_UCAST)
        in_disc_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_IN_DISCARDS)
        out_disc_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_OUT_DISCARDS)
        in_err_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_IN_ERRORS)
        out_err_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_OUT_ERRORS)
        hcin_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_HCIN)
        hcout_w = await self._snmp_walk(
            snmp_engine, community, transport, context, self.OID_IF_HCOUT)

        now = time.time()
        pppoe_online, pppoe_total = 0, 0
        for idx, name_val in descr_w.items():
            name_str = str(name_val).strip().strip('"')

            if "pppoe" in name_str.lower():
                pppoe_total += 1
                if self._to_native(oper_w.get(idx)) == 1:
                    pppoe_online += 1

            # Skip loopback, virtuales y dinamicas segun plantilla
            if self._is_skipped_interface(name_str):
                continue

            in_oct = self._to_native(hcin_w.get(idx))
            out_oct = self._to_native(hcout_w.get(idx))

            # Tasas rx/tx (bps) derivadas de contadores 64 bits entre ciclos
            rx_bps, tx_bps = self._compute_rates(idx, in_oct, out_oct, now)

            interfaces.append({
                "name": name_str,
                "index": idx,
                "ifType": self._to_native(type_w.get(idx)),
                "ifMTU": self._to_native(mtu_w.get(idx)),
                "ifSpeed": self._to_native(speed_w.get(idx)),
                "ifAdminStatus": self._to_native(admin_w.get(idx)),
                "ifOperStatus": self._to_native(oper_w.get(idx)),
                "ifLastChange": self._parse_ticks(last_w.get(idx)),
                "ifHCInOctets": in_oct,
                "ifHCOutOctets": out_oct,
                "ifInErrors": self._to_native(in_err_w.get(idx)),
                "ifOutErrors": self._to_native(out_err_w.get(idx)),
                "ifInDiscards": self._to_native(in_disc_w.get(idx)),
                "ifOutDiscards": self._to_native(out_disc_w.get(idx)),
                "ifInUcastPkts": self._to_native(in_ucast_w.get(idx)),
                "ifOutUcastPkts": self._to_native(out_ucast_w.get(idx)),
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
            })

        return {
            "interfaces": interfaces,
            "pppoe_online": pppoe_online,
            "pppoe_total": pppoe_total,
        }

    async def _subprocess_walk_table(
        self, base_oid: str, timeout: int = 90
    ) -> Dict[str, Dict[int, str]]:
        """snmpbulkwalk de una tabla completa -> {indice: {columna: valor}}.

        El valor se conserva como string crudo; se normaliza luego con
        _to_native/_parse_ticks.
        """
        rows: Dict[str, Dict[int, str]] = {}
        import tempfile
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.snmp', delete=False)
            tmp.close()

            proc = await asyncio.create_subprocess_exec(
                "snmpbulkwalk", "-v2c", "-c", self.config.community,
                "-Cr50", "-t", "30", "-r", "0",
                "-On",
                self.config.ip, base_oid,
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
                    oid_part, val_part = line.split("=", 1)
                    oid_str = oid_part.strip().lstrip(".")
                    if not oid_str.startswith(base_oid):
                        continue
                    suffix = oid_str[len(base_oid):].lstrip(".")
                    parts = suffix.split(".")
                    if len(parts) < 2:
                        continue
                    try:
                        col = int(parts[0])
                    except ValueError:
                        continue
                    idx = ".".join(parts[1:])
                    val = val_part.strip()
                    if ":" in val:
                        val = val.split(":", 1)[1].strip().strip('"')
                    else:
                        val = val.strip('"')
                    rows.setdefault(idx, {})[col] = val

        except Exception as e:
            logger.debug(f"snmpbulkwalk failed for {base_oid}: {e}")
        finally:
            if tmp is not None:
                try:
                    import os
                    os.unlink(tmp.name)
                except OSError:
                    pass

        return rows

    def _compute_rates(
        self, idx: str, in_oct: Optional[int], out_oct: Optional[int], now: float
    ) -> Tuple[Optional[int], Optional[int]]:
        """Deriva rx/tx en bps entre el ciclo actual y el anterior."""
        key = (self.config.hostname, str(idx))
        rx_bps = tx_bps = None
        prev = _RATE_CACHE.get(key)
        if prev is not None and in_oct is not None and out_oct is not None:
            p_ts, p_in, p_out = prev
            dt = now - p_ts
            if 0 < dt <= 900:
                d_in = in_oct - p_in
                d_out = out_oct - p_out
                if d_in >= 0:
                    rx_bps = int(d_in * 8 / dt)
                if d_out >= 0:
                    tx_bps = int(d_out * 8 / dt)
        if in_oct is not None and out_oct is not None:
            _RATE_CACHE[key] = (now, in_oct, out_oct)
        return rx_bps, tx_bps

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
        try:
            from pysnmp.hlapi.v3arch.asyncio import walk_cmd, ObjectType, ObjectIdentity

            async for (error_indication, error_status, error_index, var_binds) in walk_cmd(
                snmp_engine,
                community,
                transport,
                context,
                ObjectType(ObjectIdentity(oid))
            ):
                if error_indication or error_status:
                    break

                if not var_binds:
                    break

                for o, v in var_binds:
                    oid_str = str(o)
                    if not oid_str.startswith(oid):
                        return result
                    idx = oid_str.rsplit(".", 1)[-1]
                    result[idx] = v

        except Exception as e:
            logger.debug(f"SNMP walk failed for {oid}: {e}")

        return result

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

    def _parse_str(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            val_str = str(value).strip().strip('"')
            return val_str if val_str else None
        except (ValueError, TypeError):
            pass
        return None

    def _to_native(self, value: Any) -> Any:
        """Convert pysnmp types (Counter64, Counter32, Gauge32, etc.) to native Python."""
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
        try:
            return str(value).strip().strip('"')
        except:
            pass
        return None

    def _parse_ticks(self, value: Any) -> Optional[int]:
        """Parse timeticks (hundredths of a second) to integer."""
        if value is None:
            return None
        try:
            val_str = str(value).strip().strip('"')
            # snmpbulkwalk: "Timeticks: (654) 0:00:06.54" -> "(654)" = ticks crudos
            m = re.search(r'\((\d+)\)', val_str)
            if m:
                return int(m.group(1)) // 100
            # Timeticks format: "123456789" or "123:45:67:89.00"
            if ':' in val_str:
                # Convert HH:MM:SS.ss format to seconds
                parts = val_str.split(':')
                if len(parts) == 4:
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = int(parts[2])
                    hundredths = int(parts[3].split('.')[0])
                    return hours * 3600 + minutes * 60 + seconds
            else:
                # Raw timeticks (hundredths of second) -> seconds
                ticks = int(re.sub(r'[^0-9]', '', val_str))
                return ticks // 100
        except (ValueError, TypeError):
            pass
        return None
