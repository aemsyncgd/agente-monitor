# src/api/snmp_live.py
"""Lectura SNMP bajo demanda para trafico de interfaces en TIEMPO REAL.

El tiempo real NO se persiste: cada poll hace 2 SNMP GETs y deriva la tasa con
una caché en memoria propia. Solo se usa para el modal de trafico del dashboard.
"""
import logging
import threading
import time
from typing import Dict, Optional, Tuple

from ..config_manager import get_config_manager

logger = logging.getLogger(__name__)

# Serializa las lecturas (los polls cada 5s corren en threads vía to_thread):
# evita apilar walks lentos cuando falta interface_index.
_READ_LOCK = threading.Lock()

OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HCIN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HCOUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"

# Caché en memoria de tasas del tiempo real. Clave: (device_ip, interface_name,
# if_index). TTL: si el poll salta (pestana oculta), se descarta la referencia
# anterior y el siguiente par de muestras recalcula.
_LIVE_RATE_CACHE: Dict[Tuple[str, str, str], Tuple[float, int, int]] = {}
_LIVE_TTL = 60.0

# Caché de resolucion ifName -> ifIndex (se re-resuelve al minuto 5).
_IFNAME_CACHE: Dict[Tuple[str, str], Tuple[float, str]] = {}
_IFNAME_CACHE_TTL = 300.0


def clear_live_cache() -> None:
    _LIVE_RATE_CACHE.clear()
    _IFNAME_CACHE.clear()


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip().strip('"\''))
    except (ValueError, TypeError):
        return None


def derive_live_rate(
    key: Tuple[str, str, str],
    rx_octets: Optional[int],
    tx_octets: Optional[int],
    now: float,
) -> Tuple[Optional[float], Optional[float]]:
    """rx/tx en bps entre muestreos consecutivos (con TTL). Actualiza la caché."""
    rx_bps = tx_bps = None
    prev = _LIVE_RATE_CACHE.get(key)
    if prev is not None and rx_octets is not None and tx_octets is not None:
        p_ts, p_in, p_out = prev
        dt = now - p_ts
        if 0 < dt <= _LIVE_TTL:
            d_in = rx_octets - p_in
            d_out = tx_octets - p_out
            if d_in >= 0:
                rx_bps = d_in * 8 / dt
            if d_out >= 0:
                tx_bps = d_out * 8 / dt
    if rx_octets is not None and tx_octets is not None:
        _LIVE_RATE_CACHE[key] = (now, rx_octets, tx_octets)
    return rx_bps, tx_bps


def get_device_credentials(device_type: str, device_ip: str) -> Optional[Dict[str, str]]:
    """Comunidad/hostname del dispositivo desde el ConfigManager."""
    manager = get_config_manager()
    if (device_type or "").lower() == "olt":
        olt = manager.get_olt_by_ip(device_ip)
        if olt is None:
            return None
        return {"ip": olt.ip, "community": olt.community, "hostname": olt.hostname}
    mt = manager.get_mikrotik_by_ip(device_ip)
    if mt is None:
        return None
    return {"ip": mt.ip, "community": mt.community, "hostname": mt.hostname}


def _pysnmp_get_many(ip: str, community: str, oids, timeout: float = 4.0, retries: int = 1):
    """N GETs SNMP v2c (pysnmp v3arch asyncio, sync via asyncio.run en thread)."""
    import asyncio

    async def _do():
        from pysnmp.hlapi.v3arch.asyncio import (
            get_cmd, SnmpEngine, CommunityData, UdpTransportTarget,
            ContextData, ObjectType, ObjectIdentity,
        )
        engine = SnmpEngine()
        transport = await UdpTransportTarget.create(
            (ip, 161), timeout=timeout, retries=retries
        )
        try:
            indicator, status, _, var_binds = await get_cmd(
                engine,
                CommunityData(community),
                transport,
                ContextData(),
                *[ObjectType(ObjectIdentity(oid)) for oid in oids],
            )
            if indicator or status:
                return None
            return {list(oids)[i]: str(v) for i, (_, v) in enumerate(var_binds)}
        finally:
            engine.close_dispatcher()

    try:
        return asyncio.run(_do())
    except Exception as e:
        logger.debug(f"live snmp get failed {ip}: {e}")
        return None


def _pysnmp_ifname_walk(ip: str, community: str, timeout: float = 40.0, retries: int = 1):
    """Walk de la tabla ifName (solo cuando falta interface_index).

    La tabla puede ser enorme (miles de interfaces) y la OLT lenta para
    GETNEXT iterativos de pysnmp; se usa `snmpbulkwalk` por subproceso (mismo
    enfoque que los collectors). Devuelve lo ya volcado aunque el subproceso
    sea cortado por timeout (tabla parcial). El frotend normalmente pasa el
    interface_index explícito y este fallback solo resuelve casos puntuales.
    """
    import os
    import subprocess
    import tempfile

    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.snmp', delete=False)
        tmp.close()
        with open(tmp.name, 'w') as out:
            try:
                subprocess.run(
                    ["snmpbulkwalk", "-v2c", "-c", community,
                     "-Cr100", "-t", "30", "-r", "0",
                     "-On", ip, OID_IF_NAME],
                    stdout=out, stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                pass
        table = {}
        with open(tmp.name, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                oid_part, _, val_part = line.partition("=")
                oid_part = oid_part.strip().lstrip(".")
                if not oid_part.startswith(OID_IF_NAME):
                    continue
                suffix = oid_part[len(OID_IF_NAME) + 1:]
                val = val_part.strip()
                if ":" in val:
                    val = val.split(":", 1)[-1].strip()
                table[suffix] = val.strip('"')
        return table
    except Exception as e:
        logger.debug(f"live ifName walk failed {ip}: {e}")
        return {}
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


class LiveTrafficReader:
    """Lector de contadores bajo demanda (tiempo real, no persistido)."""

    def read(
        self,
        device_type: str,
        device_ip: str,
        interface_name: str,
        interface_index: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict:
        creds = get_device_credentials(device_type, device_ip)
        if creds is None:
            return {"ok": False, "error": "dispositivo_no_encontrado"}
        now_val = now if now is not None else time.time()

        idx = interface_index
        if not idx:
            now0 = time.time()
            cached = _IFNAME_CACHE.get((creds["ip"], interface_name))
            if cached and now0 - cached[0] <= _IFNAME_CACHE_TTL and cached[1]:
                idx = cached[1]
            else:
                # 1) rápido: Datos ya muestreados por el sampler (0 consulta SNMP)
                idx = self._resolve_index_from_home(creds["hostname"], interface_name)
                if not idx:
                    # 2) lento: walk de ifName (por subproceso), serializado
                    with _READ_LOCK:
                        idx = self._resolve_index(creds["ip"], creds["community"], interface_name)
                if idx:
                    _IFNAME_CACHE[(creds["ip"], interface_name)] = (time.time(), idx)
            if idx is None:
                return {"ok": False, "error": "interfaz_no_encontrada"}

        rx_oid = f"{OID_IF_HCIN_OCTETS}.{idx}"
        tx_oid = f"{OID_IF_HCOUT_OCTETS}.{idx}"
        try:
            raw = _pysnmp_get_many(creds["ip"], creds["community"], [rx_oid, tx_oid])
        except Exception as e:
            logger.debug(f"live snmp read failed: {e}")
            raw = None
        if raw is None:
            return {"ok": False, "error": "snmp_error"}

        rx_octets = _safe_int(raw.get(rx_oid))
        tx_octets = _safe_int(raw.get(tx_oid))
        key = (device_ip, interface_name, str(idx))
        rx_bps, tx_bps = derive_live_rate(key, rx_octets, tx_octets, now_val)
        return {
            "ok": True,
            "device_type": device_type,
            "device_ip": device_ip,
            "device_name": creds["hostname"],
            "interface_name": interface_name,
            "interface_index": str(idx),
            "rx_octets": rx_octets,
            "tx_octets": tx_octets,
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
            "ts": now_val,
        }

    def _resolve_index_from_home(self, device_name: str, interface_name: str) -> Optional[str]:
        try:
            from .influx_helper import resolve_interface_index
            return resolve_interface_index(device_name, interface_name)
        except Exception as e:
            logger.debug(f"resolve index from influx failed: {e}")
            return None

    def _resolve_index(self, ip: str, community: str, interface_name: str) -> Optional[str]:
        now = time.time()
        cached = _IFNAME_CACHE.get((ip, interface_name))
        if cached and now - cached[0] <= _IFNAME_CACHE_TTL:
            return cached[1]
        try:
            table = _pysnmp_ifname_walk(ip, community)
        except Exception as e:
            logger.debug(f"ifName walk failed: {e}")
            table = {}
        needle = interface_name.strip().lower()
        for suffix, name in table.items():
            if str(name).strip().strip('"').lower() == needle:
                _IFNAME_CACHE[(ip, interface_name)] = (now, suffix)
                return suffix
        # cache negativo: evita reandar durante 5 min en cada poll
        _IFNAME_CACHE[(ip, interface_name)] = (now, None)
        return None