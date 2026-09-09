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
    return str(value).strip()


def _pick(raw: dict, *names) -> str:
    for n in names:
        v = raw.get(n)
        if v not in (None, ""):
            return _norm(v)
    return ""


_SERIAL_KEYS = ("serial_onu", "serial", "serial_ont", "onu_serial",
                "Serial Onu", "serial_onu ")
_PPPOE_KEYS = ("usuario_pppoe", "usuario", "username", "login", "cuenta",
               "pppoe_user", "Usuario PPPoE")
_NAME_KEYS = ("nombre", "nombre_cliente", "name", "Nombre Cliente")
_ADDR_KEYS = ("direccion", "address", "direccion_servicio",
              "Direccion Servicio")
_PHONE_KEYS = ("telefono", "phone", "celular", "contacto")
_PLAN_KEYS = ("plan", "producto", "velocidad", "service")
_STATE_KEYS = ("estado", "estado_cliente", "status", "state", "Estado")
_IP_KEYS = ("ip_servicio", "ip", "Ip Servicio")
_NAP_KEYS = ("nap", "nap_nombre", "caja", "nodo_nap", "nombre_nap")
_PORT_KEYS = ("puerto", "puerto_servicio", "puerto_nap", "caja_puerto")
_ZONE_KEYS = ("zona", "nodo", "router", "sector", "Router", "comuna")
_MAC_KEYS = ("mac", "mac_address", "mac_onu", "onu_mac")


def normalize_client(raw: dict) -> dict:
    """Normaliza un dict de cliente (respuesta HTTP o fila CSV) a esquema común."""
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
        "plan": _pick(raw, *_PLAN_KEYS),
        "estado": _pick(raw, *_STATE_KEYS).lower(),
        "ip_servicio": _pick(raw, *_IP_KEYS),
        "nap_nombre": _pick(raw, *_NAP_KEYS),
        "puerto": _pick(raw, *_PORT_KEYS),
        "zona": _pick(raw, *_ZONE_KEYS),
        "raw": raw if isinstance(raw, dict) else None,
    }


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

    # --- helpers de URL / headers ---
    def _base_headers(self) -> dict:
        return {"x-api-key": self.api_key}

    def _resolve(self, endpoint: str) -> str:
        return endpoint.format(slug=self.config.isp_slug)

    def _get(self, url: str) -> Optional[requests.Response]:
        try:
            resp = self.session.get(url, timeout=self.config.timeout_seconds)
            resp.raise_for_status()
            return resp
        except Exception as e:
            logger.warning(f"Error HTTP czwisp ({url}): {e}")
            return None

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
    def fetch_by_document(self, documento: str) -> Optional[dict]:
        """Lookup individual por documento. Respetando cooldown entre consultas."""
        if not documento:
            return None
        endpoint = self.config.document_endpoint.strip()
        url = self._get_url(endpoint)
        documento = _norm(documento)
        url = url.replace("{documento}", documento)

        now = time.time()
        last = self._last_doc_lookup.get(documento, 0)
        cooldown = 1.0
        if now - last < cooldown:
            return None
        self._last_doc_lookup[documento] = now

        resp = self._get(url)
        if resp is None:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
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
                return normalize_client(inner)
        return None

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