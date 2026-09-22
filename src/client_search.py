# src/client_search.py
"""
Búsqueda vital de clientes: WISP vivo (fuente de verdad) + Influx.

No depende de la base `clientes_enriquecidos` (rota por colisión de tipos
en pivot) ni de Redis. Consulta la API de czwisp por serial/documento y
cruza con Influx (optical_power, onu_mac, onu_location) para MAC->serial
y estado óptico en vivo.

Módulo autocontenido (sin pivot Influx; fusión en Python).
"""
import logging
import time
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOS_DOWN = [1, 4, 6]
_ONLINE_VALUE = 3

_CACHE_TTL_SECONDS = 60.0
_MAX_GROUP = 20


def _norm_serial(v: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(v or "")).upper()


class ClientSearchService:
    def __init__(self, wisp, influx):
        self.wisp = wisp          # WispClient
        self.influx = influx      # InfluxClient
        self._cache: Dict[str, tuple] = {}  # key -> (ts, ficha|None)

    # ------------------------------------------------------------------ flux #

    def _query(self, flux: str) -> List[Dict]:
        return self.influx.query(flux) or []

    def _optical_by_serial(self, serial: str,
                           hours: int = 48) -> Optional[Dict]:
        """Último punto por campo de optical_power para un serial.

        Devuelve {status, rx_power, tx_power, olt_name, olt_ip, pon_port,
        onu_index, onu_model, nodo, time} o None.
        """
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "optical_power") '
            f'|> filter(fn: (r) => r["onu_serial"] == "{_norm_serial(serial)}") '
            f'|> group(columns: ["_field"]) '
            f'|> last()'
        )
        rows = self._query(flux)
        if not rows:
            return None
        out: Dict[str, Any] = {}
        for r in rows:
            f = r.get("field")
            if f == "status":
                try:
                    out["status"] = int(r.get("value"))
                except (TypeError, ValueError):
                    out["status"] = r.get("value")
            elif f == "rx_power":
                out["rx_power"] = r.get("value")
            elif f == "tx_power":
                out["tx_power"] = r.get("value")
            for tag in ("olt_name", "olt_ip", "pon_port", "onu_index",
                        "onu_model", "nodo"):
                if not out.get(tag) and r.get(tag):
                    out[tag] = r[tag]
            if not out.get("time") and r.get("time"):
                out["time"] = r["time"].isoformat() \
                    if hasattr(r["time"], "isoformat") else r["time"]
        return out

    def _serial_by_mac(self, mac: str, hours: int = 168) -> Optional[str]:
        """Resuelve serial ONU desde onu_mac (tag mac, field onu_serial)."""
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "onu_mac") '
            f'|> filter(fn: (r) => r["mac"] == "{str(mac).lower()}") '
            f'|> filter(fn: (r) => r["_field"] == "onu_serial") '
            f'|> last()'
        )
        rows = self._query(flux)
        for r in rows:
            serial = _norm_serial(r.get("value"))
            if serial:
                return serial
        return None

    def _location_by_mac(self, mac: str, hours: int = 168) -> str:
        """Devuelve la ubicación declarada (onu_location) para una MAC."""
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "onu_location") '
            f'|> filter(fn: (r) => r["mac"] == "{str(mac).lower()}") '
            f'|> last()'
        )
        rows = self._query(flux)
        if not rows:
            return ""
        return str(rows[0].get("location") or "").strip()

    # ------------------------------------------------------------------ find #

    def find(self, serial: Optional[str] = None,
             documento: Optional[str] = None,
             mac: Optional[str] = None,
             nombre: Optional[str] = None) -> Dict:
        """Ficha vital de un cliente. Fuentes: WISP vivo + Influx en vivo.

        La ficha está siempre laxa con lo que se tenga: si WISP no responde
        pero hay estado óptico, se devuelve ficha parcial con source.
        """
        cache_key = self._cache_key(serial=serial, documento=documento,
                                    mac=mac, nombre=nombre)
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if time.time() - ts < _CACHE_TTL_SECONDS:
                return {"found": cached is not None, "ficha": cached}

        wisp_cli: Optional[dict] = None
        eff_serial = _norm_serial(serial)

        if eff_serial:
            wisp_cli = self.wisp.fetch_by_serial(eff_serial)
            if wisp_cli is None:
                wisp_cli = None
        elif documento:
            try:
                fetched = self.wisp.fetch_by_document(str(documento).strip())
            except Exception as e:
                logger.warning(f"WISP fetch_by_document({documento}) falló: {e}")
                fetched = None
            if fetched:
                wisp_cli = fetched
                eff_serial = _norm_serial(fetched.get("serial_onu"))
        elif mac:
            eff_serial = self._serial_by_mac(str(mac).lower()) or ""
            if eff_serial:
                try:
                    wisp_cli = self.wisp.fetch_by_serial(eff_serial)
                except Exception as e:
                    logger.warning(f"WISP fetch_by_serial({eff_serial}) falló: {e}")
                    wisp_cli = None

        optical = self._optical_by_serial(eff_serial) if eff_serial else None
        ficha = self._build_ficha(wisp_cli=wisp_cli, serial=eff_serial,
                                  mac=mac, optical=optical)
        found = ficha is not None
        self._cache[cache_key] = (time.time(), ficha)
        return {"found": found, "ficha": ficha}

    @staticmethod
    def _cache_key(**parts) -> str:
        key = "|".join(f"{k}={_norm_serial(v) if isinstance(v, str) else v}"
                       for k, v in sorted(parts.items()) if v)
        return key or "empty"

    def _build_ficha(self, wisp_cli: Optional[dict], serial: str,
                     mac: Optional[str],
                     optical: Optional[Dict]) -> Optional[Dict]:
        """Fusiona WISP + Influx en una ficha vital (sin pivot)."""
        if wisp_cli is None and optical is None and not mac:
            return None

        ficha: Dict[str, Any] = {
            "documento": "", "nombre": "", "direccion": "", "telefono": "",
            "email": "", "plan": "", "tecnologia": "", "estado_servicio": "",
            "ip_servicio": "", "usuario_pppoe": "", "nap": "", "zona": "",
            "olt": "", "olt_ip": "", "pon_port": "", "onu_index": "",
            "onu_model": "", "nodo": "", "estado_onu": None,
            "rx_power": None, "tx_power": None,
            "ultima_medicion": "", "mac": "", "serial_onu": "",
        }
        src: List[str] = []
        if wisp_cli:
            ficha["documento"] = wisp_cli.get("documento") or ""
            ficha["nombre"] = wisp_cli.get("nombre") or ""
            ficha["direccion"] = wisp_cli.get("direccion") or ""
            ficha["telefono"] = wisp_cli.get("telefono") or ""
            ficha["email"] = wisp_cli.get("email") or ""
            ficha["plan"] = wisp_cli.get("plan") or ""
            ficha["tecnologia"] = wisp_cli.get("tecnologia") or ""
            ficha["estado_servicio"] = wisp_cli.get("estado") or ""
            ficha["ip_servicio"] = wisp_cli.get("ip_servicio") or ""
            ficha["usuario_pppoe"] = wisp_cli.get("usuario_pppoe") or ""
            ficha["nap"] = wisp_cli.get("nap_nombre") or ""
            ficha["zona"] = wisp_cli.get("zona") or ""
            if not mac:
                mac = wisp_cli.get("mac") or ""
            src.append("wisp")

        ficha["serial_onu"] = serial or (wisp_cli or {}).get("serial_onu") or ""
        ficha["mac"] = str(mac or "").lower()

        if optical:
            ficha["olt"] = optical.get("olt_name") or ""
            ficha["olt_ip"] = optical.get("olt_ip") or ""
            ficha["pon_port"] = optical.get("pon_port") or ""
            ficha["onu_index"] = optical.get("onu_index") or ""
            ficha["onu_model"] = optical.get("onu_model") or ""
            ficha["estado_onu"] = optical.get("status")
            ficha["rx_power"] = optical.get("rx_power")
            ficha["tx_power"] = optical.get("tx_power")
            ficha["nodo"] = optical.get("nodo") or ""
            ficha["ultima_medicion"] = optical.get("time") or ""
            if not ficha.get("zona") and ficha.get("nodo"):
                ficha["zona"] = ficha["nodo"]
            src.append("influx")

        if ficha.get("mac") and not optical:
            loc = self._location_by_mac(ficha["mac"])
            if loc:
                ficha["zona"] = loc
                src.append("onu_location")

        ficha["source"] = "+".join(sorted(set(src)) or ["poco"])
        return ficha

    # ----------------------------------------------------------------- cache #

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------- discover #

    def discover_serials(self, olt_name: Optional[str] = None,
                         pon_port: Optional[str] = None,
                         nap: Optional[str] = None) -> List[str]:
        """Seriales ONU de un grupo: OLT/PON (vivo) o NAP (base enriquecida)."""
        bucket = self.influx.bucket
        if olt_name and pon_port:
            flux = (
                f'from(bucket: "{bucket}") '
                f'|> range(start: -48h) '
                f'|> filter(fn: (r) => r["_measurement"] == "optical_power") '
                f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}") '
                f'|> filter(fn: (r) => r["pon_port"] == "{pon_port}") '
                f'|> group() |> distinct(column: "onu_serial")'
            )
            col = "onu_serial"
        elif nap:
            flux = (
                f'from(bucket: "{bucket}") '
                f'|> range(start: -168h) '
                f'|> filter(fn: (r) => r["_measurement"] == '
                f'"clientes_enriquecidos") '
                f'|> filter(fn: (r) => r["nap"] == "{nap}") '
                f'|> group() |> distinct(column: "serial_onu")'
            )
            col = "serial_onu"
        else:
            return []

        serials: List[str] = []
        seen = set()
        for r in self._query(flux):
            v = _norm_serial(r.get(col)) or _norm_serial(r.get("value"))
            if v and v not in seen:
                seen.add(v)
                serials.append(v)
        return sorted(serials)

    # --------------------------------------------------------- group_analysis #

    def group_analysis(self, serials: Optional[List[str]] = None,
                       olt_name: Optional[str] = None,
                       pon_port: Optional[str] = None,
                       nap: Optional[str] = None) -> Dict[str, Any]:
        """Análisis agregado de un grupo de clientes afectados.

        - `serials`: lista explícita (seriales y/o nombres/cédulas).
        - si no, descubre seriales por OLT/PON o NAP.
        Cada serial se enriquece con find(); los afectados llevan dirección.
        """
        scopes: List[str] = []
        if nap:
            scopes.append(f"NAP {nap}")
        if olt_name and pon_port:
            scopes.append(f"OLT {olt_name} · PON {pon_port}")

        if serials:
            list_serials: List[str] = []
            for s in serials:
                v = _norm_serial(s) if isinstance(s, str) else ""
                if v:
                    list_serials.append(v)
        else:
            list_serials = self.discover_serials(olt_name=olt_name,
                                                 pon_port=pon_port, nap=nap)
            if not scopes and list_serials:
                scopes.append(f"{len(list_serials)} seriales")

        if not list_serials:
            return {"scope": " | ".join(scopes) or "sin entradas", "total": 0,
                    "afectados": [], "offline": 0, "loss": 0,
                    "low_power": 0, "distribucion_estados": {},
                    "fichas": [], "truncado": False}

        fichas: List[Dict] = []
        truncated = False
        for i, serial in enumerate(list_serials[:_MAX_GROUP]):
            try:
                res = self.find(serial=serial)
            except Exception as e:
                logger.warning(f"find({serial}) falló: {e}")
                res = {"found": False}
            if res.get("found") and res["ficha"]:
                fichas.append(res["ficha"])
            if i and i % 3 == 0:
                time.sleep(0.05)  # espacio entre lookups WISP (best-effort)
        truncated = len(list_serials) > _MAX_GROUP

        total = len(fichas)
        est_map: Dict[str, int] = {}
        offline = 0
        loss = 0
        low_power = 0
        afectados: List[Dict[str, Any]] = []
        for f in fichas:
            e = f.get("estado_onu")
            est_map[f"estado_{e}"] = est_map.get(f"estado_{e}", 0) + 1
            rx = f.get("rx_power")
            try:
                rx_f = float(rx) if rx is not None else None
            except (TypeError, ValueError):
                rx_f = None
            if e not in (None, _ONLINE_VALUE):
                offline += 1
                if e in _LOS_DOWN:
                    loss += 1
                afectados.append({
                    "serial_onu": f.get("serial_onu") or "",
                    "nombre": f.get("nombre") or "—",
                    "direccion": f.get("direccion") or "",
                    "telefono": f.get("telefono") or "",
                    "documento": f.get("documento") or "",
                    "estado_onu": e,
                    "rx_power": rx_f,
                })
            if rx_f is not None and rx_f < -28.0:
                low_power += 1

        return {
            "scope": " | ".join(scopes) or f"{total} seriales",
            "total": total,
            "afectados": afectados,
            "offline": offline,
            "loss": loss,
            "low_power": low_power,
            "distribucion_estados": est_map,
            "fichas": fichas,
            "truncado": truncated,
        }