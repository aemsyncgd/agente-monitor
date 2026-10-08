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
_MAX_BATCH = 20
_HISTORY_DAYS = 30

# Campos de texto de `clientes_enriquecidos` que aportan datos de contacto.
# Se piden de una sola vez porque comparten tags con el resto de la medición.
# Se excluyen a propósito los numéricos (estado_onu, precio_usd, velocidades):
# el filtro `!= ""` que descarta nulls rompería el tipo en esos registros.
_ENRICHED_TEXT_FIELDS = (
    "nombre", "direccion", "telefono", "email", "ip_servicio",
    "plan", "pppoe_status", "tecnologia", "tipo_documento", "puerto_pon",
    "clave_pppoe",
)


def _flux_str(v: str) -> str:
    """Literal de cadena para Flux.

    No se puede usar `re.escape`: ahí los espacios salen como `\\ ` y el
    texto buscado deja de coincidir con el valor real.
    """
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _norm_serial(v: Optional[str]) -> str:
    return re.sub(r"\s+", "", str(v or "")).upper()


def _norm_mac(v: Optional[str]) -> str:
    return re.sub(r"[\s:.-]", "", str(v or "")).lower()


def _looks_like_name(v: Optional[str]) -> bool:
    """True si el término es un nombre (contiene espacios y letras).

    Los seriales ONU y documentos no llevan espacios, así que la búsqueda por
    nombre queda separada de la indexada por tags.
    """
    s = str(v or "").strip()
    return " " in s and bool(re.search(r"[A-Za-z]", s))


_MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}[:\-\s]){5}[0-9a-fA-F]{2}$")

def _is_mac(v: Optional[str]) -> bool:
    """Detecta el FORMATO de una MAC (6 pares hex separados).

    No se puede deducir del largo: un serial ONU de 12 caracteres seria
    indistinguible de una MAC sin separadores.
    """
    s = str(v or "").strip()
    return bool(_MAC_RE.match(s)) or (
        len(_norm_mac(s)) == 12 and bool(re.fullmatch(r"[0-9a-fA-F]{12}", _norm_mac(s)))
        and ":" not in s
    )


def _iso(v: Any) -> str:
    """Normaliza a ISO-8601; acepta datetime o string ya formateado."""
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
            f'|> filter(fn: (r) => r["onu_serial"] =~ '
            f'/(?i)^{re.escape(_norm_serial(serial))}$/) '
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
        """Resuelve serial ONU desde onu_mac (tag mac, field onu_serial).

        El colector OLT escribe la MAC en MAYÚSCULAS mientras el WISP y el
        usuario la escriben en minúsculas: se compara normalizando ambos lados
        para que el cruce no dependa del caso.
        """
        target = _norm_mac(mac)
        if not target:
            return None
        mac_re = ":".join([target[i:i + 2] for i in range(0, len(target), 2)])
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "onu_mac") '
            f'|> filter(fn: (r) => r["mac"] =~ /(?i)^({mac_re})$/) '
            f'|> filter(fn: (r) => r["_field"] == "onu_serial") '
            f'|> last()'
        )
        rows = self._query(flux)
        for r in rows:
            serial = _norm_serial(r.get("value"))
            if serial:
                return serial
        return None

    def _mac_by_serial(self, serial: str, hours: int = 48) -> Optional[str]:
        """Resuelve la MAC aprendida del cliente desde la base enriquecida
        (tag `mac` para cada field) o desde onu_location (tag mac) si la base
        via es la unica con dato. Usado como respaldo cuando el WISP no entrega
        la MAC en el look-up por serial/documento."""
        bucket = self.influx.bucket
        serialn = _norm_serial(serial)
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos") '
            f'|> filter(fn: (r) => r["serial_onu"] == "{serialn}") '
            f'|> last()'
        )
        rows = self._query(flux)
        for r in rows:
            mac = str(r.get("mac") or "").strip().lower()
            if mac:
                return mac
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "onu_location") '
            f'|> filter(fn: (r) => r["onu_serial"] == "{serialn}") '
            f'|> last()'
        )
        rows = self._query(flux)
        for r in rows:
            mac = str(r.get("mac") or "").strip().lower()
            if mac:
                return mac
        return None

    def _serial_by_nombre(self, nombre: str, hours: int = 24) -> Optional[str]:
        """Resuelve un serial candidato por nombre (substring case-insensitive
        sobre la base enriquecida). Ventana corta (24h: el enriquecimiento
        refresca cada 15min) y distinct() por serial: `group(columns:
        ["_field"]) |> last()` colapsa a UNA fila global por campo y devolvia
        un serial arbitrario."""
        bucket = self.influx.bucket
        flux = (
            f'import "strings"\n'
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos") '
            f'|> filter(fn: (r) => r["_field"] == "nombre") '
            f'|> filter(fn: (r) => strings.containsStr('
            f'v: strings.toLower(v: r["_value"]), '
            f'substr: strings.toLower(v: "{re.escape(str(nombre))}"))) '
            f'|> group() |> distinct(column: "serial_onu")'
        )
        rows = self._query(flux)
        serials = set()
        for r in rows:
            serial = _norm_serial(r.get("serial_onu") or r.get("value"))
            if serial:
                serials.add(serial)
        if serials:
            return sorted(serials)[0]
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
        elif nombre:
            eff_serial = self._serial_by_nombre(str(nombre)) or ""
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
        if wisp_cli is not None and not any(
                wisp_cli.get(k) for k in
                ("documento", "nombre", "serial_onu", "usuario_pppoe",
                 "mac", "direccion", "telefono")):
            wisp_cli = None
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

        if not mac and ficha["serial_onu"]:
            influx_mac = self._mac_by_serial(ficha["serial_onu"])
            if influx_mac:
                ficha["mac"] = influx_mac
                src.append("influx")

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
                f'|> filter(fn: (r) => r["_field"] == "status") '
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
                # Sin `keep`+`distinct` (y sin `group()` previo) la consulta
                # llega vacía: la clave de grupo se queda sin columnas de datos
                # y no se emite ningún registro.
                f'|> keep(columns: ["serial_onu"]) '
                f'|> distinct(column: "serial_onu")'
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

        # Un único find_many() en lugar de N find(): mismo dato, coste
        # constante en consultas y sin llamadas WISP por cliente.
        try:
            res = self.find_many(list(list_serials[:_MAX_GROUP]),
                                 include_history=False, include_infra=False)
        except Exception as e:
            logger.warning(f"find_many() falló para el grupo: {e}")
            res = {"results": []}
        fichas = [r["ficha"] for r in res.get("results", [])
                  if r.get("found") and r.get("ficha")]
        truncated = bool(res.get("truncated")) or len(list_serials) > _MAX_GROUP

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

    # ============================================================== batch === #
    # Búsqueda multi-cliente con coste de consultas CONSTANTE: el número de
    # consultas a Influx no depende del número de clientes, solo del número de
    # etapas (enriquecido -> óptico -> histórico -> infraestructura).

    def _optical_by_serials(self, serials: List[str],
                            hours: int = 48) -> Dict[str, Dict[str, Any]]:
        """Último valor de cada campo óptico para N seriales en UNA consulta."""
        if not serials:
            return {}
        alt = "|".join(re.escape(_norm_serial(s)) for s in serials)
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "optical_power") '
            f'|> filter(fn: (r) => r["onu_serial"] =~ /(?i)^({alt})$/) '
            f'|> group(columns: ["onu_serial", "_field"]) '
            f'|> last()'
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in self._query(flux):
            sn = _norm_serial(r.get("onu_serial"))
            if not sn:
                continue
            row = out.setdefault(sn, {})
            field = r.get("field")
            if field == "status":
                row["status"] = _as_int(r.get("value"))
                if row.get("status") is None:
                    row["status"] = r.get("value")
            elif field in ("rx_power", "tx_power"):
                row[field] = r.get("value")
            for tag in ("olt_name", "olt_ip", "pon_port", "onu_index",
                        "onu_model", "nodo"):
                if not row.get(tag) and r.get(tag):
                    row[tag] = r[tag]
            if r.get("time"):
                row["time"] = _iso(r["time"])
        return out

    def _history_30d(self, serials: List[str], days: int = 30
                     ) -> Dict[str, Dict[str, Any]]:
        """Histórico de estado de N seriales en UNA consulta agregada."""
        if not serials:
            return {}
        alt = "|".join(re.escape(_norm_serial(s)) for s in serials)
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{days}d) '
            f'|> filter(fn: (r) => r["_measurement"] == "optical_power") '
            f'|> filter(fn: (r) => r["_field"] == "status") '
            f'|> filter(fn: (r) => r["onu_serial"] =~ /(?i)^({alt})$/) '
            f'|> aggregateWindow(every: 1h, fn: last, createEmpty: false)'
        )
        buckets: Dict[str, List[Dict]] = {}
        for r in self._query(flux):
            sn = _norm_serial(r.get("onu_serial"))
            if sn:
                buckets.setdefault(sn, []).append({
                    "time": _iso(r.get("time")),
                    "status": _as_int(r.get("value")),
                })
        out: Dict[str, Dict[str, Any]] = {}
        for sn, samples in buckets.items():
            samples.sort(key=lambda s: s["time"] or "")
            down = [s for s in samples if s["status"] in _LOS_DOWN]
            dias = sorted({(s["time"] or "")[:10] for s in down if s["time"]})
            out[sn] = {
                "dias_ventana": days,
                "muestras_total": len(samples),
                "muestras_offline": len(down),
                "dias_offline": len(dias),
                "porcentaje_offline": round(
                    100.0 * len(down) / len(samples), 1) if samples else 0.0,
                "ultimo_down": down[-1]["time"] if down else "",
                "primera_caida": down[0]["time"] if down else "",
                "muestras": samples[-48:],
            }
        return out

    def _infra_by_names(self, names: List[str], hours: int = 24
                        ) -> Dict[str, Dict[str, Any]]:
        """Estado de la infra (ping_check) de las entidades instaladas.

        `ping_check` guarda 10 campos (status, latencias, pérdida, downtime...).
        Se piden todos en una sola consulta agrupando por `_field` y se
        pivotan a una fila por entidad: agrupar sin `_field` colapsaría los
        campos entre sí y `last()` devolvería una sola medida.
        """
        if not names:
            return {}
        bucket = self.influx.bucket
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "ping_check") '
            f'|> group(columns: ["name", "ip", "type", "_field"]) '
            f'|> last()'
        )
        rows = self._query(flux)
        wanted = {n.strip().upper() for n in names if n and n.strip()}
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            name = str(r.get("name") or "").strip()
            if not name or name.upper() not in wanted:
                continue
            key = name.upper()
            d = out.setdefault(key, {
                "name": name,
                "ip": r.get("ip") or "",
                "type": r.get("type") or "",
                "estado_ping": "?",
                "latencia_ms": None,
                "latencia_max_ms": None,
                "perdida_pct": None,
                "downtime_s": None,
                "ultima_medicion": _iso(r.get("time")),
            })
            field = str(r.get("field") or r.get("_field") or "")
            val = r.get("value")
            if field == "status":
                st = _as_int(val)
                d["estado_ping"] = "up" if st == 1 else ("down" if st == 0 else "?")
            elif field == "latency_ms_avg":
                d["latencia_ms"] = _as_float(val)
            elif field == "latency_ms_max":
                d["latencia_max_ms"] = _as_float(val)
            elif field == "packet_loss_pct":
                d["perdida_pct"] = _as_float(val)
            elif field == "downtime_seconds":
                d["downtime_s"] = _as_float(val)
        return out

    def find_many(self, queries: List[str], include_history: bool = True,
                  include_infra: bool = True) -> Dict[str, Any]:
        """Busca varios clientes con coste de consultas constante.

        Devuelve un resultado por entrada (en el orden de entrada) con la
        ficha esencial, histórico 30d e infraestructura relacionada. El
        número de consultas Influx es fijo (≈4) sin importar cuántos clientes
        se busquen; el coste variable se limita a la resolución WISP por
        serial, que ya está cacheada y acotada por _MAX_BATCH.
        """
        cleaned: List[str] = []
        seen_q = set()
        for q in queries or []:
            s = str(q or "").strip()
            if s and s not in seen_q:
                seen_q.add(s)
                cleaned.append(s)
        truncated = len(cleaned) > _MAX_BATCH
        cleaned = cleaned[:_MAX_BATCH]
        if not cleaned:
            return {"results": [], "total": 0, "truncated": truncated}

        # 1) Resolver cada consulta -> enriched row (1 consulta)
        resolved = self._enriched_lookup(cleaned)  # q -> enriched|None
        # 2ª pasada: identificadores/MAC sin fila o sin nombre -> ventana
        # 168h. Son filtros de tag (baratos, ~0.03s); los nombres NO se
        # re-escanean a 168h (containsStr sobre toda la medición = timeout).
        def _row_nombre(row) -> str:
            if not row:
                return ""
            return str(row.get("nombre")
                       or (row.get("_fields") or {}).get("nombre") or "")

        missing = [
            q for q in cleaned
            if not _row_nombre(resolved.get(q))
            and not _looks_like_name(q)
        ]
        if missing:
            wide = self._enriched_lookup(missing, hours=168)
            for q in missing:
                w = wide.get(q)
                if w and (_row_nombre(w) or not resolved.get(q)):
                    resolved[q] = w
        serials: List[str] = []

        def _add(sn: str) -> None:
            if sn and sn not in serials:
                serials.append(sn)

        for q in cleaned:
            row = resolved.get(q)
            sn = _norm_serial((row or {}).get("serial_onu") or "")
            if not sn:
                # fallback por nombre/documento/mac-vivo sólo si no cruzó
                sn = self._resolve_fallback(q) or ""
            if not sn:
                # Último recurso: si el término YA es un serial ONU, se consulta
                # como tal. Clientes con telemetría óptica pero sin cruce en la
                # base enriquecida deben seguir siendo visibles.
                nq = _norm_serial(q)
                if nq and not _is_mac(q) and not _looks_like_name(q):
                    sn = nq
            _add(sn)

        # 2) Óptico para todos (1 consulta)
        optical = self._optical_by_serials(serials)
        # 3) Histórico 30d para todos (1 consulta, opcional)
        history = self._history_30d(serials) if include_history else {}
        # 4) Infraestructura de OLTs implicadas (1 consulta, opcional)
        infra: Dict[str, Dict[str, Any]] = {}
        if include_infra:
            olts = []
            for sn in serials:
                o = optical.get(sn) or {}
                for cand in (o.get("olt_name"), (resolved.get(sn) or {}).get("olt")):
                    if cand and cand not in olts:
                        olts.append(cand)
            infra = self._infra_by_names(olts)

        # 5) Fusionar por consulta
        results: List[Dict[str, Any]] = []
        found = 0
        for q in cleaned:
            row = resolved.get(q) or {}
            sn = _norm_serial(row.get("serial_onu") or "") or self._resolve_fallback(q) or ""
            if not sn:
                nq = _norm_serial(q)
                if nq and not _is_mac(q) and not _looks_like_name(q):
                    sn = nq
            opt = optical.get(sn) or {}
            if not opt and not row:
                results.append({"query": q, "found": False, "ficha": None,
                                "historico_30d": {}, "infraestructura": []})
                continue
            found += 1
            ficha = self._ficha_from_parts(row=row, serial=sn, optical=opt,
                                           mac=row.get("mac") or "")
            hist = history.get(sn) or {"dias_ventana": _HISTORY_DAYS,
                                       "muestras_total": 0,
                                       "muestras_offline": 0,
                                       "dias_offline": 0,
                                       "porcentaje_offline": 0.0,
                                       "ultimo_down": "", "primera_caida": "",
                                       "muestras": []}
            devices = []
            if infra:
                for cand in (ficha.get("olt"), ficha.get("nodo")):
                    d = infra.get((cand or "").strip().upper())
                    if d and d not in devices:
                        devices.append(d)
            results.append({
                "query": q, "found": True, "ficha": ficha,
                "historico_30d": hist, "infraestructura": devices,
            })
        return {"results": results, "total": len(results),
                "found": found, "truncated": truncated}

    def _enriched_by_ident(self, idents: set, mac_q: set,
                           hours: int) -> List[Dict]:
        """Cruce por serial/documento/MAC: sólo tags (consulta indexada)."""
        parts: List[str] = []
        if idents:
            alt = "|".join(re.escape(i) for i in sorted(idents))
            # `exists` es obligatorio: hay puntos sin estos tags (coincidencia
            # olt_only) y comparar null contra un regex rompe el tipo en Flux.
            parts.append(
                f'(exists r["serial_onu"] and r["serial_onu"] =~ /(?i)^({alt})$/)')
            parts.append(
                f'(exists r["documento"] and r["documento"] =~ /(?i)^({alt})$/)')
        if mac_q:
            malts = []
            for q in mac_q:
                m = _norm_mac(q)
                malts.append(":".join(m[i:i + 2] for i in range(0, 12, 2)))
            malts_str = "|".join(re.escape(x) for x in sorted(set(malts)))
            parts.append(
                f'(exists r["mac"] and r["mac"] =~ /(?i)^({malts_str})$/)')
        if not parts:
            return []
        or_clause = "\n        or ".join(parts)
        bucket = self.influx.bucket
        # Se piden todos los campos de contacto en la MISMA consulta: comparten
        # tags, así que no cuesta consultas extra ni llamadas a WISP.
        alt_fields = "|".join(_ENRICHED_TEXT_FIELDS)
        flux = (
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos") '
            f'|> filter(fn: (r) => r["_field"] =~ /^({alt_fields})$/) '
            f'|> filter(fn: (r) => r["_value"] != "") '
            f'|> filter(fn: (r) =>\n        {or_clause}\n    ) '
            f'|> group(columns: ["serial_onu", "mac", "_field"]) '
            f'|> last()'
        )
        return self._query(flux)

    def _enriched_by_name(self, names: List[str], hours: int) -> List[Dict]:
        """Cruce por nombre: escaneo de `_value` (slow, sólo si hay nombres)."""
        if not names:
            return []
        bucket = self.influx.bucket
        cond = "\n        or ".join(
            'strings.containsStr(v: strings.toLower(v: r["_value"]), '
            f'substr: strings.toLower(v: {_flux_str(q.lower())}))'
            for q in names
        )
        flux = (
            f'import "strings"\n'
            f'from(bucket: "{bucket}") '
            f'|> range(start: -{hours}h) '
            f'|> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos") '
            f'|> filter(fn: (r) => r["_field"] == "nombre") '
            f'|> filter(fn: (r) => r["_value"] != "") '
            f'|> filter(fn: (r) =>\n        {cond}\n    ) '
            # Sólo interesa el serial: `keep`+`distinct` sin `group()` previo
            # es la forma que devuelve filas. Con `group()` la tabla se queda
            # sin columnas de datos y el cliente no ve ningún registro.
            f'|> keep(columns: ["serial_onu"]) '
            f'|> distinct(column: "serial_onu")'
        )
        return self._query(flux)

    def _enriched_lookup(self, queries: List[str], hours: int = 24
                         ) -> Dict[str, Dict[str, Any]]:
        """Cruce contra `clientes_enriquecidos` resolviendo cada término.

        Se separa deliberadamente en DOS consultas:

        - Identificadores (serial/documento/MAC): sólo filtros de *tag*, que
          Influx resuelve con el índice. 20 seriales -> 0.03s.
        - Nombres: `strings.containsStr` sobre `_value` obliga a escanear toda
          la medición; mezclado con los anteriores Provoca timeout (>30s) en
          lotes de 20. Se ejecuta sólo si hay términos con espacios, que es el
          caso raro en búsqueda masiva.

        El número de consultas es constante respecto al número de clientes en
        el camino de identificadores (el habitual).
        """
        if not queries:
            return {}
        normed = [(q, _norm_serial(q), _norm_mac(q)) for q in queries]
        idents: set = set()
        mac_q: set = set()
        names: List[str] = []
        for q, nq, _ in normed:
            if _is_mac(q):
                mac_q.add(q)
            elif _looks_like_name(q):
                names.append(q)
            elif nq and len(nq) >= 5:
                idents.add(nq)

        rows: List[Dict] = []
        if idents or mac_q:
            rows.extend(self._enriched_by_ident(idents, mac_q, hours))
        if names:
            # El nombre sólo sirve para localizar seriales. Los campos de la
            # ficha se piden después con el cruce indexado por serial: las
            # filas de esta consulta llevan el serial en `_value` (por el
            # `distinct`), así que mezclarlas taparía el nombre real.
            extra = {_norm_serial(r.get("serial_onu"))
                     for r in self._enriched_by_name(names, hours)}
            extra.discard("")
            if extra:
                rows.extend(self._enriched_by_ident(extra, set(), hours))

        # index por serial -> tags + fields, quedándonos con el valor útil
        by_serial: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            sn = _norm_serial(r.get("serial_onu"))
            if not sn:
                continue
            rec = by_serial.setdefault(sn, {"serial_onu": sn})
            fld = r.get("field")
            # Un mismo serial tiene varias variantes de tag `mac` (la del WISP
            # y la `olt_only`). Se fusiona quedándose con el valor informativo:
            # un null nunca debe tapar un nombre real.
            if fld:
                prev = rec.setdefault("_fields", {}).get(fld)
                if prev in (None, "") or (r.get("value") not in (None, "")):
                    rec["_fields"][fld] = r.get("value")
            for tag in ("documento", "mac", "nap", "nodo", "olt", "zona",
                        "usuario_pppoe", "ubicacion_descuadrada"):
                if r.get(tag) and not rec.get(tag):
                    rec[tag] = r[tag]
        out: Dict[str, Dict[str, Any]] = {}
        for q, nq, mq in normed:
            hit = None
            for sn, rec in by_serial.items():
                if sn == nq:
                    hit = rec
                    break
            if not hit:
                for sn, rec in by_serial.items():
                    if _norm_serial(rec.get("documento")) == nq:
                        hit = rec
                        break
            if not hit and _is_mac(q):
                for sn, rec in by_serial.items():
                    if _norm_mac(rec.get("mac")) == mq:
                        hit = rec
                        break
            if not hit:
                ql = q.lower()
                for sn, rec in by_serial.items():
                    nm = str(rec.get("_fields", {}).get("nombre") or "").lower()
                    if nm and ql in nm:
                        hit = rec
                        break
            if hit:
                out[q] = hit
        return out

    def _resolve_fallback(self, q: str) -> Optional[str]:
        """Resolución costosa para términos que no cruzaron en enriquecidos."""
        nq = _norm_serial(q)
        mq = _norm_mac(q)
        if _is_mac(q):
            try:
                return self._serial_by_mac(q)
            except Exception:
                return None
        if nq and re.fullmatch(r"\d{5,12}", nq):
            try:
                got = self.wisp.fetch_by_document(str(q).strip())
                if got and got.get("serial_onu"):
                    return _norm_serial(got.get("serial_onu"))
            except Exception:
                return None
        if " " in str(q).strip():
            try:
                return self._serial_by_nombre(str(q).strip())
            except Exception:
                return None
        return None

    @staticmethod
    def _ficha_from_parts(row: Dict[str, Any], serial: str,
                          optical: Dict[str, Any], mac: str) -> Dict[str, Any]:
        """Ficha esencial (OLT/PON/serial/NAP/estado/Rx/Tx) desde tags+óptico."""
        fields = (row or {}).get("_fields", {}) or {}
        ficha: Dict[str, Any] = {
            "serial_onu": serial or "",
            "olt": optical.get("olt_name") or row.get("olt") or "",
            "olt_ip": optical.get("olt_ip") or "",
            "pon_port": optical.get("pon_port") or "",
            "onu_index": optical.get("onu_index") or "",
            "onu_model": optical.get("onu_model") or "",
            "nodo": optical.get("nodo") or row.get("nodo") or "",
            "nap": row.get("nap") or "",
            "zona": row.get("zona") or "",
            "estado_onu": optical.get("status"),
            "rx_power": optical.get("rx_power"),
            "tx_power": optical.get("tx_power"),
            "ultima_medicion": optical.get("time") or "",
            "mac": _norm_mac(mac),
            "documento": row.get("documento") or "",
            "usuario_pppoe": row.get("usuario_pppoe") or "",
            "nombre": fields.get("nombre") or "",
            "direccion": fields.get("direccion") or "",
            "telefono": fields.get("telefono") or "",
            "email": fields.get("email") or "",
            "ip_servicio": fields.get("ip_servicio") or "",
            "plan": fields.get("plan") or "",
            "pppoe_status": fields.get("pppoe_status") or "",
            "clave_pppoe": fields.get("clave_pppoe") or "",
            "ubicacion_descuadrada": row.get("ubicacion_descuadrada") == "true",
        }
        if ficha["estado_onu"] is None:
            ficha["estado_onu"] = _as_int(fields.get("estado_onu"))
        ficha["source"] = "enriquecidos+optical"
        return ficha