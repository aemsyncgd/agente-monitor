# src/ai/llm_agent/tools.py
"""
Tools = enabled rules.

Each enabled instruction in config/instructions.json gates one or more tools
exposed to the LLM. All tools are READ-ONLY except `send_alert`, which the LLM
uses to dispatch alerts via Telegram and to the dashboard event log.

The registry is rebuilt each cycle so toggling a rule from the dashboard takes
effect immediately.
"""
import time
import logging
import redis
import json
import os
from typing import Dict, List, Optional, Any, Callable, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# InfluxDB reader (lightweight, no torch)
# ---------------------------------------------------------------------------


class InfluxReader:
    def __init__(self, influx_client):
        self.influx = influx_client

    def _query(self, flux: str) -> List[Dict]:
        return self.influx.query(flux) or []

    def overall_stats(self) -> Dict[str, Any]:
        bucket = self.influx.bucket
        def _count(flux: str) -> int:
            rows = self._query(flux)
            if rows and rows[0].get("value") is not None:
                try:
                    return int(rows[0]["value"])
                except (TypeError, ValueError):
                    return 0
            return 0

        def _float_power(extra: str) -> str:
            return f'''
        from(bucket: "{bucket}") |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["_field"] == "rx_power")
            {extra}
            |> map(fn: (r) => ({{
                r with _value: float(v: r._value)
            }}))'''

        olt_flux = f'''
        from(bucket: "{bucket}") |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "olt_system")
            |> filter(fn: (r) => r["_field"] == "cpu")
            |> group(columns: ["olt_name"]) |> last() |> group() |> count()'''
        onu_flux = f'''
        from(bucket: "{bucket}") |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["_field"] == "status")
            |> group() |> distinct(column: "onu_serial") |> count()'''
        onu_online_flux = f'''
        from(bucket: "{bucket}") |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["_field"] == "status")
            |> filter(fn: (r) => r["_value"] == 3)
            |> group() |> distinct(column: "onu_serial") |> count()'''
        low_power_flux = _float_power(
            '|> last() |> filter(fn: (r) => r["_value"] < -28.0)') + '''|> group() |> count()'''

        total_olts = _count(olt_flux)
        total_onus = _count(onu_flux)
        online_onus = _count(onu_online_flux)
        low_power = _count(low_power_flux)

        down_hosts = 0
        ping_rows = self._query(f'''
        from(bucket: "{bucket}") |> range(start: -10m)
            |> filter(fn: (r) => r["_measurement"] == "ping_check")
            |> filter(fn: (r) => r["_field"] == "status") |> last()''')
        for row in ping_rows:
            if int(row.get("value", 1)) == 0:
                down_hosts += 1

        return {
            "olts": total_olts,
            "onus_total": total_onus,
            "onus_online": online_onus,
            "onus_offline": total_onus - online_onus,
            "low_power_alerts": low_power,
            "hosts_down": down_hosts,
        }

    def anomalies(self, olt_name: Optional[str] = None, hours: int = 24,
                  limit: int = 50) -> List[Dict]:
        bucket = self.influx.bucket
        olt_filter = ""
        if olt_name:
            olt_filter = f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'
        flux = f'''
        from(bucket: "{bucket}") |> range(start: -{hours}h)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["_field"] == "rx_power")
            {olt_filter}
            |> last() |> filter(fn: (r) => r["_value"] < -28.0)
            |> limit(n: {limit})'''
        out = []
        for r in self._query(flux):
            olt = r.get("olt_name", "")
            pon = r.get("pon_port", "")
            serial = r.get("onu_serial", "")
            rx = r.get("value")
            status = r.get("status", "")
            sev = "critical" if (rx is not None and rx < -32.0) else "warning"
            out.append({
                "olt": olt, "pon": pon, "onu_serial": serial,
                "rx_power": rx, "status": status, "severity": sev,
            })
        return out

    def ping_status(self) -> Dict[str, Any]:
        bucket = self.influx.bucket
        rows = self._query(f'''
        from(bucket: "{bucket}") |> range(start: -10m)
            |> filter(fn: (r) => r["_measurement"] == "ping_check")
            |> filter(fn: (r) => r["_field"] == "status") |> last()''')
        down, up = [], []
        for r in rows:
            name = r.get("name", "")
            ip = r.get("ip", "")
            htype = r.get("type", "")
            if int(r.get("value", 1)) == 0:
                down.append({"name": name, "ip": ip, "type": htype})
            else:
                up.append({"name": name, "ip": ip, "type": htype})
        return {"down": down, "up": up}

    def olt_status(self, limit: int = 30) -> List[Dict]:
        bucket = self.influx.bucket
        rows = self._query(f'''
        from(bucket: "{bucket}") |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "olt_system")
            |> last() |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["olt_name"]) |> limit(n: {limit})''')
        out = []
        for r in rows:
            out.append({
                "olt": r.get("olt_name", ""),
                "node": r.get("nodo", ""),
                "modelo": r.get("modelo", ""),
                "cpu": r.get("cpu_usage") if r.get("cpu_usage") is not None else r.get("cpu"),
                "ram": r.get("ram_usage") if r.get("ram_usage") is not None else r.get("mem_usage"),
                "temp": r.get("temp"),
                "onus_total": r.get("onu_total") or r.get("onus_total"),
                "onus_online": r.get("onu_online") or r.get("onus_online"),
            })
        return out

    def interface_status(self, device_name: Optional[str] = None,
                         top: int = 15) -> List[Dict]:
        bucket = self.influx.bucket
        device_filter = ""
        if device_name:
            device_filter = f'|> filter(fn: (r) => r["device_name"] == "{device_name}")'
        rows = self._query(f'''
        from(bucket: "{bucket}") |> range(start: -20m)
            |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
            {device_filter}
            |> last() |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["_time"], desc: true) |> limit(n: {top * 4})''')
        ranked = sorted(
            rows,
            key=lambda r: (r.get("rx_bps") or 0) + (r.get("tx_bps") or 0),
            reverse=True,
        )
        out = []
        for r in ranked[:top]:
            out.append({
                "device": r.get("device_name", ""),
                "interface": r.get("interface_name", ""),
                "rx_bps": r.get("rx_bps"),
                "tx_bps": r.get("tx_bps"),
                "oper_status": r.get("ifOperStatus"),
            })
        return out

    def enriched_clients(self, search: Optional[str] = None,
                         olt: Optional[str] = None,
                         nap: Optional[str] = None,
                         zona: Optional[str] = None,
                         mismatched: bool = False,
                         limit: int = 50) -> List[Dict]:
        """Clientes enriquecidos (clientes_enriquecidos), ultima fila por MAC."""
        bucket = self.influx.bucket
        filters = ""
        if olt:
            filters += f'|> filter(fn: (r) => r["olt"] == "{olt}")'
        if nap:
            filters += f'|> filter(fn: (r) => r["nap"] == "{nap}")'
        if zona:
            filters += f'|> filter(fn: (r) => r["zona"] == "{zona}")'
        if mismatched:
            filters += '|> filter(fn: (r) => r["ubicacion_descuadrada"] == "true")'

        flux = f'''
        from(bucket: "{bucket}") |> range(start: -168h)
            |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")
            {filters}
            |> group(columns: ["mac"]) |> last()
            |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> sort(columns: ["olt", "nap"]) |> limit(n: {limit})'''
        rows = self._query(flux)
        if search:
            needle = search.strip().lower()
            rows = [
                r for r in rows
                if needle in (r.get("nombre") or "").lower()
                or needle in (r.get("documento") or "").lower()
                or needle in (r.get("serial_onu") or "").lower()
                or needle in (r.get("usuario_pppoe") or "").lower()
                or needle in (r.get("mac") or "").lower()
            ]
        return rows

    def client_by_documento(self, documento: str) -> Optional[Dict]:
        """Ficha enriquecida de un cliente por documento."""
        bucket = self.influx.bucket
        esc = str(documento).replace("\\", "\\\\").replace('"', '\\"')
        flux = f'''
        from(bucket: "{bucket}") |> range(start: -168h)
            |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")
            |> filter(fn: (r) => r["documento"] == "{esc}")
            |> last()
            |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> limit(n: 1)'''
        rows = self._query(flux)
        return rows[0] if rows else None

    def predict_failures(self, hours: int = 24, max_results: int = 20) -> List[Dict]:
        """Trend-based failure prediction per ONU (same logic as deterministic agent).

        Solo se usa la tendencia de las últimas 24 muestras, así que el rango se
        acota a 6h con media cada 15m (igual que la API): evita materializar
        24h x 5m (~216k filas / 40MB) por llamada, que saturaba InfluxDB.
        """
        bucket = self.influx.bucket
        _WINDOW = 15  # minutos por muestra
        rows = self._query(f'''
        from(bucket: "{bucket}") |> range(start: -6h)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["_field"] == "rx_power")
            |> aggregateWindow(every: {_WINDOW}m, fn: mean, createEmpty: false)''')

        onu_data: Dict[Tuple, List] = {}
        for r in rows:
            key = (r.get("olt_name"), r.get("pon_port"), r.get("onu_index"), r.get("onu_serial"))
            if key not in onu_data:
                onu_data[key] = []
            v = r.get("value")
            if v is not None:
                onu_data[key].append(float(v))

        risks = []
        for (olt, pon, idx, serial), values in onu_data.items():
            if len(values) < 12:
                continue
            recent = values[-12:]
            older = values[-24:-12] if len(values) >= 24 else values[: len(values) // 2]
            if not older:
                continue
            recent_avg = sum(recent) / len(recent)
            older_avg = sum(older) / len(older)
            hours_data = len(recent) * _WINDOW / 60
            if hours_data <= 0:
                continue
            trend = (recent_avg - older_avg) / hours_data
            if trend < -0.5 and recent_avg > -30:
                hours_to_failure = (recent_avg + 30) / abs(trend)
                confidence = min(1.0, abs(trend) / 2.0)
                if hours_to_failure < 48:
                    risks.append({
                        "olt": olt, "pon": pon, "onu_serial": serial,
                        "current_power": round(recent_avg, 2),
                        "trend_db_per_hour": round(trend, 3),
                        "hours_to_failure": round(hours_to_failure, 1),
                        "confidence": round(confidence, 2),
                    })
        risks.sort(key=lambda r: r["hours_to_failure"])
        return risks[:max_results]


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

ToolFunc = Callable[..., Any]


def _estado_label_legacy(c: Dict) -> str:
    """Estado legible de la ONU para el tool legacy `lookup_client`."""
    try:
        estado = int(c.get("estado_onu", -1))
    except (TypeError, ValueError):
        estado = -1
    if estado == 3:
        return "Activo"
    if estado in (1, 4, 6):
        return "Caído"
    return "N/A"


class ToolRegistry:
    """Maps enabled instruction ids -> available tools."""

    # instruction_id -> list of tool names it gates
    RULE_GATES = {
        "syslog_monitor": ["get_syslog_events", "get_syslog_by_device", "get_syslog_summary"],
        "detect_anomaly": ["get_anomalies"],
        "predict_failure": ["predict_failures"],
        "check_ping_status": ["get_ping_status"],
        "client_lookup": ["lookup_client", "lookup_client_full",
                          "clients_by_nap", "clients_by_zone",
                          "mismatched_locations", "group_analysis"],
        "zone_grouping": ["clients_on_pon", "group_analysis"],
        "check_interface": ["get_interface_status"],
        "daily_summary": ["get_network_summary"],
        "llm_analyze": ["get_olt_status"],
        "send_telegram": ["send_alert"],
    }
    # Tools always available (agent infrastructure/memory)
    ALWAYS = ["get_recent_events", "get_syslog_events", "get_syslog_summary"]

    def __init__(self, reader: InfluxReader, client_lookup, event_store,
                 alert_sender, client_search=None):
        self.reader = reader
        self.events = event_store
        self.alert_sender = alert_sender  # callable (message, kind) -> bool
        self.client_search = client_search  # ClientSearchService (opcional)
        # client_lookup se conserva por compatibilidad de firma pero ya no se
        # usa: las consultas de clientes van sobre la base enriquecida (Influx).
        self.lookup = client_lookup

    # ---- handlers ---------------------------------------------------------

    def _get_network_summary(self, **_kwargs) -> Dict:
        return self.reader.overall_stats()

    def _get_anomalies(self, olt_name: Optional[str] = None, hours: int = 24,
                       limit: int = 50) -> Dict:
        items = self.reader.anomalies(olt_name=olt_name, hours=hours, limit=limit)
        return {"count": len(items), "anomalies": items}

    def _get_ping_status(self, **_kwargs) -> Dict:
        return self.reader.ping_status()

    def _predict_failures(self, hours: int = 24, max_results: int = 20) -> Dict:
        items = self.reader.predict_failures(hours=hours, max_results=max_results)
        return {"count": len(items), "predictions": items}

    def _lookup_client(self, serial: Optional[str] = None,
                       name: Optional[str] = None) -> Dict:
        if serial:
            rows = self.reader.enriched_clients(search=str(serial).strip(), limit=10)
            serial_u = str(serial).strip().upper()
            exact = [c for c in rows
                     if (c.get("serial_onu") or "").upper() == serial_u]
            if not exact and rows:
                exact = rows
            if not exact:
                return {"found": False, "query": {"serial": serial}}
            c = exact[0]
            return {"found": True, "client": {
                "nombre": c.get("nombre", ""), "serial_onu": c.get("serial_onu", ""),
                "direccion": c.get("direccion", ""), "nodo": c.get("nodo", ""),
                "puerto_pon": c.get("pon_port", ""),
                "estado": _estado_label_legacy(c),
            }}
        if name:
            clients = self.reader.enriched_clients(search=name, limit=10)
            return {"found": bool(clients), "count": len(clients), "clients": [
                {"nombre": c.get("nombre", ""), "serial_onu": c.get("serial_onu", ""),
                 "direccion": c.get("direccion", ""), "nodo": c.get("nodo", ""),
                 "puerto_pon": c.get("pon_port", ""),
                 "estado": _estado_label_legacy(c)}
                for c in clients
            ]}
        return {"found": False, "message": "Provide 'serial' or 'name'"}

    # ---- clientes enriquecidos (3 fuentes) --------------------------------

    def _client_to_dict(self, c: Dict) -> Dict:
        try:
            estado = int(c.get("estado_onu", -1))
        except (TypeError, ValueError):
            estado = -1
        online = estado == 3
        return {
            "nombre": c.get("nombre", ""),
            "documento": c.get("documento", ""),
            "usuario_pppoe": c.get("usuario_pppoe", ""),
            "serial_onu": c.get("serial_onu", ""),
            "mac": c.get("mac", ""),
            "olt": c.get("olt", ""),
            "nodo": c.get("nodo", ""),
            "nap": c.get("nap", ""),
            "zona": c.get("zona", ""),
            "pon_port": c.get("pon_port", ""),
            "estado_onu": estado,
            "online": online,
            "coincidencia": c.get("coincidencia", ""),
            "ubicacion_descuadrada": c.get("ubicacion_descuadrada") == "true",
            "ultima_medicion": c.get("_time") or c.get("time"),
        }

    def _lookup_client_full(self, serial: Optional[str] = None,
                            name: Optional[str] = None,
                            documento: Optional[str] = None) -> Dict:
        if documento:
            c = self.reader.client_by_documento(documento)
            if not c:
                return {"found": False, "query": {"documento": documento}}
            return {"found": True, "client": self._client_to_dict(c)}
        if serial:
            clients = self.reader.enriched_clients(search=serial, limit=10)
            exact = [c for c in clients
                     if (c.get("serial_onu") or "").upper()
                     == str(serial).strip().upper()]
            if not exact and clients:
                exact = clients
            return {"found": bool(exact), "query": {"serial": serial},
                    "count": len(exact),
                    "clients": [self._client_to_dict(c) for c in exact[:10]]}
        if name:
            clients = self.reader.enriched_clients(search=name, limit=10)
            return {"found": bool(clients), "query": {"name": name},
                    "count": len(clients),
                    "clients": [self._client_to_dict(c) for c in clients]}
        return {"found": False,
                "message": "Provide 'serial', 'name' or 'documento'"}

    def _clients_by_nap(self, nap: str, limit: int = 50) -> Dict:
        clients = self.reader.enriched_clients(nap=nap, limit=limit)
        down = [c for c in clients if self._client_to_dict(c)["online"] is False]
        return {"nap": nap, "count": len(clients),
                "onu_down": len(down),
                "clientes_down": [self._client_to_dict(c) for c in down[:20]],
                "clients": [self._client_to_dict(c) for c in clients[:limit]]}

    def _clients_by_zone(self, zona: str, limit: int = 50) -> Dict:
        clients = self.reader.enriched_clients(zona=zona, limit=limit)
        down = [c for c in clients if self._client_to_dict(c)["online"] is False]
        return {"zona": zona, "count": len(clients),
                "onu_down": len(down),
                "clientes_down": [self._client_to_dict(c) for c in down[:20]],
                "clients": [self._client_to_dict(c) for c in clients[:limit]]}

    def _mismatched_locations(self, limit: int = 50) -> Dict:
        clients = self.reader.enriched_clients(mismatched=True, limit=limit)
        return {"count": len(clients),
                "mismatched": [self._client_to_dict(c) for c in clients]}

    def _clients_on_pon(self, olt_name: str, pon_port: str) -> Dict:
        rows = self.reader.enriched_clients(olt=olt_name, limit=100)
        pon = str(pon_port).strip().lower()
        clients = [c for c in rows
                   if pon in (c.get("pon_port") or "").lower()]
        return {"olt": olt_name, "pon_port": pon_port, "count": len(clients),
                "clients": [{"nombre": c.get("nombre", ""),
                             "serial_onu": c.get("serial_onu", ""),
                             "direccion": c.get("direccion", "")}
                            for c in clients[:20]]}

    def _get_olt_status(self, limit: int = 30) -> Dict:
        items = self.reader.olt_status(limit=limit)
        return {"count": len(items), "olts": items}

    def _get_interface_status(self, device_name: Optional[str] = None,
                              top: int = 15) -> Dict:
        items = self.reader.interface_status(device_name=device_name, top=top)
        return {"count": len(items), "interfaces": items}

    def _get_recent_events(self, limit: int = 10, **_kwargs) -> Dict:
        items = self.events.recent(limit=limit)
        return {"count": len(items), "events": items}

    def _group_analysis(self, serials=None, olt_name=None, pon_port=None,
                        nap=None) -> Dict:
        """Análisis de un grupo de clientes (WISP+Influx vía servicio)."""
        if not self.client_search:
            return {"ok": False,
                    "error": "ClientSearchService no disponible en este agente"}
        try:
            return self.client_search.group_analysis(
                serials=serials or None, olt_name=olt_name or None,
                pon_port=pon_port or None, nap=nap or None)
        except Exception as e:
            logger.warning(f"group_analysis falló: {e}")
            return {"ok": False, "error": f"group_analysis falló: {e}"}


    # ---------------------------- SYSLOG TOOLS ----------------------------

    def _get_redis_syslog(self):
        try:
            return redis.Redis(host='127.0.0.1', port=6380, db=0, decode_responses=True)
        except Exception as e:
            logger.warning(f'No se pudo conectar a redis-syslog: {e}')
            return None

    def _get_syslog_events(self, device_ip: Optional[str] = None,
                           device_name: Optional[str] = None,
                           device_type: Optional[str] = None,
                           site: Optional[str] = None,
                           severity: Optional[str] = None,
                           minutes: int = 60,
                           limit: int = 100,
                           search: Optional[str] = None) -> Dict:
        r = self._get_redis_syslog()
        if not r:
            return {"ok": False, "error": "redis-syslog no disponible"}
        try:
            now = time.time()
            cutoff = now - minutes * 60
            items = r.zrangebyscore('syslog:recent', cutoff, now)
            out = []
            for s in items:
                try:
                    d = json.loads(s)
                except Exception:
                    continue
                if device_ip and str(d.get('device_ip')) != str(device_ip):
                    continue
                if device_name:
                    dn = str(d.get('device_name','')).lower()
                    if str(device_name).lower() not in dn and dn != str(device_name).lower():
                        continue
                if device_type and str(d.get('device_type')) != str(device_type):
                    continue
                if site and str(d.get('site','')).lower() != str(site).lower():
                    continue
                if severity and str(d.get('severity','')).lower() != str(severity).lower():
                    continue
                if search:
                    st = search.lower()
                    blob = ' '.join([str(d.get('message','')), str(d.get('tag','')), str(d.get('program',''))]).lower()
                    if st not in blob:
                        continue
                out.append(d)
            out = sorted(out, key=lambda x: x.get('timestamp',''), reverse=True)
            return {"ok": True, "count": len(out[:limit]), "events": out[:limit], "window_minutes": minutes}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_syslog_by_device(self, device_ip: Optional[str] = None,
                              device_name: Optional[str] = None,
                              minutes: int = 120,
                              limit: int = 200) -> Dict:
        r = self._get_redis_syslog()
        if not r:
            return {"ok": False, "error": "redis-syslog no disponible"}
        try:
            now = time.time()
            cutoff = now - minutes * 60
            items = r.zrangebyscore('syslog:recent', cutoff, now)
            out = []
            for s in items:
                try:
                    d = json.loads(s)
                except Exception:
                    continue
                if device_ip and str(d.get('device_ip')) != str(device_ip):
                    continue
                if device_name and str(device_name).lower() not in str(d.get('device_name','')).lower():
                    continue
                out.append(d)
            out = sorted(out, key=lambda x: x.get('timestamp',''), reverse=True)
            return {"ok": True, "device_ip": device_ip, "device_name": device_name, "count": len(out[:limit]), "events": out[:limit]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_syslog_summary(self, minutes: int = 60, limit: int = 30) -> Dict:
        r = self._get_redis_syslog()
        if not r:
            return {"ok": False, "error": "redis-syslog no disponible"}
        try:
            now = time.time()
            cutoff = now - minutes * 60
            items = r.zrangebyscore('syslog:recent', cutoff, now)
            events = []
            for s in items:
                try:
                    events.append(json.loads(s))
                except Exception:
                    continue
            by_sev = {}
            by_dev = {}
            for e in events:
                sev = e.get('severity') or 'unknown'
                by_sev[sev] = by_sev.get(sev, 0) + 1
                dn = e.get('device_name') or e.get('device_ip') or 'unknown'
                by_dev[dn] = by_dev.get(dn, 0) + 1
            recent = sorted(events, key=lambda x: x.get('timestamp',''), reverse=True)[:limit]
            return {"ok": True, "window_minutes": minutes, "total": len(events), "by_severity": by_sev, "by_device": by_dev, "recent": recent}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _send_alert(self, title: str, severity: str = "warning",
                    message: str = "", channel: str = "telegram") -> Dict:
        if not title:
            return {"ok": False, "error": "title is required"}
        sev = severity if severity in ("info", "warning", "critical") else "warning"
        ok = self.alert_sender(title=title, severity=sev, message=message,
                               channel=channel)
        return {"ok": ok, "title": title, "severity": sev, "channel": channel}

    # ---- schemas ----------------------------------------------------------

    def schemas(self) -> List[Dict]:
        return [self._schema(name, params) for name, params in _TOOL_PARAMS.items()]

    def _schema(self, name: str, params: Dict) -> Dict:
        description = _TOOL_DESCRIPTIONS.get(name, "")
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": params,
            },
        }

    # ---- build ------------------------------------------------------------

    def build(self, enabled_instruction_ids: List[str],
              enabled_tools_override: Optional[List[str]] = None
              ) -> Tuple[List[Dict], Dict[str, ToolFunc]]:
        """Return (schemas, dispatch) for the currently enabled rules."""
        allowed = set(self.ALWAYS)
        for inst_id in enabled_instruction_ids:
            allowed.update(self.RULE_GATES.get(inst_id, []))

        if enabled_tools_override:
            allowed &= set(enabled_tools_override)
            allowed.update(self.ALWAYS)

        dispatch: Dict[str, ToolFunc] = {}
        for name in sorted(allowed):
            handler = self._handler(name)
            if handler:
                dispatch[name] = handler

        schemas = [self._schema(name, _TOOL_PARAMS[name])
                   for name in sorted(allowed)
                   if name in _TOOL_PARAMS and name in dispatch]
        return schemas, dispatch

    def _handler(self, name: str) -> Optional[ToolFunc]:
        handlers = {
            "get_network_summary": self._get_network_summary,
            "get_anomalies": self._get_anomalies,
            "get_ping_status": self._get_ping_status,
            "predict_failures": self._predict_failures,
            "lookup_client": self._lookup_client,
            "clients_on_pon": self._clients_on_pon,
            "get_olt_status": self._get_olt_status,
            "get_interface_status": self._get_interface_status,
            "get_recent_events": self._get_recent_events,
            "send_alert": self._send_alert,
            "lookup_client_full": self._lookup_client_full,
            "clients_by_nap": self._clients_by_nap,
            "clients_by_zone": self._clients_by_zone,
            "mismatched_locations": self._mismatched_locations,
            "group_analysis": self._group_analysis,
            "get_syslog_events": self._get_syslog_events,
            "get_syslog_by_device": self._get_syslog_by_device,
            "get_syslog_summary": self._get_syslog_summary,
        }
        return handlers.get(name)


_TOOL_DESCRIPTIONS = {
    "get_network_summary": (
        "Resumen global de la red FTTH: cantidad de OLTs, ONUs online/offline, "
        "alertas de potencia baja y hosts caidos por ping. Usalo primero para "
        "tener una vision general."
    ),
    "get_anomalies": (
        "Lista ONUs con potencia optica baja (< -28 dBm) que constituyen "
        "anomalias o riesgos. Opcional: filtrar por olt_name, horas de ventana y limite."
    ),
    "get_ping_status": (
        "Lista de hosts (OLTs/MikroTiks) sin respuesta (down) y con respuesta "
        "(up) segun el monitoreo ICMP reciente."
    ),
    "predict_failures": (
        "Prediccion de fallas por tendencia: ONUs cuya potencia esta degradandose "
        "y podrian fallar pronto (horas estimadas a falla y confianza)."
    ),
    "lookup_client": (
        "Busca un cliente por serial de ONU (serial) o por nombre (name). "
        "Devuelve nombre, direccion, nodo y puerto PON."
    ),
    "clients_on_pon": (
        "Lista los clientes conectados a una OLT:PON especifica. "
        "Usar para evaluar alcance de una falla. Params: olt_name, pon_port."
    ),
    "get_olt_status": (
        "Estado de las OLTs: CPU, RAM, temperatura y ONUs online/total por OLT."
    ),
    "get_interface_status": (
        "Trafico de interfaces de MikroTik/OLT ordenado por uso (rx+tx bps). "
        "Opcional filtrar por device_name."
    ),
    "get_recent_events": (
        "Eventos recientes generados por el agente LLM (alertas y analisis). "
        "Usar para no repetir alertas ya emitidas."
    ),
    "send_alert": (
        "ENVIA una alerta/notificacion (Telegram y dashboard) cuando detectes un "
        "problema real que merezca atencion. Params: title (obligatorio), severity "
        "(info|warning|critical), message (detalle), channel. Usalo con moderacion "
        "y solo cuando haya evidencia."
    ),
    "lookup_client_full": (
        "Busca un cliente en la base enriquecida (WISP + OLT + PPPoE) por serial "
        "de ONU, nombre o documento. Devuelve ubicacion fisica (OLT/PON/NAP/zona), "
        "estado de la ONU y flag de ubicacion descuadrada. Es mas completo que "
        "lookup_client: usa este para ficha completa."
    ),
    "clients_by_nap": (
        "Lista los clientes agrupados por NAP (tablero/caja de distribucion). "
        "Incluye ONUs caidas y estados. Usarlo para evaluar el alcance de una "
        "falla de NAP (sobre todo si hay muchos clientes caidos juntos)."
    ),
    "clients_by_zone": (
        "Lista los clientes agrupados por zona. Incluye ONUs caidas y estados. "
        "Usarlo para inforar sobre cortes que afectan una zona."
    ),
    "mismatched_locations": (
        "Clientes con ubicacion descuadrada: el serial registrado en WISP no "
        "coincide con el serial fisico de la ONU en la OLT del mismo PON/MAC. "
        "Indica posibles errores de installacion o reasignacion."
    ),
    "group_analysis": (
        "Analiza un grupo de clientes para evaluar el alcance de una falla. "
        "Entrada: lista de seriales explicitos (serials), o descubrir el grupo "
        "por OLT/PON (olt_name + pon_port) o por NAP (nap). Devuelve total, "
        "afectados con direccion, perdida de senal (loss), baja potencia "
        "(low_power) y distribucion de estados. Usalo cuando haya varias ONUs "
        "cayendo juntas para saber cuantos clientes y quienes estan afectados."
    ),
    "get_syslog_events": (
        "Obtiene eventos recientes de syslog de MikroTik/OLTs (enriquecidos con "
        "nombre del equipo, tipo, sitio). Permite filtrar por device_ip, device_name, "
        "device_type (mikrotik/olt), severity, desde hace N minutos, limite y busqueda "
        "por texto en mensaje/tag. Usalo para investigar incidentes o responder "
        "'¿Qué ha pasado?' por equipo/red."
    ),
    "get_syslog_by_device": (
        "Eventos syslog recientes de un dispositivo concreto (por IP o nombre). "
        "Ideal para ver el histórico detallado de un MikroTik/OLT específico."
    ),
    "get_syslog_summary": (
        "Resumen de eventos syslog en los últimos minutos: conteo por severidad, "
        "por equipo, top mensajes y últimos eventos relevantes. Úsalo para tener "
        "visión rápida de actividad/alertas."
    ),
}

_TOOL_PARAMS: Dict[str, Dict] = {
    "get_network_summary": {"type": "object", "properties": {}, "required": []},
    "get_anomalies": {
        "type": "object",
        "properties": {
            "olt_name": {"type": "string", "description": "Filtrar por nombre de OLT"},
            "hours": {"type": "integer", "description": "Horas de ventana (default 24)"},
            "limit": {"type": "integer", "description": "Maximo de resultados (default 50)"},
        },
    },
    "get_ping_status": {"type": "object", "properties": {}, "required": []},
    "predict_failures": {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "description": "Horas de historial (default 24)"},
            "max_results": {"type": "integer", "description": "Maximo de resultados (default 20)"},
        },
    },
    "lookup_client": {
        "type": "object",
        "properties": {
            "serial": {"type": "string", "description": "Serial de ONU (ej. OEMT...) o numero de casilla"},
            "name": {"type": "string", "description": "Nombre o parte del nombre del cliente"},
        },
    },
    "clients_on_pon": {
        "type": "object",
        "properties": {
            "olt_name": {"type": "string", "description": "Nombre de la OLT (ej. OLT-SISAL-1)"},
            "pon_port": {"type": "string", "description": "Puerto PON (ej. GPON0/3)"},
        },
        "required": ["olt_name", "pon_port"],
    },
    "get_olt_status": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Maximo de OLTs (default 30)"}},
    },
    "get_interface_status": {
        "type": "object",
        "properties": {
            "device_name": {"type": "string", "description": "Filtrar por nombre del dispositivo"},
            "top": {"type": "integer", "description": "Cuantas interfaces mostrar (default 15)"},
        },
    },
    "get_recent_events": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "Cuantos eventos (default 10)"}},
    },
    "send_alert": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Titulo breve de la alerta"},
            "severity": {"type": "string", "enum": ["info", "warning", "critical"],
                         "description": "Severidad (default warning)"},
            "message": {"type": "string", "description": "Detalle del problema y recomendacion"},
            "channel": {"type": "string", "description": "Canal de salida (default telegram)"},
        },
        "required": ["title"],
    },
    "lookup_client_full": {
        "type": "object",
        "properties": {
            "serial": {"type": "string", "description": "Serial de ONU (ej. OEMT...)"},
            "name": {"type": "string", "description": "Nombre o parte del nombre del cliente"},
            "documento": {"type": "string", "description": "Documento del cliente"},
        },
    },
    "clients_by_nap": {
        "type": "object",
        "properties": {
            "nap": {"type": "string", "description": "Identificador del NAP (ej. NAP-12)"},
            "limit": {"type": "integer", "description": "Maximo de resultados (default 50)"},
        },
        "required": ["nap"],
    },
    "clients_by_zone": {
        "type": "object",
        "properties": {
            "zona": {"type": "string", "description": "Nombre de la zona"},
            "limit": {"type": "integer", "description": "Maximo de resultados (default 50)"},
        },
        "required": ["zona"],
    },
    "mismatched_locations": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximo de resultados (default 50)"},
        },
    },
    "group_analysis": {
        "type": "object",
        "properties": {
            "serials": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista de seriales de ONU del grupo (opcional si se da olt/pon o nap)",
            },
            "olt_name": {"type": "string", "description": "Nombre de la OLT (ej. OLT-SISAL-1)"},
            "pon_port": {"type": "string", "description": "Puerto PON (ej. GPON0/3)"},
            "nap": {"type": "string", "description": "NAP (caja de distribucion, ej. I01313)"},
        },
        "required": [],
    },
    "get_syslog_events": {
        "type": "object",
        "properties": {
            "device_ip": {"type": "string"},
            "device_name": {"type": "string"},
            "device_type": {"type": "string", "enum": ["mikrotik", "olt", "unknown"]},
            "site": {"type": "string"},
            "severity": {"type": "string"},
            "minutes": {"type": "integer", "description": "Minutos hacia atrás (default 60)"},
            "limit": {"type": "integer", "description": "Máximo resultados (default 100)"},
            "search": {"type": "string", "description": "Buscar en message/tag/program"}
        },
        "required": []
    },
    "get_syslog_by_device": {
        "type": "object",
        "properties": {
            "device_ip": {"type": "string"},
            "device_name": {"type": "string"},
            "minutes": {"type": "integer", "description": "Minutos hacia atrás (default 120)"},
            "limit": {"type": "integer", "description": "Máximo resultados (default 200)"}
        },
        "required": []
    },
    "get_syslog_summary": {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Minutos hacia atrás (default 60)"},
            "limit": {"type": "integer", "description": "Límite de eventos recientes (default 30)"}
        },
        "required": []
    },
}
