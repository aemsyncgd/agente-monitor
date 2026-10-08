# src/redis_store.py
"""
ClientStore: cache en Redis de las fichas de clientes enriquecidos.

Provee consultas instantáneas (ficha por documento/serial/mac/usuario,
búsqueda por texto y agrupación por zona/NAP) sin golpear InfluxDB por
request. Es un snapshot que se reconstruye periódicamente (scheduler) y
cae en rama N/A si Redis no está disponible.

Claves:
  cli:{doc}            -> JSON con todos los campos de la ficha
  idx:serial:{serial}  -> SET de documentos
  idx:mac:{mac}        -> SET de documentos
  idx:user:{usuario}   -> SET de documentos
  grp:zona:{zona}      -> SET de documentos
  grp:nap:{nap}        -> SET de documentos
  cli:meta             -> hash {count, built_at}
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATES_DOWN = [1, 4, 6]

_SEARCH_FIELDS = (
    "documento", "nombre", "direccion", "serial_onu", "usuario_pppoe",
    "mac", "olt", "nap", "zona", "telefono", "pon_port",
)


def _collapse_variants(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Colapsa variantes de tagset del mismo cliente en una sola ficha.

    `clientes_enriquecidos` puede tener conviviendo una fila 'solo OLT' (sin
    documento -> clave = serial) y una fila con PII para el mismo serial.
    Sin colapso ambas se escribian y la busqueda exacta por serial devolvia
    la vacia. Gana la mas rica (nombre > documento); las demas solo
    rellenan campos que la ganadora trae vacios.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    sin_ident: List[Dict[str, Any]] = []
    for row in rows:
        ident = _norm(row.get("serial_onu")) or _norm(row.get("documento"))
        if not ident:
            sin_ident.append(row)
            continue
        groups.setdefault(ident, []).append(row)
    out: List[Dict[str, Any]] = list(sin_ident)
    for variants in groups.values():
        if len(variants) == 1:
            out.append(variants[0])
            continue
        variants.sort(key=lambda r: (
            bool(str(r.get("nombre") or "").strip()),
            bool(str(r.get("documento") or "").strip()),
        ), reverse=True)
        base = dict(variants[0])
        for extra in variants[1:]:
            for k, v in extra.items():
                if base.get(k) in (None, "") and v not in (None, ""):
                    base[k] = v
        out.append(base)
    return out


class ClientStore:
    """Snapshot de fichas en Redis. No debe usarse concurrentemente."""
    _PREFIX_CLI = "cli:"
    _PREFIX_IDX = "idx:"
    _PREFIX_GRP = "grp:"
    _META = "cli:meta"

    def __init__(self, redis=None):
        self._r = redis

    @property
    def _redis(self):
        return self._r

    # --- construcción ---
    def rebuild(self, rows: List[Dict[str, Any]]) -> int:
        if self._redis is None:
            logger.warning("ClientStore sin redis; rebuild omitido")
            return 0

        rows = _collapse_variants(rows)
        pipe = self._redis.pipeline(transaction=False)
        # limpiar snapshot anterior
        for key in self._scan_keys(self._PREFIX_CLI + "*"):
            pipe.delete(key)
        for key in self._scan_keys(self._PREFIX_IDX + "*"):
            pipe.delete(key)
        for key in self._scan_keys(self._PREFIX_GRP + "*"):
            pipe.delete(key)
        pipe.delete(self._META)
        pipe.execute()

        pipe = self._redis.pipeline(transaction=False)
        count = 0
        for row in rows:
            doc = str(row.get("documento") or "").strip()
            if not doc:
                # Fichas sin documento/contrato (CPE no asociado): indexar
                # por serial para no perderlas en alertas LOS/agrupación.
                doc = str(row.get("serial_onu") or "").strip()
            if not doc:
                continue
            ficha = dict(row)
            ficha["documento"] = doc
            ficha["estado_onu"] = _as_int(ficha.get("estado_onu"))
            pipe.set(self._PREFIX_CLI + doc, json.dumps(ficha, ensure_ascii=False, default=str))
            serial = _norm(row.get("serial_onu"))
            if serial:
                pipe.sadd(self._PREFIX_IDX + "serial:" + serial, doc)
            mac = _norm(row.get("mac"))
            if mac:
                pipe.sadd(self._PREFIX_IDX + "mac:" + mac, doc)
            user = _norm(row.get("usuario_pppoe"))
            if user:
                pipe.sadd(self._PREFIX_IDX + "user:" + user, doc)
            zona = _norm(row.get("zona"))
            if zona:
                pipe.sadd(self._PREFIX_GRP + "zona:" + zona, doc)
            nap = _norm(row.get("nap"))
            if nap:
                pipe.sadd(self._PREFIX_GRP + "nap:" + nap, doc)
            count += 1

        pipe.hset(self._META, mapping={
            "count": count,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        pipe.execute()
        logger.info(f"ClientStore: {count} fichas en Redis")
        return count

    def rebuild_from_influx(self, influx, hours: int = 168) -> int:
        """Reconstruye el snapshot desde InfluxDB (clientes_enriquecidos).

        Evita pivot() en Flux (colisiona tipos string/int); cada campo llega
        como registro separado y se unen en Python por las columnas de tag
        reales (excluyendo columnas de cómputo de la consulta).
        """
        flux = (
            f'from(bucket: "{influx.bucket}")\n'
            f'  |> range(start: -{hours}h)\n'
            f'  |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")\n'
            f'  |> last()\n'
        )
        rows = _merge_snapshot_rows(influx.query(flux))
        return self.rebuild(rows)

    # --- lectura ---
    def get_ficha(self, doc: str) -> Optional[Dict[str, Any]]:
        if self._redis is None:
            return None
        raw = self._redis.get(self._PREFIX_CLI + str(doc))
        if not raw:
            return None
        return _decode(raw)

    def get_fichas(self, docs: List[str]) -> List[Dict[str, Any]]:
        if self._redis is None or not docs:
            return []
        raws = self._redis.mget([self._PREFIX_CLI + str(d) for d in docs])
        out = []
        for d, raw in zip(docs, raws):
            if not raw:
                continue
            ficha = _decode(raw)
            if ficha:
                out.append(ficha)
        return out

    def by_serial(self, serial: str) -> List[str]:
        return self._docs_by(self._PREFIX_IDX + "serial:" + _norm(serial))

    def by_mac(self, mac: str) -> List[str]:
        return self._docs_by(self._PREFIX_IDX + "mac:" + _norm(mac))

    def by_user(self, usuario: str) -> List[str]:
        return self._docs_by(self._PREFIX_IDX + "user:" + _norm(usuario))

    def group_members(self, by: str, clave: str) -> List[str]:
        return self._docs_by(self._PREFIX_GRP + by + ":" + _norm(clave))

    def _docs_by(self, key: str) -> List[str]:
        if self._redis is None:
            return []
        return sorted(self._redis.smembers(key) or ())

    def search(self, q: str, limit: int = 25, nap: Optional[str] = None,
               zona: Optional[str] = None, olt: Optional[str] = None) -> List[str]:
        if self._redis is None:
            return []

        doc_exacto = str(q).strip()
        if doc_exacto and self._redis.exists(self._PREFIX_CLI + doc_exacto):
            if _match_filters(self.get_ficha(doc_exacto), nap=nap, zona=zona, olt=olt):
                return [doc_exacto]

        needle = _norm(q)
        nap_f, zona_f, olt_f = _norm(nap), _norm(zona), _norm(olt)
        out: List[str] = []
        for ficha in self._iter_fichas():
            if nap_f and nap_f != _norm(ficha.get("nap")):
                continue
            if zona_f and zona_f != _norm(ficha.get("zona")):
                continue
            if olt_f and olt_f != _norm(ficha.get("olt")):
                continue
            if not needle:
                out.append(str(ficha.get("documento") or ""))
            elif _match_kw(ficha, needle):
                out.append(str(ficha.get("documento") or ""))
            if len(out) >= limit:
                break
        return out

    def los(self, estados: Optional[List[int]] = None) -> List[str]:
        down = set(estados or _STATES_DOWN)
        if self._redis is None:
            return []
        return [str(f.get("documento") or "") for f in self._iter_fichas()
                if _as_int(f.get("estado_onu")) in down]

    def group_los(self, estados: Optional[List[int]] = None, by: str = "zona") -> List[Dict[str, Any]]:
        down = set(estados or _STATES_DOWN)
        acc: Dict[str, Dict[str, Any]] = {}
        if self._redis is not None:
            for ficha in self._iter_fichas():
                clave = _grupo(ficha.get(by))
                grp = acc.setdefault(clave, {"clave": clave, "total": 0, "los": 0})
                grp["total"] += 1
                if _as_int(ficha.get("estado_onu")) in down:
                    grp["los"] += 1
        return sorted(acc.values(), key=lambda g: -g["los"])

    def group_counts(self, by: str = "zona") -> List[Dict[str, Any]]:
        acc: Dict[str, Dict[str, Any]] = {}
        if self._redis is not None:
            for ficha in self._iter_fichas():
                clave = _grupo(ficha.get(by))
                grp = acc.setdefault(clave, {"clave": clave, "total": 0})
                grp["total"] += 1
        return sorted(acc.values(), key=lambda g: -g["total"])

    # --- util ---
    def count(self) -> int:
        if self._redis is None:
            return 0
        return int(self.meta().get("count", 0))

    def meta(self) -> Dict[str, Any]:
        if self._redis is None:
            return {}
        meta = self._redis.hgetall(self._META) or {}
        try:
            meta["count"] = int(meta.get("count", 0))
        except (TypeError, ValueError):
            meta["count"] = 0
        return meta

    def _iter_fichas(self):
        for key in self._scan_keys(self._PREFIX_CLI + "*"):
            if key == self._META:
                continue
            raw = self._redis.get(key)
            if raw:
                ficha = _decode(raw)
                if ficha:
                    yield ficha

    def _scan_keys(self, pattern: str) -> List[str]:
        return list(self._redis.scan_iter(match=pattern, count=1000))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _grupo(value: Any) -> str:
    return str(value or "").strip() or "(sin asignar)"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _decode(raw: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ClientStore: ficha JSON invalida")
        return None


def _match_filters(ficha, nap=None, zona=None, olt=None) -> bool:
    if not ficha:
        return False
    if nap and _norm(nap) != _norm(ficha.get("nap")):
        return False
    if zona and _norm(zona) != _norm(ficha.get("zona")):
        return False
    if olt and _norm(olt) != _norm(ficha.get("olt")):
        return False
    return True


_NON_TAG_COLS = {
    "measurement", "field", "value", "time", "result", "table",
    "_result", "_start", "_stop", "_measurement", "_field", "_value", "_time",
}


def _merge_snapshot_rows(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Une registros campo-a-campo (ultima fila por cliente) en fichas.

    `InfluxClient.query()` devuelve un registro por campo con tags repetidos;
    aqui se agrupan por el conjunto de columnas de tag reales (las que no son
    columnas de cómputo de la consulta) y se conserva el ultimo timestamp.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    order: Dict[str, float] = {}
    for r in results:
        tags = {k: v for k, v in r.items() if k not in _NON_TAG_COLS}
        if not tags:
            continue
        key = "|".join(f"{k}={v}" for k, v in sorted(tags.items()))
        ts = r.get("time")
        try:
            ts_key = ts.timestamp() if hasattr(ts, "timestamp") else float(ts or 0)
        except (TypeError, ValueError):
            ts_key = 0.0
        if order.get(key, float("-inf")) > ts_key:
            continue
        row = rows.setdefault(key, dict(tags))
        row[r.get("field", "")] = r.get("value")
        order[key] = ts_key
    return list(rows.values())


def _match_kw(ficha: Dict[str, Any], needle: str) -> bool:
    if not needle:
        return True
    # AND por tokens: "PEREZ JUAN" encuentra "Juan Perez Diaz" aunque no
    # sean contiguos (el substring simple nunca matcheaba eso).
    tokens = _norm(needle).split()
    haystack = " ".join(_norm(ficha.get(f)) for f in _SEARCH_FIELDS)
    return all(t in haystack for t in tokens)


_store_lock = threading.Lock()
_client_store: Optional[ClientStore] = None


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if url:
        return url
    try:
        from .config_ia import load_config_ia
        return load_config_ia(
            os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
        ).redis.url
    except Exception:
        return "redis://localhost:6379/0"


def get_client_store() -> ClientStore:
    """Singleton del ClientStore (conexion lazy a Redis)."""
    global _client_store
    with _store_lock:
        if _client_store is None:
            import redis as _redis
            _client_store = ClientStore(
                _redis.Redis.from_url(_redis_url(), socket_timeout=3,
                                      decode_responses=True)
            )
    return _client_store