# src/wisp_client.py
"""
WispClient: acceso a la base de datos de clientes WISP (Fuente 3).

Se conecta a la API HTTP de czwisp para obtener el catálogo de clientes
(listado masivo paginado, o lookup por documento) y normaliza los datos a
un esquema común. Si la API no está disponible o el ISP no expone listado,
se puede sembrar con un CSV local (WISP_DOCUMENTOS_CSV).

Esquema normalizado emitido por `normalize_client`:
    documento, serial_onu, usuario_pppoe, nombre, direccion, telefono,
    plan, estado, ip_servicio, nap_nombre, puerto, zona, mac, raw
"""
import csv
import json
import logging
import os
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_norm(v) for v in value if _norm(v))
    return str(value).strip()


def _pick(raw: dict, *names) -> str:
    for n in names:
        v = raw.get(n)
        if v not in (None, ""):
            return _norm(v)
    return ""


def _num(raw: dict, *names, default: float = 0.0) -> str:
    """Extrae un numérico de la fila (o sub-dict) como string limpio."""
    for n in names:
        v = raw.get(n)
        if v in (None, ""):
            continue
        try:
            f = float(v)
            if f.is_integer():
                return str(int(f))
            return f"{f:.2f}"
        except (ValueError, TypeError):
            return _pick(raw, n)
    return str(default)


_SERIAL_KEYS = ("serial_onu", "serial", "serial_ont", "onu_serial",
                "Serial Onu", "serial_onu ")
_PPPOE_KEYS = ("usuario_pppoe", "usuario", "username", "login", "cuenta",
               "pppoe_user", "Usuario PPPoE")
_NAME_KEYS = ("nombre_cliente", "name", "nombre", "Nombre Cliente")
_ADDR_KEYS = ("direccion", "address", "direccion_servicio",
              "Direccion Servicio")
_PHONE_KEYS = ("telefono", "phone", "celular", "contacto", "telefonos")
_EMAIL_KEYS = ("email", "correo", "mail", "emails")
_PLAN_KEYS = ("plan", "producto", "velocidad", "service", "plan_nombre",
              "servicio_nombre")
_TECH_KEYS = ("tecnologia", "technology", "tecnología")
_STATE_KEYS = ("estado", "estado_cliente", "status", "state", "Estado",
               "estado_servicio")
_IP_KEYS = ("ip_servicio", "ip", "Ip Servicio")
_NAP_KEYS = ("nap", "nap_nombre", "caja", "nodo_nap", "nombre_nap")
_PORT_KEYS = ("puerto", "puerto_servicio", "puerto_nap", "caja_puerto")
_PPORT_KEYS = ("puerto_pon", "pon_port", "puerto_puerto")
_ZONE_KEYS = ("zona", "nodo_nombre", "router", "nodo", "sector", "Router",
              "comuna", "sucursal_nombre")
_MAC_KEYS = ("mac", "mac_address", "mac_onu", "onu_mac", "mac_router")
_COORD_KEYS = ("coordenadas", "lat_lng", "latitud", "location", "geolocation")


def normalize_client(raw: dict) -> dict:
    """Normaliza un dict de cliente (respuesta HTTP o fila CSV) a esquema común."""
    raw = dict(raw or {})
    doc = _pick(raw, "documento", "Documento", "documento_identidad",
                "cedula", "nit", "doc")
    serial = _pick(raw, *_SERIAL_KEYS)
    usuario = _pick(raw, *_PPPOE_KEYS)
    mac = _pick(raw, *_MAC_KEYS)
    return {
        "documento": doc,
        "serial_onu": serial.upper(),
        "usuario_pppoe": usuario.lower(),
        "mac": mac.lower(),
        "nombre": _pick(raw, *_NAME_KEYS),
        "direccion": _pick(raw, *_ADDR_KEYS),
        "telefono": _pick(raw, *_PHONE_KEYS),
        "email": _pick(raw, *_EMAIL_KEYS),
        "plan": _pick(raw, *_PLAN_KEYS),
        "tecnologia": _pick(raw, *_TECH_KEYS),
        "velocidad_down_mbps": _num(raw, "velocidad_down_mbps",
                                    "vel_down_mbps", "velocidad_d"),
        "velocidad_up_mbps": _num(raw, "velocidad_up_mbps",
                                  "vel_up_mbps", "velocidad_u"),
        "precio_usd": _num(raw, "precio_usd", "precio", "price_usd"),
        "estado": _pick(raw, *_STATE_KEYS).lower(),
        "ip_servicio": _pick(raw, *_IP_KEYS),
        "nap_nombre": _pick(raw, *_NAP_KEYS),
        "puerto": _pick(raw, *_PORT_KEYS),
        "puerto_pon": _pick(raw, *_PPORT_KEYS),
        "zona": _pick(raw, *_ZONE_KEYS),
        "coordenadas": _pick(raw, *_COORD_KEYS),
        "tipo_documento": _pick(raw, "tipo_documento", "doc_type"),
        "codigo_cliente": _pick(raw, "codigo_cliente", "client_code"),
        "raw": raw,
    }


def _flatten_czwisp_service(entry: dict) -> dict:
    """Aplana una entrada de `servicios[]` de czwisp a claves planas.

    czwisp devuelve por servicio: {cliente:{...}, servicio:{...},
    plan:{...}, red:{...}, facturacion:{...}}. Se fusionan en un solo
    dict plano para que `normalize_client` los lea.
    """
    cliente = entry.get("cliente") or {}
    servicio = entry.get("servicio") or {}
    plan = entry.get("plan") or {}
    red = entry.get("red") or {}
    fact = entry.get("facturacion") or {}
    if cliente or servicio or plan or red or fact:
        # Forma anidada: {cliente:{...}, servicio:{...}, plan:{...}, ...}
        flat = {}
        for chunk in (cliente, servicio, red, fact):
            for k, v in chunk.items():
                if v in (None, ""):
                    continue
                if isinstance(v, dict):
                    # Sub-objetos tipo {nombre, activo} (ej. red.router): usar .nombre.
                    flat.setdefault(k, _pick(v, "nombre", "name"))
                else:
                    flat.setdefault(k, v)
        # `plan.nombre` colisionaría con `cliente.nombre_cliente`: se mapea a plan_nombre.
        for k, v in plan.items():
            if v in (None, ""):
                continue
            key = "plan_nombre" if k == "nombre" else k
            flat.setdefault(key, v)
        return flat
    # Forma plana: {id, estado, plan_nombre, usuario_pppoe, serial_onu, ...}
    return {k: v for k, v in entry.items() if v not in (None, "")}


def extract_czwisp_servicios(payload) -> List[dict]:
    """Extrae la lista de servicios de una respuesta czwisp.

    Formato: {encontrado, total, servicios:[{cliente, servicio, plan, red,
    facturacion}]}. Devuelve dicts planos ya fusionados. Si `encontrado` es
    false o no hay servicios, devuelve [].
    """
    if not isinstance(payload, dict):
        return []
    if payload.get("encontrado") is False:
        return []
    raw_list = payload.get("servicios")
    if not isinstance(raw_list, list):
        return []
    return [_flatten_czwisp_service(e) for e in raw_list if isinstance(e, dict)]


def _extract_rows(payload) -> List[dict]:
    """Extrae la lista de clientes de una respuesta HTTP (JSON variado)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "clientes", "results", "items", "clients"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _response_rows(resp: requests.Response) -> (List[dict], dict):
    """Retorna (rows, meta) desde una respuesta JSON. meta incluye
    page/total/next para controlar la paginación."""
    payload = resp.json()
    if isinstance(payload, dict):
        rows = _extract_rows(payload)
        return rows, {
            "page": payload.get("page", payload.get("pagina", 1)),
            "total": payload.get("total", payload.get("total_pages", 0)),
            "next": payload.get("next"),
        }
    return _extract_rows(payload), {}


def load_from_csv(path: str) -> List[dict]:
    """Lee la semilla local de clientes WISP (CSV). Devuelve dicts normalizados."""
    if not path or not os.path.exists(path):
        logger.warning(f"CSV WISP no encontrado: {path}")
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                if not raw:
                    continue
                client = normalize_client({str(k).strip(): v for k, v in raw.items()})
                if client["documento"] or client["serial_onu"] or client["usuario_pppoe"]:
                    rows.append(client)
    except Exception as e:
        logger.error(f"Fallo leyendo CSV WISP {path}: {e}")
        return []
    logger.info(f"CSV WISP: cargados {len(rows)} clientes desde {path}")
    return rows


class WispClient:
    """Cliente HTTP contra la API de czwisp con fallback a CSV local."""

    def __init__(self, config, api_key: str = "",
                 session: Optional[requests.Session] = None,
                 cache_path: str = "data/wisp_clients_cache.json"):
        self.config = config
        self.api_key = api_key or os.environ.get(config.api_key_env, "")
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "x-api-key": self.api_key,
        })
        self.cache_path = cache_path
        self._last_doc_lookup: Dict[str, float] = {}
        self._last_request_ts: float = 0.0
        self._serial_cache: Dict[str, tuple] = {}  # serial -> (ts, cliente|None)

    # --- helpers de URL / headers ---
    def _base_headers(self) -> dict:
        return {"x-api-key": self.api_key}

    def _resolve(self, endpoint: str) -> str:
        # Reemplazo puntual de {slug} (evita .format() @@ con {documento} en el path)
        return endpoint.replace("{slug}", self.config.isp_slug)

    def _pace(self) -> None:
        """Espacia las consultas HTTP según min_interval_ms (rate limit)."""
        interval = getattr(self.config, "min_interval_ms", 250) / 1000.0
        elapsed = time.time() - self._last_request_ts
        if self._last_request_ts and elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_ts = time.time()

    def _get(self, url: str) -> Optional[requests.Response]:
        try:
            self._pace()
            resp = self.session.get(url, timeout=self.config.timeout_seconds)
            resp.raise_for_status()
            return resp
        except Exception as e:
            logger.warning(f"Error HTTP czwisp ({url}): {e}")
            return None

    def _get_ex(self, url: str) -> tuple:
        """GET que distingue fallo transitorio de respuesta definitiva.

        Returns (resp, ok):
          - (resp, True)  → respuesta definitiva (incluye 404: recurso no existe)
          - (None, False) → error de red / timeout / 5xx (reintentar pronto)
        """
        try:
            self._pace()
            resp = self.session.get(url, timeout=self.config.timeout_seconds)
        except Exception as e:
            logger.warning(f"Error HTTP czwisp ({url}): {e}")
            return None, False
        if getattr(resp, "status_code", 200) == 404:
            return resp, True
        try:
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Error HTTP czwisp ({url}): {e}")
            return None, False
        return resp, True

    # --- listado masivo (paginado) ---
    def fetch_list(self, force: bool = False) -> List[dict]:
        """Listado paginado de clientes. Sin endpoint configurado retorna []."""
        endpoint = self.config.list_endpoint.strip()
        if not endpoint:
            logger.info("WISP list_endpoint no configurado; se omite listado masivo")
            return []
        if not self.config.isp_slug:
            logger.warning("WISP isp_slug vacío; no se puede consultar el listado")
            return []

        url = self._get_url(endpoint)
        rows: List[dict] = []
        page = 1
        while True:
            resp = self._get(self._paginate(url, page))
            if resp is None:
                break
            chunk, meta = _response_rows(resp)
            rows.extend(chunk)
            if not chunk:
                break
            total = meta.get("total") or 0
            next_url = meta.get("next")
            if next_url:
                url = next_url
                page = 1
                continue
            if total and page >= int(total):
                break
            if meta.get("next") is None and int(meta.get("page", page)) >= page \
                    and len(chunk) < self.config.page_size:
                break
            if len(chunk) < self.config.page_size:
                break
            page += 1

        clients = [normalize_client(c) for c in rows]
        logger.info(f"WISP listado: {len(clients)} clientes")
        return clients

    def _get_url(self, endpoint: str) -> str:
        base = self.config.base_url.rstrip("/")
        ep = self._resolve(endpoint)
        if ep.startswith("http"):
            return ep
        return f"{base}{ep}"

    def _paginate(self, url: str, page: int) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}&limit={self.config.page_size}"

    # --- lookup por documento ---
    def fetch_by_document_ex(self, documento: str) -> tuple:
        """Lookup por documento → (cliente|None, ok).

        ok=False: fallo transitorio (red/5xx/cooldown) → el caller debe
        reintentar pronto y NO registrar "no encontrado" en su estado.
        ok=True:  respuesta definitiva (200 con o sin cliente, o 404).
        """
        if not documento:
            return None, True
        endpoint = self.config.document_endpoint.strip()
        if not endpoint:
            return None, True
        url = self._get_url(endpoint)
        documento = _norm(documento)
        url = url.replace("{documento}", documento)

        now = time.time()
        last = self._last_doc_lookup.get(documento, 0)
        cooldown = 1.0
        if now - last < cooldown:
            return None, False
        self._last_doc_lookup[documento] = now

        resp, ok = self._get_ex(url)
        if resp is None:
            return None, False
        if getattr(resp, "status_code", 200) == 404:
            return None, True
        try:
            payload = resp.json()
        except Exception:
            return None, False
        # Respuesta czwisp anidada: {encontrado, total, servicios:[...]}
        services = extract_czwisp_servicios(payload)
        if services:
            first = dict(services[0])
            # Los datos personales (nombre, documento, teléfonos) viven en
            # el bloque `cliente` del payload, no en cada servicio plano.
            cliente = payload.get("cliente") or {}
            for k, v in cliente.items():
                if v not in (None, "") and not first.get(k):
                    first[k] = v
            return normalize_client(first), True
        # Devolver embedido o el objeto mismo
        inner = payload
        if isinstance(inner, dict):
            for key in ("cliente", "data", "client"):
                if isinstance(inner.get(key), dict):
                    inner = inner[key]
                    break
        if isinstance(inner, dict):
            known = set(["documento", "serial_onu", "usuario_pppoe", "nombre",
                         "nombre_cliente", "usuario", "serial", "documento_identidad"])
            if known.intersection(inner.keys()):
                return normalize_client(inner), True
        return None, True

    def fetch_by_document(self, documento: str) -> Optional[dict]:
        """Lookup individual por documento. Respetando cooldown entre consultas."""
        return self.fetch_by_document_ex(documento)[0]

    # --- lookup por serial ONU (servicio) ---
    def fetch_by_serial_ex(self, serial: str, force: bool = False) -> tuple:
        """Lookup por serial → (cliente|None, ok). Ver `fetch_by_document_ex`."""
        if not serial:
            return None, True
        serial = _norm(serial).upper()
        endpoint = self.config.serial_endpoint.strip()
        if not endpoint:
            logger.info("WISP serial_endpoint no configurado; se omite lookup por serial")
            return None, True

        ttl = self.config.cache_hours * 3600
        cached = self._serial_cache.get(serial)
        if not force and cached and time.time() - cached[0] < ttl:
            return cached[1], True

        url = self._get_url(endpoint).replace("{serial}", serial)
        resp, ok = self._get_ex(url)
        if resp is None:
            return None, False
        if getattr(resp, "status_code", 200) == 404:
            self._serial_cache[serial] = (time.time(), None)
            return None, True
        try:
            payload = resp.json()
        except Exception:
            return None, False

        services = extract_czwisp_servicios(payload)
        result: Optional[dict] = None
        if services:
            matched = next((s for s in services
                            if str(s.get("serial_onu", "")).upper() == serial), None)
            result = normalize_client(matched or services[0])
            if result.get("serial_onu") != serial:
                result["serial_onu"] = serial
        self._serial_cache[serial] = (time.time(), result)
        return result, True

    def fetch_by_serial(self, serial: str, force: bool = False) -> Optional[dict]:
        """Lookup individual por serial ONU vía `serial_endpoint`.

        Usa caché en memoria por serial con TTL = cache_hours; un serial que
        WISP no conoce también se guarda (None) para no re-consultarlo en el
        mismo ciclo. El pacing (`min_interval_ms`) se aplica en `_get`.
        """
        return self.fetch_by_serial_ex(serial, force=force)[0]

    def clear_serial_cache(self) -> None:
        self._serial_cache.clear()

    # --- caché local (listado) ---
    def list_clients(self, force: bool = False) -> List[dict]:
        """Listado con caché en disco + semilla CSV si la API no responde."""
        if not force and self.config.cache_hours > 0 and os.path.exists(self.cache_path):
            age = time.time() - os.path.getmtime(self.cache_path)
            if age < self.config.cache_hours * 3600:
                try:
                    with open(self.cache_path, "r", encoding="utf-8") as fh:
                        cached = json.load(fh)
                    if isinstance(cached, list):
                        return cached
                except Exception:
                    pass

        clients = self.fetch_list()
        if clients:
            self._save_cache(clients)
            return clients

        # Fallback: caché vieja o CSV semilla
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if isinstance(cached, list) and cached:
                    logger.warning("WISP: usando caché local (API sin datos)")
                    return cached
            except Exception:
                pass
        return load_from_csv(self.config.documents_csv)

    def _save_cache(self, clients: List[dict]) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump([{k: v for k, v in c.items() if k != "raw"}
                           for c in clients], fh, ensure_ascii=False)
            logger.info(f"WISP caché guardada: {self.cache_path}")
        except Exception as e:
            logger.warning(f"No se pudo guardar caché WISP: {e}")