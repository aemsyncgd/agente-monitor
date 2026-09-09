# src/enricher.py
"""
Enriquecedor de eventos WISP.

Antes de que un evento llegue al agente LLM, lo enriquece con:
- Clientes afectados (base enriquecida clientes_enriquecidos en InfluxDB)
- Zona geográfica
- Si es parte de una cascada (topología en InfluxDB)
- Histórico reciente del mismo dispositivo
"""
import time
import logging
from typing import Dict, Optional, Any, List

from .event_bus import WispEvent
from .models import Client

logger = logging.getLogger(__name__)


def _estado_label(estado_onu) -> str:
    try:
        estado = int(estado_onu)
    except (TypeError, ValueError):
        estado = -1
    if estado == 3:
        return "Activo"
    if estado in (1, 4, 6):
        return "Caído"
    return "N/A"


def _as_client(row) -> Optional[Client]:
    """Normaliza una fila enriquecida (dict) o un Client ya cargado."""
    if row is None:
        return None
    if isinstance(row, Client):
        return row
    if isinstance(row, dict):
        return Client(
            documento=str(row.get("documento", "") or ""),
            nombre=str(row.get("nombre", "") or ""),
            serial_onu=str(row.get("serial_onu", "") or ""),
            mac=str(row.get("mac", "") or ""),
            olt=str(row.get("olt", "") or ""),
            nodo=str(row.get("nodo", "") or ""),
            puerto_pon=str(row.get("pon_port", "") or row.get("puerto_pon", "") or ""),
            direccion=str(row.get("direccion", "") or ""),
            estado=_estado_label(row.get("estado_onu", -1)),
            zona=str(row.get("zona", "") or ""),
            ip_servicio=str(row.get("ip_servicio", "") or ""),
        )
    return None


class EventEnricher:
    """Enriquece eventos con contexto de red, clientes y topología.

    Acepta `reader` (InfluxReader sobre clientes_enriquecidos) o, por
    compatibilidad, un `client_lookup` legacy con `lookup_by_serial` /
    `clients_on_pon` (p.ej. un MagicMock en tests).
    """

    def __init__(self, client_lookup=None, influx_client=None, reader=None):
        self.client_lookup = client_lookup
        self.reader = reader
        self.influx = influx_client
        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 60.0

    def enrich(self, event: WispEvent) -> WispEvent:
        if event._enriched:
            return event

        try:
            self._add_client_info(event)
            self._add_zone_info(event)
            self._add_affected_clients_count(event)
            self._add_topology_context(event)
            self._add_recent_history(event)
            event._enriched = True
        except Exception as e:
            logger.error(f"Enrichment failed for event {event.id}: {e}")

        return event

    def _find_client(self, serial: str) -> Optional[Client]:
        if self.reader is not None:
            serial_u = str(serial).strip().upper()
            rows = self.reader.enriched_clients(search=str(serial).strip(), limit=10)
            for r in rows:
                if (r.get("serial_onu") or "").upper() == serial_u:
                    return _as_client(r)
            return _as_client(rows[0]) if rows else None
        if self.client_lookup is not None:
            return _as_client(self.client_lookup.lookup_by_serial(serial))
        return None

    def _clients_on_pon(self, olt_name: str, pon_port: str) -> List[Client]:
        if self.reader is not None:
            rows = self.reader.enriched_clients(olt=str(olt_name or ""), limit=100)
            pon = str(pon_port or "").strip().lower()
            matched = [r for r in rows
                       if pon in (r.get("pon_port") or "").lower()]
            return [_as_client(r) for r in matched[:50] if r]
        if self.client_lookup is not None:
            return [_as_client(c) for c in
                    self.client_lookup.clients_on_pon(olt_name, pon_port)]
        return []

    def _add_client_info(self, event: WispEvent) -> None:
        serial = event.payload.get("onu_serial", "")
        if not serial:
            return

        client = self._find_client(serial)
        if client:
            event.context["client_name"] = client.nombre
            event.context["client_address"] = client.direccion
            event.context["client_node"] = client.nodo
            event.context["client_pon_port"] = client.puerto_pon
            event.context["client_status"] = client.estado

    def _add_zone_info(self, event: WispEvent) -> None:
        address = event.context.get("client_address", "")
        if not address:
            node = event.context.get("client_node", "")
            if node:
                event.context["zone"] = node
            return

        zone = self._extract_zone(address)
        if zone:
            event.context["zone"] = zone

    def _add_affected_clients_count(self, event: WispEvent) -> None:
        olt_name = event.payload.get("olt_name", "")
        pon_port = event.payload.get("pon_port", "")
        if not olt_name or not pon_port:
            return

        pon_clients = self._clients_on_pon(olt_name, pon_port)
        event.context["clientes_afectados"] = len(pon_clients)
        event.context["affected_client_names"] = [
            c.nombre for c in pon_clients[:10] if c.nombre
        ]

    def _add_topology_context(self, event: WispEvent) -> None:
        if not self.influx:
            return

        device_id = event.device_id
        if not device_id:
            return

        cache_key = f"topology:{device_id}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached.get("_ts", 0)) < self._cache_ttl:
            event.context["topology"] = cached.get("topology", {})
            return

        try:
            flux = f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "ping_check")
                |> filter(fn: (r) => r["name"] == "{device_id}")
                |> last()
            '''
            results = self.influx.query(flux)
            if results:
                topo = {
                    "device_name": device_id,
                    "last_seen": results[0].get("time", ""),
                    "status": "up" if results[0].get("value", 1) == 1 else "down",
                }
                event.context["topology"] = topo
                self._cache[cache_key] = {"topology": topo, "_ts": time.time()}
        except Exception as e:
            logger.debug(f"Topology context query failed: {e}")

    def _add_recent_history(self, event: WispEvent) -> None:
        device_id = event.device_id or event.payload.get("olt_name", "")
        if not device_id or not self.influx:
            return

        try:
            flux = f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -24h)
                |> filter(fn: (r) => r["_measurement"] == "optical_power")
                |> filter(fn: (r) => r["olt_name"] == "{device_id}")
                |> filter(fn: (r) => r["_field"] == "rx_power")
                |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
                |> last()
            '''
            results = self.influx.query(flux)
            if results:
                recent_values = [r.get("value") for r in results if r.get("value") is not None]
                if recent_values:
                    event.context["historic_avg_power"] = round(
                        sum(recent_values) / len(recent_values), 2
                    )
                    event.context["historic_min_power"] = round(min(recent_values), 2)
                    event.context["historic_max_power"] = round(max(recent_values), 2)
                    event.context["historic_samples"] = len(recent_values)
        except Exception as e:
            logger.debug(f"History query failed: {e}")

    @staticmethod
    def _extract_zone(address: str) -> str:
        if not address:
            return ""
        addr = address.strip()
        for prefix in ["Sector ", "Barrio ", "Colonia ", "Zona ", "Urb. "]:
            if addr.startswith(prefix):
                addr = addr[len(prefix):]
        words = addr.split()[:3]
        return " ".join(words) if words else ""