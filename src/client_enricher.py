# src/client_enricher.py
"""
ClientEnricher: cruza las 3 fuentes de datos para enriquecer la base de clientes.

Fuente 1: OLTs (InfluxDB `onu_location`) -> MAC + serial + OLT + PON + estado.
          `optical_power` actua como fuente de respaldo/complementaria: si una
          ONU no aparece en onu_location, se toma serial/PON/estado de alli.
Fuente 2: MikroTik RouterOS API (InfluxDB `routeros_pppoe` / `routeros_dhcp`)
          -> usuario PPPoE -> MAC.
Fuente 3: WISP (API czwisp / CSV) -> documento, serial, PPPoE, datos de servicio.

Matching (por cliente WISP):
  1. serial_onu  -> OLT                      (fuente 1, match directo)
  2. usuario_pppoe -> MAC (fuente 2) -> OLT  (match indirecto)
  3. si no cruza -> `clientes_pendientes` con causa.

Persistencia: todo lo que se descubre se guarda en la base; los atributos que no
cambian (direccion, serial, MAC, ubicacion) se conservan entre ciclos y solo se
actualizan si la fuente viva muestra un cambio real.

Persiste:
  - wisp_clients          (catálogo, tags de búsqueda)
  - clientes_enriquecidos (OLT + pon + estado + flag ubicacion_descuadrada)
  - clientes_pendientes   (con causa)

Las ONUs de la OLT que no pertenecen a ningun cliente WISP se persisten igual
como `clientes_enriquecidos` (coincidencia=olt_only) con lo que la base cubre
todo el inventario OLT aunque el catálogo WISP este incompleto o ausente.
"""
import json
import logging
import re
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


def _dni_from_pppoe(user) -> str:
    """Extrae el run de dígitos más largo de un usuario PPPoE como DNI.

    Formatos reales: `VN12345678-1` -> `12345678`; `A9876543B` -> `9876543`.
    """
    runs = re.findall(r"\d{6,}", str(user or ""))
    return max(runs, key=len) if runs else ""


def _tag(doc: dict, key: str) -> str:
    v = doc.get(key)
    return str(v).strip() if v is not None else ""


def _field(doc: dict, key: str, default: str = "") -> str:
    v = doc.get(key)
    if v is None:
        return default
    return str(v)


# ---------------------------------------------------------------------------
# Fusión de fuentes OLT (onu_location + optical_power)
# ---------------------------------------------------------------------------

def _estado_desconocido(value) -> bool:
    """True si el estado no aporta informacion (vacio, 0 o -1)."""
    s = str(value).strip() if value is not None else ""
    if s in ("", "0", "-1", "none"):
        return True
    try:
        return int(float(s)) <= 0
    except (ValueError, TypeError):
        return False


def _merge_olt_rows(location_rows: List[dict],
                    optic_rows: List[dict]) -> List[dict]:
    """Une ambas fuentes OLT preservando la fila de onu_location como
    canónica.  De optical_power solo se extraen campos que falten en la
    fila de onu_location (estado_onu/status, pon_port, onu_index, olt_name).

    Los atributos lentos (serial, olt, nodo) que ya estan en onu_location no
    se pisan nunca.
    """
    by_serial: Dict[str, dict] = {}
    no_serial: List[dict] = []

    for row in location_rows:
        row = dict(row)
        row["estado_onu"] = _field(row, "estado_onu", "")
        serial = _field(row, "onu_serial").upper()
        if serial:
            by_serial.setdefault(serial, row)
        else:
            no_serial.append(row)

    for row in optic_rows:
        serial = _field(row, "onu_serial").upper()
        if not serial:
            continue
        status = _field(row, "status", "")
        base = by_serial.get(serial)
        if base is None:
            new_row = dict(row)
            if status:
                new_row["estado_onu"] = status
            by_serial[serial] = new_row
            continue
        # Solo rellenar lo que falte en base (no pisa onu_location)
        if _estado_desconocido(base.get("estado_onu")) and status:
            base["estado_onu"] = status
        if not _tag(base, "pon_port") and _tag(row, "pon_port"):
            base["pon_port"] = _tag(row, "pon_port")
        if not _tag(base, "onu_index") and _tag(row, "onu_index"):
            base["onu_index"] = _tag(row, "onu_index")
        if not base.get("onu_model") and row.get("onu_model"):
            base["onu_model"] = row["onu_model"]

    return no_serial + list(by_serial.values())


def _build_olt_only_rows(olt_rows: List[dict],
                         exclude_serials: Optional[set] = None) -> List[dict]:
    """Construye filas OLT-only (sin cliente WISP) para un ONU determinado.
    Se descartan filas sin serial o que ya fueron matcheadas por WISP.
    """
    exclude = exclude_serials or set()
    out: List[dict] = []
    for row in olt_rows:
        serial = _field(row, "onu_serial").upper()
        if not serial or serial in exclude:
            continue
        out.append(_build_matched(
            {}, row, None, coincidencia="olt_only", descuadrado=False,
        ))
    return out


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
        "serial_onu": (_tag(client, "serial_onu").upper()
                       or _field(onu, "onu_serial").upper()),
        "mac": mac,
        "olt": _tag(onu, "olt") or _tag(onu, "olt_name"),
        "nodo": _tag(onu, "nodo"),
        "pon_port": _tag(onu, "pon_port"),
        "onu_index": _tag(onu, "onu_index"),
        "nap": _tag(client, "nap_nombre"),
        "zona": _tag(client, "zona") or _tag(onu, "nodo"),
        "estado_onu": (_field(onu, "estado_onu", "")
                       or _field(onu, "status", "")),
        "ubicacion_descuadrada": descuadrado,
        "coincidencia": coincidencia,
        "nombre": _field(client, "nombre"),
        "direccion": _field(client, "direccion"),
        "telefono": _field(client, "telefono"),
        "email": _field(client, "email"),
        "ip_servicio": _field(client, "ip_servicio"),
        "puerto": _field(client, "puerto"),
        "puerto_pon": _field(client, "puerto_pon"),
        "plan": _field(client, "plan"),
        "tecnologia": _field(client, "tecnologia"),
        "velocidad_down_mbps": _field(client, "velocidad_down_mbps"),
        "velocidad_up_mbps": _field(client, "velocidad_up_mbps"),
        "precio_usd": _field(client, "precio_usd"),
        "coordenadas": _field(client, "coordenadas"),
        "tipo_documento": _field(client, "tipo_documento"),
        "codigo_cliente": _field(client, "codigo_cliente"),
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


def _merge_catalog_live(catalog: Dict[str, dict], live: List[dict]) -> List[dict]:
    """Fusiona el catálogo de Influx con la fuente viva (lista/CSV).

    La fuente viva gana campo a campo (no vacíos) y se marca para
    re-persistirse; las entradas solo-catálogo llevan `_from_catalog` y no
    se re-escriben, preservando así su `_ts` como fecha real de refresh.
    """
    clients = list(catalog.values())
    by_serial: Dict[str, int] = {}
    by_doc: Dict[str, int] = {}
    by_user: Dict[str, int] = {}
    for i, c in enumerate(clients):
        s = _tag(c, "serial_onu").upper()
        if s:
            by_serial.setdefault(s, i)
        d = _tag(c, "documento")
        if d:
            by_doc.setdefault(d, i)
        u = _tag(c, "usuario_pppoe").lower()
        if u:
            by_user.setdefault(u, i)

    for c in live:
        i = None
        s = _tag(c, "serial_onu").upper()
        if s and s in by_serial:
            i = by_serial[s]
        elif _tag(c, "documento") and _tag(c, "documento") in by_doc:
            i = by_doc[_tag(c, "documento")]
        elif _tag(c, "usuario_pppoe") and _tag(c, "usuario_pppoe").lower() in by_user:
            i = by_user[_tag(c, "usuario_pppoe").lower()]
        if i is None:
            i = len(clients)
            clients.append(dict(c))
            if s:
                by_serial.setdefault(s, i)
            if _tag(c, "documento"):
                by_doc.setdefault(_tag(c, "documento"), i)
            if _tag(c, "usuario_pppoe"):
                by_user.setdefault(_tag(c, "usuario_pppoe").lower(), i)
        else:
            merged = dict(clients[i])
            for k, v in c.items():
                if v not in (None, ""):
                    merged[k] = v
            merged["_ts"] = time.time()
            clients[i] = merged
        clients[i].pop("_from_catalog", None)
    return clients


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
    serial_lookups: int = 0
    serial_found: int = 0
    doc_lookups: int = 0
    doc_found: int = 0
    catalog: int = 0


class ClientEnricher:
    """Ciclo de enriquecimiento: WISP + OLT + PPPoE/DHCP -> InfluxDB."""

    def __init__(self, influx: InfluxClient, wisp, config: EnrichmentConfig,
                 olt_measurement: str = "onu_location",
                 optic_measurement: str = "optical_power",
                 pppoe_measurement: str = "routeros_pppoe",
                 dhcp_measurement: str = "routeros_dhcp"):
        self.influx = influx
        self.wisp = wisp
        self.config = config
        self.olt_measurement = olt_measurement
        self.optic_measurement = optic_measurement
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

        # El catálogo vivo (list_endpoint/CSV) viene casi siempre vacío: sin
        # re-sembrarlo desde `wisp_clients` cada ciclo, la ficha parpadearía
        # (0 wisp / N olt-only) y se perdería la PII ya resuelta.
        catalog = self._load_wisp_catalog()
        live = self.wisp.list_clients() if self.wisp else []
        clients = _merge_catalog_live(catalog, live)
        stats.catalog = len(catalog)
        stats.wisp_total = len(clients)

        olt_location = self._load_rows(self.olt_measurement, "onu", last_only=True)
        olt_optic = self._load_rows(self.optic_measurement, "onu", last_only=True)
        olt_rows = _merge_olt_rows(olt_location, olt_optic)
        pppoe_rows = self._load_rows(
            self.pppoe_measurement, "pppoe",
            fields=["mac_address", "status"])
        dhcp_rows = self._load_rows(
            self.dhcp_measurement, "dhcp",
            fields=["mac_address"])

        doc_on = bool(self.wisp) and bool(
            getattr(self.wisp.config, "document_endpoint", "").strip()) \
            and hasattr(self.wisp, "fetch_by_document_ex")
        serial_on = bool(self.wisp) and bool(
            getattr(self.wisp.config, "serial_endpoint", "").strip())
        state = self._load_state() if (doc_on or serial_on) else None
        if doc_on:
            clients, stats = self._lookup_by_document(
                clients, olt_rows, pppoe_rows, stats, state=state)
        if serial_on:
            budget = None
            if stats.doc_lookups:
                cap = getattr(self.wisp.config, "max_lookups_per_run", 400)
                budget = max(cap - stats.doc_lookups, 0)
            clients, stats = self._lookup_olt_serials(
                clients, olt_rows, stats, state=state, budget=budget)
        if state is not None:
            self._save_state(state)

        logger.info(
            f"Enriquecimiento fuentes: olt={len(olt_rows)} "
            f"(location={len(olt_location)}/optical={len(olt_optic)}) "
            f"pppoe={len(pppoe_rows)} dhcp={len(dhcp_rows)} "
            f"wisp={stats.wisp_total} (catalog={stats.catalog}) "
            f"doc_lookups={stats.doc_lookups} doc_found={stats.doc_found} "
            f"serial_lookups={stats.serial_lookups} "
            f"serial_found={stats.serial_found}"
        )

        if not clients and not olt_rows:
            logger.warning("Sin clientes WISP y sin ONUs en OLT; enriquecimiento omitido")
            return stats

        matched, pending = enrich_clients(clients, olt_rows, pppoe_rows, dhcp_rows)
        matched_serials = {m.get("serial_onu", "").upper() for m in matched
                           if m.get("serial_onu")}
        olt_only = _build_olt_only_rows(olt_rows, exclude_serials=matched_serials)
        all_matched = matched + olt_only

        stats.matched = len(all_matched)
        stats.pending = len(pending)
        for p in pending:
            causa = p.get("causa", "")
            stats.by_causa[causa] = stats.by_causa.get(causa, 0) + 1
        stats.mismatched = sum(1 for m in all_matched if m.get("ubicacion_descuadrada"))

        if clients:
            self._persist_catalog(clients)
        self._persist_matched(all_matched)
        self._persist_pending(pending)

        elapsed = time.time() - start
        logger.info(
            f"Enriquecimiento: {stats.matched} enriquecidos "
            f"({len(matched)} wisp/{len(olt_only)} olt-only), {stats.pending} "
            f"pendientes, {stats.mismatched} descuadrados ({elapsed:.1f}s)"
        )
        return stats

# ----- lectura de fuentes -----
    def _load_rows(self, measurement: str, kind: str,
                   last_only: bool = False,
                   fields: Optional[List[str]] = None) -> List[dict]:
        # optical_power es la medicion mas pesada del bucket (millones de
        # puntos; 912MB/7d medidos). Solo se usa como respaldo en
        # _merge_olt_rows para el estado actual por ONU, asi que last() por
        # serie basta y evita saturar InfluxDB.
        #
        # Si `fields` viene, se emite una consulta `last()` POR CAMPO (rango
        # acotado + ultimo valor por serie) en lugar del barrido completo
        # multi-campo. El barrido de `onu_location` sobre -168h materializa
        # ~10.7M filas en Python (pico real de 7GB que OOM-killed al
        # scheduler); el mismo patron con 1-2 campos estables (pppoe/dhcp)
        # solo necesita el ultimo valor por serie, no el historial.
        def _flux(field: Optional[str] = None) -> str:
            lines = [
                f'from(bucket: "{self.influx.bucket}")',
                f'  |> range(start: -{self.config.lookback_hours}h)',
                f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")',
            ]
            if field:
                lines.append(f'  |> filter(fn: (r) => r["_field"] == "{field}")')
            if field or last_only:
                lines.append("  |> last()")
            lines.append('  |> yield(name: "enrich")')
            return "\n".join(lines) + "\n"

        if fields:
            results = []
            for field in fields:
                results.extend(self.influx.query(_flux(field)) or [])
        else:
            results = self.influx.query(_flux()) or []
        rows = rows_from_query(results)
        return list(rows.values())

    # ----- delta-driven: lookup WISP por serial de ONUs OLT -----
    def _lookup_olt_serials(self, clients: List[dict], olt_rows: List[dict],
                            stats: EnrichStats,
                            state: Optional[Dict] = None,
                            budget: Optional[int] = None) -> (List[dict], EnrichStats):
        """Consulta WISP por serial para ONUs OLT sin datos WISP vigentes.

        Reglas por estado en `wisp_serial_lookups`:
          - rec inexistente + sin datos -> consultar; con datos -> lo refresca
            la cadena por documento (no se duplica la llamada aquí).
          - found   -> reconsultar a las `cache_hours`.
          - notfound-> reconsultar a las `unknown_retry_hours`.
          - err     -> reconsultar a las `error_retry_hours` (5xx/red).
        `state` (dict completo del fichero) y `budget` opcionales permiten
        compartir presupuesto con la cadena por documento; sin ellos se
        carga/guarda el fichero internamente (compatibilidad con tests).
        """
        if not self.wisp:
            return clients, stats
        if not getattr(self.wisp.config, "serial_endpoint", "").strip():
            return clients, stats

        own_state = state is None
        if own_state:
            state = self._load_state()
        serial_map = state.setdefault("wisp_serial_lookups", {})

        known = {_tag(c, "serial_onu").upper() for c in clients
                 if _tag(c, "serial_onu")}
        now = time.time()
        fresh_h = getattr(self.wisp.config, "cache_hours", 24) * 3600
        unknown_h = getattr(self.wisp.config, "unknown_retry_hours", 168) * 3600
        err_h = getattr(self.wisp.config, "error_retry_hours", 1) * 3600
        cap = budget if budget is not None else getattr(
            self.wisp.config, "max_lookups_per_run", 400)

        pending_serials: List[str] = []
        for row in olt_rows:
            serial = _field(row, "onu_serial").upper()
            if not serial:
                continue
            rec = serial_map.get(serial)
            if rec is None:
                if serial in known:
                    continue
                pending_serials.append(serial)
                continue
            age = now - rec.get("ts", 0)
            if rec.get("err"):
                due = age >= err_h
            elif rec.get("found"):
                due = age >= fresh_h
            else:
                due = age >= unknown_h
            if due:
                pending_serials.append(serial)

        stats.serial_lookups = len(pending_serials)
        fetch_ex = getattr(self.wisp, "fetch_by_serial_ex", None)
        for serial in pending_serials[:cap]:
            if fetch_ex:
                client, ok = fetch_ex(serial)
            else:
                client, ok = self.wisp.fetch_by_serial(serial), True
            rec = {"ts": now, "found": bool(client)}
            if not ok:
                rec["err"] = True
                rec["found"] = False
            serial_map[serial] = rec
            if client:
                known.add(serial)
                clients.append(client)
                stats.serial_found += 1

        if own_state:
            self._save_state(state)
        logger.info(
            f"WISP lookup por serial: {len(pending_serials)} pendientes, "
            f"{stats.serial_found} encontrados (cap={cap})"
        )
        return clients, stats

    # ----- cadena principal: OLT -> MAC -> PPPoE -> DNI -> WISP -----
    def _lookup_by_document(self, clients: List[dict], olt_rows: List[dict],
                            pppoe_rows: List[dict], stats: EnrichStats,
                            state: Optional[Dict] = None,
                            budget: Optional[int] = None) -> (List[dict], EnrichStats):
        """Resuelve clientes WISP por documento (DNI) derivado de la OLT.

        Para cada serial OLT con datos WISP ausentes o vencidos toma el
        usuario PPPoE por prioridad (RB activo -> histórico -> catálogo),
        extrae el DNI y consulta `document_endpoint`. El serial OLT es la
        fuente de verdad: se impone sobre el que devuelva WISP.
        """
        if not self.wisp:
            return clients, stats
        if not getattr(self.wisp.config, "document_endpoint", "").strip():
            return clients, stats
        fetch_ex = getattr(self.wisp, "fetch_by_document_ex", None)
        if not fetch_ex:
            return clients, stats

        own_state = state is None
        if own_state:
            state = self._load_state()
        doc_map = state.setdefault("wisp_doc_lookups", {})

        cap = budget if budget is not None else getattr(
            self.wisp.config, "max_lookups_per_run", 400)
        fresh_h = getattr(self.wisp.config, "cache_hours", 24) * 3600
        unknown_h = getattr(self.wisp.config, "unknown_retry_hours", 168) * 3600
        err_h = getattr(self.wisp.config, "error_retry_hours", 1) * 3600
        now = time.time()

        idx: Dict[str, int] = {}
        client_ts: Dict[str, float] = {}
        for i, c in enumerate(clients):
            serial = _tag(c, "serial_onu").upper()
            if serial and serial not in idx:
                idx[serial] = i
                if c.get("_ts"):
                    client_ts[serial] = _ts_seconds(c["_ts"])

        # Usuario PPPoE activo por MAC (fuente 2: RB) y histórico por serial.
        pppoe_by_mac: Dict[str, str] = {}
        for row in pppoe_rows:
            mac = _norm_mac(_field(row, "mac_address"))
            user = _tag(row, "username").lower()
            if mac and user:
                pppoe_by_mac.setdefault(mac, user)

        olt_by_serial: Dict[str, dict] = {}
        for row in olt_rows:
            serial = _field(row, "onu_serial").upper()
            if serial:
                olt_by_serial.setdefault(serial, row)

        targets: List[str] = []
        for serial in olt_by_serial:
            rec = doc_map.get(serial)
            if rec is not None:
                age = now - rec.get("ts", 0)
                if rec.get("err"):
                    due = age >= err_h
                elif rec.get("found"):
                    due = age >= fresh_h
                else:
                    due = age >= unknown_h
                if due:
                    targets.append(serial)
                continue
            if serial in idx:
                ts = client_ts.get(serial)
                if not ts or now - ts < fresh_h:
                    # Datos frescos (o edad desconocida): sembrar el estado
                    # para no re-consultar hasta que la propia fila envejezca.
                    doc_map[serial] = {"ts": ts or now, "found": True,
                                       "via": "catalogo"}
                    continue
                # Datos viejos sin rec: cae a targets para refrescar.
            targets.append(serial)

        stats.doc_lookups = 0
        stats.doc_found = 0
        if not targets:
            if own_state:
                self._save_state(state)
            return clients, stats

        enriched_user = self._load_usuario_by_serial()

        for serial in targets:
            if stats.doc_lookups >= cap:
                break
            olt_row = olt_by_serial.get(serial) or {}
            mac = _norm_mac(_tag(olt_row, "mac"))
            user = pppoe_by_mac.get(mac, "")
            if not user:
                user = enriched_user.get(serial, "")
            if not user and serial in idx:
                user = _tag(clients[idx[serial]], "usuario_pppoe")
            dni = _dni_from_pppoe(user)
            if not dni:
                continue
            client, ok = fetch_ex(dni)
            stats.doc_lookups += 1
            prev = doc_map.get(serial) or {}
            if not ok:
                doc_map[serial] = {"ts": now, "err": True, "dni": dni,
                                   "found": bool(prev.get("found"))}
                continue
            doc_map[serial] = {"ts": now, "found": bool(client), "dni": dni}
            if not client:
                continue
            client = dict(client)
            client["serial_onu"] = serial  # la OLT manda sobre el serial WISP
            if user:
                client["usuario_pppoe"] = user
            client["_ts"] = now
            if serial in idx:
                clients[idx[serial]] = client
            else:
                idx[serial] = len(clients)
                clients.append(client)
            stats.doc_found += 1

        logger.info(
            f"WISP lookup por documento: {len(targets)} candidatos, "
            f"{stats.doc_lookups} consultas, {stats.doc_found} encontrados "
            f"(cap={cap})"
        )
        if own_state:
            self._save_state(state)
        return clients, stats

    def _load_usuario_by_serial(self) -> Dict[str, str]:
        """usuario_pppoe histórico por serial desde `clientes_enriquecidos`."""
        try:
            results = self.influx.query(
                f'from(bucket: "{self.influx.bucket}")\n'
                "  |> range(start: -168h)\n"
                '  |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")\n'
                '  |> filter(fn: (r) => r["_field"] == "nombre")\n'
                "  |> last()\n"
                '  |> yield(name: "usuarios")\n'
            ) or []
        except Exception as e:
            logger.warning(f"Usuarios históricos por serial: {e}")
            return {}
        out: Dict[str, str] = {}
        for r in results:
            serial = str(r.get("serial_onu") or "").strip().upper()
            user = str(r.get("usuario_pppoe") or "").strip().lower()
            if serial and user and serial not in out:
                out[serial] = user
        return out

    # ----- catálogo WISP persistido (re-sembrado por ciclo) -----
    def _load_wisp_catalog(self) -> Dict[str, dict]:
        """Último estado de `wisp_clients` por serial (-168h), con `_ts`.

        `nap` (tag de escritura) se aliasa a `nap_nombre` (clave de lectura
        de `enrich_clients`). Filas del mismo serial se fusionan dando
        prioridad a la más reciente y rellenando campos faltantes.
        """
        try:
            results = self.influx.query(
                f'from(bucket: "{self.influx.bucket}")\n'
                "  |> range(start: -168h)\n"
                '  |> filter(fn: (r) => r["_measurement"] == "wisp_clients")\n'
                "  |> last()\n"
                '  |> yield(name: "catalog")\n'
            ) or []
        except Exception as e:
            logger.warning(f"Catálogo WISP: lectura falló ({e}); solo fuente viva")
            return {}
        ts_by_key: Dict[str, float] = {}
        field_ts: Dict[str, Dict[str, float]] = {}
        rows: Dict[str, Dict] = {}
        for r in results:
            tags = {k: v for k, v in r.items()
                    if k not in ("measurement", "field", "value", "time")}
            key = "|".join(f"{k}={v}" for k, v in sorted(tags.items()))
            ts = _ts_seconds(r.get("time"))
            ts_by_key[key] = max(ts_by_key.get(key, 0.0), ts)
            row = rows.setdefault(key, dict(tags))
            fname = str(r.get("field", ""))
            if ts >= field_ts.setdefault(key, {}).get(fname, -1.0):
                row[fname] = r.get("value")
                field_ts[key][fname] = ts

        catalog: Dict[str, dict] = {}
        for key, row in rows.items():
            serial = str(row.get("serial_onu") or "").strip().upper()
            doc = str(row.get("documento") or "").strip()
            user = str(row.get("usuario_pppoe") or "").strip().lower()
            if not serial and not doc and not user:
                continue
            client = dict(row)
            nap = str(client.pop("nap", "") or "")
            if nap and not client.get("nap_nombre"):
                client["nap_nombre"] = nap
            client["_ts"] = ts_by_key.get(key, 0.0)
            client["_from_catalog"] = True
            map_key = (f"s:{serial}" if serial
                       else f"d:{doc}" if doc else f"u:{user}")
            prev = catalog.get(map_key)
            if prev is None:
                catalog[map_key] = client
                continue
            newer = client if client["_ts"] >= prev["_ts"] else prev
            older = prev if newer is client else client
            for k, v in older.items():
                if k in ("_ts", "_from_catalog"):
                    continue
                if not newer.get(k) and v:
                    newer[k] = v
            newer["_ts"] = max(prev["_ts"], client["_ts"])
            newer["_from_catalog"] = True
            catalog[map_key] = newer
        return catalog

    def _load_state(self) -> Dict:
        """Fichero de estado completo (serial + documento + saved_at)."""
        path = self.config.state_file
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, ValueError):
            pass
        return {}

    def _save_state(self, state: Dict) -> None:
        path = self.config.state_file
        try:
            state["saved_at"] = time.time()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"No se pudo guardar estado WISP en {path}: {e}")

    # ----- persistencia -----
    def _persist_catalog(self, clients: List[dict]) -> None:
        points = []
        for c in clients:
            # Lo re-sembrado desde Influx no se re-escribe: su `_ts` debe
            # reflejar el último refresh real (si no, nunca vencería y la
            # cadena por documento nunca re-consultaría a WISP).
            if c.get("_from_catalog"):
                continue
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
                    "email": _field(c, "email"),
                    "plan": _field(c, "plan"),
                    "tecnologia": _field(c, "tecnologia"),
                    "velocidad_down_mbps": _field(c, "velocidad_down_mbps"),
                    "velocidad_up_mbps": _field(c, "velocidad_up_mbps"),
                    "precio_usd": _field(c, "precio_usd"),
                    "estado": _tag(c, "estado"),
                    "ip_servicio": _field(c, "ip_servicio"),
                    "puerto": _field(c, "puerto"),
                    "puerto_pon": _field(c, "puerto_pon"),
                    "coordenadas": _field(c, "coordenadas"),
                    "tipo_documento": _field(c, "tipo_documento"),
                    "codigo_cliente": _field(c, "codigo_cliente"),
                },
            ))
        n = self.influx.write_points(points) if points else 0
        if points:
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
                "email": m.get("email", ""),
                "pon_port": m.get("pon_port", ""),
                "onu_index": m.get("onu_index", ""),
                "puerto": m.get("puerto", ""),
                "puerto_pon": m.get("puerto_pon", ""),
                "plan": m.get("plan", ""),
                "tecnologia": m.get("tecnologia", ""),
                "velocidad_down_mbps": m.get("velocidad_down_mbps", ""),
                "velocidad_up_mbps": m.get("velocidad_up_mbps", ""),
                "precio_usd": m.get("precio_usd", ""),
                "ip_servicio": m.get("ip_servicio", ""),
                "coordenadas": m.get("coordenadas", ""),
                "tipo_documento": m.get("tipo_documento", ""),
                "codigo_cliente": m.get("codigo_cliente", ""),
                "coincidencia": m.get("coincidencia", ""),
                "pppoe_status": m.get("pppoe_status", ""),
            }
            # estado_onu DEBE escribirse como entero: la serie existente en
            # InfluxDB es `integer`; escribirlo como string provoca
            # "field type conflict" (partial write) y descarta TODO el batch
            # (hoy: "Enriquecidos: 0/3966 puntos escritos").
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