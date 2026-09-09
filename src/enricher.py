# src/enricher.py
"""
Enriquecedor de eventos WISP.

Antes de que un evento llegue al agente LLM, lo enriquece con:
- Clientes afectados (client_lookup + grouper)
- Zona geográfica
- Si es parte de una cascada (topología en InfluxDB)
- Histórico reciente del mismo dispositivo
"""
import time
import logging
from typing import Dict, Optional, Any, List

from .event_bus import WispEvent
from .client_lookup import ClientLookup

logger = logging.getLogger(__name__)


class EventEnricher:
    """Enriquece eventos con contexto de red, clientes y topología."""

    def __init__(self, client_lookup: ClientLookup, influx_client=None):
        self.client_lookup = client_lookup
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

    def _add_client_info(self, event: WispEvent) -> None:
        serial = event.payload.get("onu_serial", "")
        if not serial:
            return

        client = self.client_lookup.lookup_by_serial(serial)
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

        pon_clients = self.client_lookup.clients_on_pon(olt_name, pon_port)
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
