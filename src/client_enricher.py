# src/client_enricher.py
"""
ClientEnricher: cruza las 3 fuentes de datos para enriquecer la base de clientes.

Fuente 1: OLTs (InfluxDB `onu_location`) -> MAC + serial + OLT + PON + estado.
Fuente 2: MikroTik RouterOS API (InfluxDB `routeros_pppoe` / `routeros_dhcp`)
          -> usuario PPPoE -> MAC.
Fuente 3: WISP (API czwisp / CSV) -> documento, serial, PPPoE, datos de servicio.

Matching (por cliente WISP):
  1. serial_onu  -> OLT                      (fuente 1, match directo)
  2. usuario_pppoe -> MAC (fuente 2) -> OLT  (match indirecto)
  3. si no cruza -> `clientes_pendientes` con causa.

Persiste:
  - wisp_clients          (catálogo, tags de búsqueda)
  - clientes_enriquecidos (OLT + pon + estado + flag ubicacion_descuadrada)
  - clientes_pendientes   (con causa)
"""
import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from .config_ia import EnrichmentConfig
from .storage.influx_client import InfluxClient, MetricPoint

logger = logging.getLogger(__name__)


def rows_from_query(results: List[Dict]) -> Dict[str, Dict]:
    """Agrupa registros de InfluxClient.query() en filas {clave_tags: {campos}}.

    Cada campo viene como un registro separado con los mismos tags; se unen
    por el conjunto de tags y se conserva el último timestamp por fila.
    """
    rows: Dict[str, Dict] = {}
    order: Dict[str, float] = {}
    for r in results:
        tags = {k: v for k, v in r.items()
                if k not in ("measurement", "field", "value", "time")}
        key = "|".join(f"{k}={v}" for k, v in sorted(tags.items()))
        ts = r.get("time")
        ts_key = _ts_seconds(ts)
        prev_ts = order.get(key)
        if prev_ts is not None and ts_key < prev_ts:
            continue
        row = rows.setdefault(key, dict(tags))
        row[r.get("field", "")] = r.get("value")
        order[key] = ts_key
    return rows


def _ts_seconds(ts) -> float:
    if ts is None:
        return 0.0
    try:
        if hasattr(ts, "timestamp"):
            return float(ts.timestamp())
        return float(ts)
    except (ValueError, TypeError):
        return 0.0


def _norm_mac(value) -> str:
    mac = str(value or "").strip().lower()
    if not mac or mac in ("none", "0:0:0:0:0:0", "00:00:00:00:00:00", "nan"):
        return ""
    parts = mac.split(":")
    if parts and len(parts[0]) == 1:
        parts = [p.rjust(2, "0") for p in parts]
    return ":".join(parts)


def _tag(doc: dict, key: str) -> str:
    v = doc.get(key)
    return str(v).strip() if v is not None else ""


def _field(doc: dict, key: str, default: str = "") -> str:
    v = doc.get(key)
    if v is None:
        return default
    return str(v)


# ---------------------------------------------------------------------------
# Matching puro (sin I/O; testeable en aislamiento)
# ---------------------------------------------------------------------------

def enrich_clients(wisp_clients: List[dict],
                   olt_rows: List[dict],
                   pppoe_rows: List[dict],
                   dhcp_rows: List[dict]) -> (List[dict], List[dict]):
    """Devuelve (enriquecidos, pendientes). No toca red ni disco."""
    olt_by_serial: Dict[str, dict] = {}
    olt_by_mac: Dict[str, dict] = {}
    for row in olt_rows:
        serial = _field(row, "onu_serial").upper()
        if serial:
            olt_by_serial.setdefault(serial, row)
        mac = _norm_mac(_tag(row, "mac"))
        if mac:
            olt_by_mac.setdefault(mac, row)

    pppoe_by_user: Dict[str, dict] = {}
    for row in pppoe_rows:
        user = _tag(row, "username").lower()
        if not user:
            continue
        row = dict(row)
        row["mac"] = _norm_mac(_field(row, "mac_address"))
        prev = pppoe_by_user.get(user)
        if prev is None or (not prev.get("mac") and row.get("mac")):
            pppoe_by_user[user] = row

    dhcp_by_mac: Dict[str, dict] = {}
    for row in dhcp_rows:
        mac = _norm_mac(_tag(row, "mac_address"))
        if mac:
            dhcp_by_mac.setdefault(mac, row)

    matched: List[dict] = []
    pending: List[dict] = []

    for client in wisp_clients:
        documento = _tag(client, "documento")
        serial = _tag(client, "serial_onu").upper()
        usuario = _tag(client, "usuario_pppoe").lower()
        if not documento and not serial and not usuario:
            continue

        pppoe = pppoe_by_user.get(usuario) if usuario else None
        mac = _norm_mac(pppoe.get("mac") if pppoe else _tag(client, "mac"))
        if not mac and usuario:
            # Pospone: sin MAC no se puede verificar la ubicación física.
            pass

        onu = olt_by_serial.get(serial) or olt_by_mac.get(mac)

        if onu:
            onu_serial = _field(onu, "onu_serial").upper()
            coincidencia = "serial" if serial and serial == onu_serial else "mac"
            descuadrado = (bool(serial) and bool(onu_serial) and serial != onu_serial)
            matched.append(_build_matched(client, onu, pppoe,
                                          coincidencia=coincidencia,
                                          descuadrado=descuadrado))
        else:
            if serial or mac:
                causa = "sin_olt"       # serial/MAC conocido pero no aparece en la OLT
            elif usuario:
                causa = "sin_pppoe"     # PPPoE sin MAC correlacionado
            else:
                causa = "sin_cruce"
            pending.append(_build_pending(client, causa=causa))

    return matched, pending


def _build_matched(client: dict, onu: dict, pppoe: Optional[dict],
                   coincidencia: str, descuadrado: bool) -> dict:
    mac = _norm_mac(onu.get("mac") or (pppoe or {}).get("mac"))
    return {
        "documento": _tag(client, "documento"),
        "usuario_pppoe": _tag(client, "usuario_pppoe").lower(),
        "serial_onu": _tag(client, "serial_onu").upper(),
        "mac": mac,
        "olt": _tag(onu, "olt") or _tag(onu, "olt_name"),
        "nodo": _tag(onu, "nodo"),
        "pon_port": _tag(onu, "pon_port"),
        "onu_index": _tag(onu, "onu_index"),
        "nap": _tag(client, "nap_nombre"),
        "zona": _tag(client, "zona") or _tag(onu, "nodo"),
        "estado_onu": _field(onu, "estado_onu", ""),
        "ubicacion_descuadrada": descuadrado,
        "coincidencia": coincidencia,
        "nombre": _field(client, "nombre"),
        "direccion": _field(client, "direccion"),
        "telefono": _field(client, "telefono"),
        "ip_servicio": _field(client, "ip_servicio"),
        "puerto": _field(client, "puerto"),
        "plan": _field(client, "plan"),
        "estado_servicio": _field(client, "estado"),
        "pppoe_status": _tag(pppoe, "status") if pppoe else "",
    }


def _build_pending(client: dict, causa: str) -> dict:
    return {
        "documento": _tag(client, "documento"),
        "usuario_pppoe": _tag(client, "usuario_pppoe").lower(),
        "serial_onu": _tag(client, "serial_onu").upper(),
        "causa": causa,
        "nombre": _field(client, "nombre"),
        "nap": _tag(client, "nap_nombre"),
        "zona": _tag(client, "zona"),
        "detalle": _field(client, "estado"),
    }


def _clean_tags(tags: Dict[str, any]) -> Dict[str, str]:
    out = {}
    for k, v in tags.items():
        s = str(v).strip() if v is not None else ""
        if s and s.lower() not in ("nan", "none"):
            out[k] = s
    return out


# ---------------------------------------------------------------------------
# Orquestador (lee Influx + WISP, persiste resultados)
# ---------------------------------------------------------------------------

@dataclass
class EnrichStats:
    wisp_total: int = 0
    matched: int = 0
    pending: int = 0
    mismatched: int = 0
    by_causa: Dict[str, int] = field(default_factory=dict)


class ClientEnricher:
    """Ciclo de enriquecimiento: WISP + OLT + PPPoE/DHCP -> InfluxDB."""

    def __init__(self, influx: InfluxClient, wisp, config: EnrichmentConfig,
                 olt_measurement: str = "onu_location",
                 pppoe_measurement: str = "routeros_pppoe",
                 dhcp_measurement: str = "routeros_dhcp"):
        self.influx = influx
        self.wisp = wisp
        self.config = config
        self.olt_measurement = olt_measurement
        self.pppoe_measurement = pppoe_measurement
        self.dhcp_measurement = dhcp_measurement

    def run(self) -> EnrichStats:
        start = time.time()
        stats = EnrichStats()
        if not self.config.enabled:
            logger.info("Enriquecimiento deshabilitado (enrichment.enabled=false)")
            return stats
        if not self.influx._query_api:
            if not self.influx.connect():
                logger.error("InfluxDB no disponible; enriquecimiento omitido")
                return stats

        clients = self.wisp.list_clients() if self.wisp else []
        stats.wisp_total = len(clients)
        if not clients:
            logger.warning("Sin clientes WISP (API ni CSV); enriquecimiento omitido")
            return stats

        olt_rows = self._load_rows(self.olt_measurement, "onu")
        pppoe_rows = self._load_rows(self.pppoe_measurement, "pppoe")
        dhcp_rows = self._load_rows(self.dhcp_measurement, "dhcp")
        logger.info(
            f"Enriquecimiento fuentes: olt={len(olt_rows)} pppoe={len(pppoe_rows)} "
            f"dhcp={len(dhcp_rows)}"
        )

        matched, pending = enrich_clients(clients, olt_rows, pppoe_rows, dhcp_rows)
        stats.matched = len(matched)
        stats.pending = len(pending)
        for p in pending:
            causa = p.get("causa", "")
            stats.by_causa[causa] = stats.by_causa.get(causa, 0) + 1
        stats.mismatched = sum(1 for m in matched if m.get("ubicacion_descuadrada"))

        self._persist_catalog(clients)
        self._persist_matched(matched)
        self._persist_pending(pending)

        elapsed = time.time() - start
        logger.info(
            f"Enriquecimiento: {stats.matched} enriquecidos, {stats.pending} "
            f"pendientes, {stats.mismatched} descuadrados ({elapsed:.1f}s)"
        )
        return stats

    # ----- lectura de fuentes -----
    def _load_rows(self, measurement: str, kind: str) -> List[dict]:
        flux = (
            f'from(bucket: "{self.influx.bucket}")\n'
            f'  |> range(start: -{self.config.lookback_hours}h)\n'
            f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
            f'  |> yield(name: "enrich")'
        )
        results = self.influx.query(flux)
        rows = rows_from_query(results)
        return list(rows.values())

    # ----- persistencia -----
    def _persist_catalog(self, clients: List[dict]) -> None:
        points = []
        for c in clients:
            doc, serial, user = _tag(c, "documento"), _tag(c, "serial_onu"), _tag(c, "usuario_pppoe")
            if not doc and not serial and not user:
                continue
            tags = _clean_tags({
                "documento": doc,
                "serial_onu": serial.upper(),
                "usuario_pppoe": user.lower(),
                "nap": _tag(c, "nap_nombre"),
                "zona": _tag(c, "zona"),
            })
            points.append(MetricPoint(
                measurement="wisp_clients",
                tags=tags,
                fields={
                    "nombre": _field(c, "nombre"),
                    "direccion": _field(c, "direccion"),
                    "telefono": _field(c, "telefono"),
                    "plan": _field(c, "plan"),
                    "estado": _tag(c, "estado"),
                    "ip_servicio": _field(c, "ip_servicio"),
                    "puerto": _field(c, "puerto"),
                },
            ))
        n = self.influx.write_points(points) if points else 0
        logger.info(f"Catálogo WISP: {n}/{len(points)} puntos escritos")

    def _persist_matched(self, matched: List[dict]) -> None:
        points = []
        for m in matched:
            tags = _clean_tags({
                "documento": m.get("documento", ""),
                "usuario_pppoe": m.get("usuario_pppoe", ""),
                "serial_onu": m.get("serial_onu", ""),
                "mac": m.get("mac", ""),
                "olt": m.get("olt", ""),
                "nodo": m.get("nodo", ""),
                "nap": m.get("nap", ""),
                "zona": m.get("zona", ""),
                "ubicacion_descuadrada": str(m.get("ubicacion_descuadrada", False)).lower(),
            })
            fields = {
                "nombre": m.get("nombre", ""),
                "direccion": m.get("direccion", ""),
                "telefono": m.get("telefono", ""),
                "pon_port": m.get("pon_port", ""),
                "onu_index": m.get("onu_index", ""),
                "puerto": m.get("puerto", ""),
                "plan": m.get("plan", ""),
                "ip_servicio": m.get("ip_servicio", ""),
                "coincidencia": m.get("coincidencia", ""),
                "pppoe_status": m.get("pppoe_status", ""),
            }
            try:
                fields["estado_onu"] = int(m.get("estado_onu", ""))
            except (ValueError, TypeError):
                fields["estado_onu"] = -1
            points.append(MetricPoint(measurement="clientes_enriquecidos",
                                      tags=tags, fields=fields))
        n = self.influx.write_points(points) if points else 0
        logger.info(f"Enriquecidos: {n}/{len(points)} puntos escritos")

    def _persist_pending(self, pending: List[dict]) -> None:
        points = []
        for p in pending:
            tags = _clean_tags({
                "documento": p.get("documento", ""),
                "usuario_pppoe": p.get("usuario_pppoe", ""),
                "serial_onu": p.get("serial_onu", ""),
                "causa": p.get("causa", ""),
            })
            points.append(MetricPoint(
                measurement="clientes_pendientes",
                tags=tags,
                fields={
                    "nombre": p.get("nombre", ""),
                    "nap": p.get("nap", ""),
                    "zona": p.get("zona", ""),
                    "detalle": p.get("detalle", ""),
                },
            ))
        n = self.influx.write_points(points) if points else 0
        logger.info(f"Pendientes: {n}/{len(points)} puntos escritos")