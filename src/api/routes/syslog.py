"""
Rutas para consultar logs de syslog (MikroTik/OLTs) enriquecidos por log_monitor.
Lee desde redis-syslog (127.0.0.1:6380) donde se almacenan en ZSET syslog:recent
y listas por dispositivo.
"""

import json
import re
import time
from typing import Optional

import redis
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_role, Role
from ...syslog_severity import effective_severity, is_important

router = APIRouter(dependencies=[Depends(require_role(Role.VIEWER))])

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6380
REDIS_DB = 0


def _with_effective_severity(event: dict) -> dict:
    """RouterOS envía severity=notice en casi todo: el nivel real está en el tag.

    Se calcula aquí y no solo al ingerir para que los eventos antiguos
    (sin ``severity_effective``) también se clasifiquen correctamente.
    """
    event["severity_effective"] = effective_severity(
        event.get("tag"), event.get("severity")
    )
    return event


def _severity_bucket(event: dict) -> str:
    """Columna del panel: noticias, warnings o errores."""
    sev = str(event.get("severity_effective") or event.get("severity") or "").lower()
    if sev in ("critical", "crit", "error", "err", "emerg", "alert"):
        return "errors"
    if sev == "warning" or "warn" in sev:
        return "warnings"
    return "news"


# Prefijo de sesión PPPoE: "<4558>: user X authentication failed" cambia en cada
# reintento y hacía que la misma alerta se repitiera cientos de veces.
_SESSION_PREFIX_RE = re.compile(r"^\s*<[^>]{1,40}>:\s*")
_WHITESPACE_RE = re.compile(r"\s+")


def _dedupe_key(event: dict) -> tuple:
    """Agrupa repeticiones del mismo evento (mismo equipo, tag y mensaje)."""
    message = _SESSION_PREFIX_RE.sub("", str(event.get("message") or "")).strip()
    message = _WHITESPACE_RE.sub(" ", message).lower()
    return (event.get("device_ip"), event.get("tag"), message)


def _get_redis():
    try:
        return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                          decode_responses=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"redis-syslog no disponible: {e}")


@router.get("/recent")
def get_syslog_recent(
    minutes: int = Query(60, ge=1, le=10080),
    limit: int = Query(200, ge=1, le=1000),
    device_ip: Optional[str] = None,
    device_name: Optional[str] = None,
    device_type: Optional[str] = None,
    site: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
):
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrangebyscore("syslog:recent", cutoff, now)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    out = []
    for s in items:
        try:
            d = json.loads(s)
        except Exception:
            continue
        _with_effective_severity(d)
        if device_ip and str(d.get("device_ip")) != str(device_ip):
            continue
        if device_name and str(device_name).lower() not in str(d.get("device_name", "")).lower():
            continue
        if device_type and str(d.get("device_type")) != str(device_type):
            continue
        if site and str(d.get("site", "")).lower() != str(site).lower():
            continue
        if severity and str(d.get("severity_effective", "")).lower() != str(severity).lower():
            continue
        if search:
            st = search.lower()
            blob = " ".join([str(d.get("message", "")), str(d.get("tag", "")), str(d.get("program", ""))]).lower()
            if st not in blob:
                continue
        out.append(d)
    out = sorted(out, key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"ok": True, "count": len(out[:limit]), "events": out[:limit], "window_minutes": minutes}


@router.get("/by-device/{ip}")
def get_syslog_by_device(
    ip: str,
    minutes: int = Query(120, ge=1, le=10080),
    limit: int = Query(200, ge=1, le=1000),
):
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrangebyscore("syslog:recent", cutoff, now)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    out = []
    for s in items:
        try:
            d = json.loads(s)
        except Exception:
            continue
        if str(d.get("device_ip")) != str(ip):
            continue
        out.append(_with_effective_severity(d))
    out = sorted(out, key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"ok": True, "device_ip": ip, "count": len(out[:limit]), "events": out[:limit]}


@router.get("/summary")
def get_syslog_summary(minutes: int = Query(60, ge=1, le=10080), limit: int = Query(30, ge=1, le=200)):
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrangebyscore("syslog:recent", cutoff, now)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    events = []
    for s in items:
        try:
            events.append(_with_effective_severity(json.loads(s)))
        except Exception:
            continue
    by_sev = {}
    by_dev = {}
    by_type = {}
    for e in events:
        sev = e.get("severity_effective") or e.get("severity") or "unknown"
        by_sev[sev] = by_sev.get(sev, 0) + 1
        dn = e.get("device_name") or e.get("device_ip") or "unknown"
        by_dev[dn] = by_dev.get(dn, 0) + 1
        dt = e.get("device_type") or "unknown"
        by_type[dt] = by_type.get(dt, 0) + 1
    recent = sorted(events, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
    return {"ok": True, "window_minutes": minutes, "total": len(events), "by_severity": by_sev, "by_device": by_dev, "by_type": by_type, "recent": recent}


@router.get("/queue/len")
def get_syslog_queue_len():
    r = _get_redis()
    try:
        qlen = r.llen("cola_logs_para_llm")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "queue": "cola_logs_para_llm", "length": qlen}


# ==================== PANELES DEL DASHBOARD ====================

# Tope de eventos leídos del ZSET por request: con 24 h de historial la lista
# puede ser enorme y el panel solo muestra lo más reciente.
_SCAN_LIMIT = 20000

_GROUP_LABELS = {"mikrotik": "MikroTik · RouterOS", "olt": "OLTs"}


def _event_group(event: dict) -> str:
    """Grupo del panel: los OLT se identifican por ``device_type``; el resto
    (RouterOS y equipos que aún no reportan tipo) va a MikroTik."""
    return "olt" if str(event.get("device_type") or "").strip().lower() == "olt" else "mikrotik"


def _panel_severity(event: dict) -> str:
    """Gravedad para las tarjetas y para el color de cada fila.

    Las OLT reportan ``severity=err`` para *todo* (incluso "ONU Online"), así
    que cuando el mensaje es reconocible manda el mensaje. En RouterOS se
    distingue el netwatch: "event down" es advertencia, "event up" es noticia.
    """
    msg = str(event.get("message") or "").lower()
    tag = str(event.get("tag") or event.get("program") or "").lower()
    if _event_group(event) == "olt":
        if "bip8" in msg:
            return "high"
        if "dying gasp" in msg or "offline" in msg:
            return "warning"
        if "port los" in msg or "link down" in msg:
            return "warning"
        if "online" in msg:
            return "news"
        if "logged in" in msg or "logged out" in msg or "logouted" in msg:
            return "news"
    elif tag.startswith("netwatch"):
        if "event down" in msg:
            return "warning"
        if "event up" in msg:
            return "news"
    sev = str(event.get("severity_effective") or event.get("severity") or "").lower()
    if sev in ("critical", "crit", "emerg", "emergency", "alert"):
        return "danger"
    if sev in ("error", "err"):
        return "high"
    if sev in ("warning", "warn"):
        return "warning"
    return "news"


def _panel_bucket(sev: str) -> str:
    if sev in ("danger", "high"):
        return "alerts"
    if sev == "warning":
        return "warnings"
    return "news"


def _iso_time(value: Optional[str]) -> str:
    """HH:MM:SS de un timestamp ISO, sin convertir de zona."""
    text = str(value or "")
    if "T" in text and len(text) >= 19:
        return text[11:19]
    return text[:19] or "--:--:--"


# Normalización de mensajes para agrupar "familias" de eventos. Los equipos
# emiten el mismo problema con distinto serial, MAC, IP, usuario, PON/ONU o
# valor de medición; sin esto el panel mostraría cientos de filas casi iguales.
_MAC_RE = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_USER_RE = re.compile(r"\buser\s+\S+", re.I)
_HASH_RE = re.compile(r"^<[0-9a-f]{3,10}>:\s*", re.I)
_PPPOE_RE = re.compile(r"<pppoe-[^>]+>", re.I)
_LOGIN_RE = re.compile(r"^\S+\s+logged\s+(in|out)", re.I)
_SERIAL_RE = re.compile(r"\b(?=[0-9a-z]*[a-z])(?=[0-9a-z]*\d)[0-9a-z]{8,}\b", re.I)
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


def _pattern_key(event: dict) -> tuple:
    """(tag, mensaje normalizado) que identifica una familia de eventos."""
    msg = str(event.get("message") or "")
    msg = _HASH_RE.sub("", msg)
    msg = _MAC_RE.sub("<mac>", msg)
    msg = _IP_RE.sub("<ip>", msg)
    msg = _USER_RE.sub("user <usuario>", msg)
    msg = _PPPOE_RE.sub("<pppoe>", msg)
    msg = _LOGIN_RE.sub(lambda m: "<usuario> logged " + m.group(1).lower(), msg)
    msg = _SERIAL_RE.sub("<serial>", msg)
    msg = _NUM_RE.sub("<n>", msg)
    msg = _WS_RE.sub(" ", msg).strip().lower()
    tag = str(event.get("tag") or event.get("program") or "").strip().lower()
    return (tag, msg)


# ==================== EVENTOS (tarjetas del dashboard) ====================

# Los mensajes traen escapes del colector ("PON 0\/9"); se limpian antes de
# interpretar el puerto, la ONU y el serial.
_SLASH_RE = re.compile(r"\\([/\"])")

# Tipos de evento recognized por su texto. El orden importa: el patrón más
# específico va primero ("Port Los ... LINK DOWN" es Port LOS, no link down).
_EVENT_TYPES = (
    ("bip8", "Errores BIP8 en ONU", r"\bbip8\b"),
    ("dying_gasp", "Corte de energía en ONU (Dying Gasp)", r"dying\s+gasp"),
    ("port_los", "Pérdida de señal en puerto de ONU (Port LOS)", r"port\s+los"),
    ("offline", "ONU fuera de línea", r"\boffline\b"),
    ("online", "ONU en línea", r"\bonline\b"),
    ("link_down", "Enlace caído", r"link\s+down"),
    ("link_up", "Enlace restablecido", r"link\s+up"),
    ("auth_fail", "Fallo de autenticación PPPoE", r"authentication\s+failed"),
    ("netwatch_down", "Host sin respuesta (netwatch)", r"event\s+down"),
    ("netwatch_up", "Host recuperado (netwatch)", r"event\s+up"),
    ("pppoe_conn", "Sesión PPPoE establecida", r"connection\s+established|:\s*connected"),
    ("pppoe_auth", "Autenticación PPPoE completada", r":\s*authenticated"),
    ("pppoe_close", "Cierre de sesión PPPoE", r"terminating|closed|disconnected"),
    ("deregister", "ONU desregistrada", r"\bderegister\b"),
    ("register", "ONU registrada", r"\bregister\b"),
    ("config_change", "Cambio de configuración en el equipo", r"config\s+changed"),
    ("login", "Inicio de sesión en el equipo", r"logged\s+in"),
    ("logout", "Cierre de sesión en el equipo", r"logged\s+out|logouted"),
)

_PON_RE = re.compile(r"\bpon\s+(\d+\s*/\s*\d+)", re.I)
_ONU_RE = re.compile(r"\bonu\s+(\d+)\b", re.I)
_SN_RE = re.compile(r"\bsn\s+([0-9a-z]{6,})\b", re.I)
_HOST_RE = re.compile(r"\bhost:\s*([0-9a-z\.\-]+)", re.I)
_PPPOE_USER_RE = re.compile(r"\buser\s+([^\s]+)", re.I)
_VALUE_RE = re.compile(r"\bvalue\s+(-?\d+)", re.I)

_TYPE_TITLES = {tid: title for tid, title, _ in _EVENT_TYPES}


def _clean_message(message: str) -> str:
    """Quita los escapes del colector ("PON 0\\/9" -> "PON 0/9")."""
    return _SLASH_RE.sub(r"\1", str(message or ""))


def _event_type(message: str) -> str:
    """Tipo canónico del evento según su texto ("bip8", "dying_gasp"...)."""
    text = _clean_message(message).lower()
    for tid, _title, pattern in _EVENT_TYPES:
        if re.search(pattern, text):
            return tid
    return ""


def _event_subject(event: dict, etype: str) -> str:
    """Sujeto del evento en equipos que no son OLT: host vigilado o usuario.

    Es lo que hace accionable el evento: "host 10.0.20.154 sin respuesta" o
    "usuario PENGCONGC1 fallando la autenticación".
    """
    text = _clean_message(event.get("message") or "")
    if etype in ("netwatch_down", "netwatch_up"):
        m = _HOST_RE.search(text)
        if m:
            return m.group(1)
    if etype in ("auth_fail", "login", "logout"):
        m = _PPPOE_USER_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _event_facts(event: dict, etype: str) -> dict:
    """Datos concretos del evento: puerto PON, ONU, serial, host y usuario."""
    text = _clean_message(event.get("message") or "")
    facts = {"pon_port": "", "onu_index": "", "onu_serial": "", "host": "", "user": ""}
    m = _PON_RE.search(text)
    if m:
        facts["pon_port"] = "GPON" + re.sub(r"\s+", "", m.group(1))
    m = _ONU_RE.search(text)
    if m:
        facts["onu_index"] = m.group(1)
    m = _SN_RE.search(text)
    if m:
        facts["onu_serial"] = m.group(1).upper()
    m = _HOST_RE.search(text)
    if m:
        facts["host"] = m.group(1)
    m = _PPPOE_USER_RE.search(text)
    if m:
        facts["user"] = m.group(1)
    return facts


def _event_key(event: dict, etype: str, facts: dict) -> tuple:
    """Clave que agrupa el mismo problema real.

    - OLT: equipo + puerto PON + tipo de evento (las ONUs van dentro del
      evento, porque un puerto con varias ONUs caídas es un solo problema).
    - MikroTik: equipo + tipo + sujeto (host vigilado o usuario PPPoE).
    """
    group = _event_group(event)
    device = event.get("device_name") or event.get("device_ip") or ""
    if group == "olt":
        return (group, device, facts.get("pon_port") or "", etype)
    subject = _event_subject(event, etype)
    return (group, device, etype, subject)


def _scan_events(minutes: int) -> list:
    """Lee los eventos del ZSET dentro de la ventana (más recientes primero)."""
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrevrangebyscore("syslog:recent", now, cutoff, start=0, num=_SCAN_LIMIT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    events = []
    for raw in items:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        _with_effective_severity(d)
        events.append(d)
    return events


def _aggregate_events(events: list) -> tuple:
    """Agrupa eventos en "eventos" reales y calcula los conteos por bucket.

    Devuelve ``(groups, counts)`` donde ``groups`` mapea clave -> evento
    agregado y ``counts`` mantiene el volumen crudo (indicadores del menú) y
    el número de eventos distintos por bucket (tarjetas).
    """
    groups = {}
    counts = {
        g: {
            "news": 0, "warnings": 0, "alerts": 0,
            "high": 0, "danger": 0,
            "families": {"news": 0, "warnings": 0, "alerts": 0},
            "families_total": 0,
            "raw": 0,
        }
        for g in _GROUP_LABELS
    }
    for d in events:
        group = _event_group(d)
        sev = _panel_severity(d)
        bucket = _panel_bucket(sev)
        message = _clean_message(d.get("message") or "")
        etype = _event_type(message)
        facts = _event_facts(d, etype)
        received = d.get("processed_at") or d.get("timestamp") or ""

        counts[group]["raw"] += 1
        counts[group][bucket] += 1
        if sev in ("high", "danger"):
            counts[group][sev] += 1

        key = _event_key(d, etype, facts)
        item = groups.get(key)
        if item is None:
            tag = str(d.get("tag") or d.get("program") or "")
            title = _TYPE_TITLES.get(etype) or ("Evento de " + (tag or "equipo"))
            item = {
                "key": "|".join(str(k) for k in key),
                "kind": "syslog",
                "group": group,
                "type": etype or "generic",
                "title": title,
                "sev": sev,
                "bucket": bucket,
                "device": d.get("device_name") or d.get("device_ip") or "",
                "device_ip": d.get("device_ip") or "",
                "device_type": d.get("device_type") or "",
                "host": d.get("host") or d.get("fromhost_ip") or d.get("device_ip") or "",
                "facility": d.get("facility") or "",
                "program": d.get("program") or "",
                "tag": tag,
                "pon_port": facts["pon_port"],
                "onu_index": facts["onu_index"],
                "onu_serials": [],
                "subject": _event_subject(d, etype),
                "user": facts["user"],
                "count": 0,
                "first_seen": received,
                "last_seen": received,
                "last_time": _iso_time(received),
                "sample": message,
                "values": [],
            }
            groups[key] = item
            counts[group]["families"][bucket] += 1
            counts[group]["families_total"] += 1

        item["count"] += 1
        if received and (not item["first_seen"] or received < item["first_seen"]):
            item["first_seen"] = received
        if received and (not item["last_seen"] or received > item["last_seen"]):
            item["last_seen"] = received
            item["last_time"] = _iso_time(received)
            item["sample"] = message
        serial = facts["onu_serial"]
        if serial and serial not in item["onu_serials"]:
            item["onu_serials"].append(serial)
        m = _VALUE_RE.search(message)
        if m and m.group(1) not in item["values"]:
            item["values"].append(m.group(1))
    return groups, counts


_SEV_RANK = {"danger": 0, "high": 1, "warning": 2, "news": 3}


def _sorted_events(groups: dict) -> list:
    """Eventos ordenados: primero los graves, luego los más repetidos."""
    items = list(groups.values())
    items.sort(key=lambda e: (
        _SEV_RANK.get(e.get("sev"), 9),
        -int(e.get("count") or 0),
        str(e.get("last_seen") or ""),
    ), reverse=False)
    return items


@router.get("/events")
def get_syslog_events(
    minutes: int = Query(360, ge=1, le=10080),
    limit: int = Query(400, ge=1, le=2000),
):
    """Eventos agrupados por problema real (equipo + puerto + tipo).

    Cada evento es una tarjeta del dashboard: un problema concreto que un
    operador puede recorrer con nombre de equipo, puerto, ONU y número de
    repeticiones. Las repeticiones del mismo problema se colapsan en un solo
    evento con su ``count`` y su primer/último avistamiento.
    """
    events = _scan_events(minutes)
    groups, counts = _aggregate_events(events)
    items = _sorted_events(groups)
    return {
        "ok": True,
        "window_minutes": minutes,
        "scanned": len(events),
        "truncated": len(events) >= _SCAN_LIMIT,
        "labels": _GROUP_LABELS,
        "counts": counts,
        "events": items[:limit],
        "events_total": len(items),
    }


@router.get("/event")
def get_syslog_event(
    minutes: int = Query(360, ge=1, le=10080),
    device_ip: str = Query("", alias="device_ip"),
    type: str = Query("", alias="type"),
    pon_port: str = Query("", alias="pon_port"),
    subject: str = Query("", alias="subject"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Ocurrencias concretas de un evento (las líneas que lo componen).

    Se filtran por equipo, tipo, puerto PON y sujeto; así el modal puede
    mostrar el historial real del problema, no una familia difusa.
    """
    events = _scan_events(minutes)
    out = []
    for d in events:
        message = _clean_message(d.get("message") or "")
        etype = _event_type(message)
        facts = _event_facts(d, etype)
        if device_ip and str(d.get("device_ip") or "") != str(device_ip):
            continue
        if type and (etype or "generic") != type:
            continue
        if pon_port and facts["pon_port"] != pon_port:
            continue
        if subject and _event_subject(d, etype) != subject:
            continue
        received = d.get("processed_at") or d.get("timestamp") or ""
        out.append({
            "time": _iso_time(received),
            "received_at": received,
            "event_at": d.get("timestamp"),
            "message": message,
            "tag": d.get("tag") or d.get("program") or "",
            "program": d.get("program") or "",
            "device": d.get("device_name") or d.get("device_ip") or "",
            "device_ip": d.get("device_ip") or "",
            "pon_port": facts["pon_port"],
            "onu_index": facts["onu_index"],
            "onu_serial": facts["onu_serial"],
            "host": facts["host"],
            "user": facts["user"],
            "value": (_VALUE_RE.search(message).group(1) if _VALUE_RE.search(message) else ""),
        })
    out.sort(key=lambda e: str(e.get("received_at") or ""), reverse=True)
    return {
        "ok": True,
        "window_minutes": minutes,
        "count": len(out),
        "truncated": len(out) > limit,
        "occurrences": out[:limit],
    }


@router.get("/panels")
def get_syslog_panels(
    minutes: int = Query(360, ge=1, le=10080),
    per_bucket: int = Query(20, ge=1, le=100),
):
    """Paneles de Logs: noticias / advertencias / alertas por tipo de equipo.

    Cada fila agrupa una *familia* de mensajes equivalentes (mismo tag y mismo
    texto tras enmascarar seriales, MACs, IPs, usuarios y números) y muestra el
    ejemplar más reciente con su ``repeats``. Así el operador ve el problema y
    su volumen, no mil variantes del mismo aviso.

    Los conteos del menú usan familias (``news``/``warnings``/``alerts``) y el
    volumen crudo queda en ``events``/``raw``. Se ordena por hora de recepción
    porque algunos equipos (OLTs) traen el reloj desfasado y su ``timestamp``
    no es comparable.
    """
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrevrangebyscore("syslog:recent", now, cutoff, start=0, num=_SCAN_LIMIT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    groups = {g: {"news": [], "warnings": [], "alerts": []} for g in _GROUP_LABELS}
    # ``news``/``warnings``/``alerts`` cuentan *eventos* (los indicadores del
    # menú muestran volumen real); ``families`` cuenta filas distintas.
    counts = {
        g: {
            "news": 0, "warnings": 0, "alerts": 0,
            "high": 0, "danger": 0,
            "families": {"news": 0, "warnings": 0, "alerts": 0},
            "families_total": 0,
            "raw": 0,
        }
        for g in _GROUP_LABELS
    }
    deduped = {}

    for raw in items:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        _with_effective_severity(d)
        group = _event_group(d)
        sev = _panel_severity(d)
        bucket = _panel_bucket(sev)
        received = d.get("processed_at") or d.get("timestamp") or ""

        counts[group]["raw"] += 1
        counts[group][bucket] += 1
        if sev in ("high", "danger"):
            counts[group][sev] += 1

        key = (group,) + _pattern_key(d)
        prev = deduped.get(key)
        if prev is None:
            d["_repeats"] = 1
            d["_first_seen"] = received
            d["_sev"] = sev
            d["_bucket"] = bucket
            deduped[key] = d
            counts[group]["families"][bucket] += 1
            counts[group]["families_total"] += 1
        else:
            prev["_repeats"] = prev.get("_repeats", 1) + 1
            if received and (not prev.get("_first_seen") or received < prev["_first_seen"]):
                prev["_first_seen"] = received

    for d in deduped.values():
        group = _event_group(d)
        groups[group][d.get("_bucket", "news")].append({
            "time": _iso_time(d.get("processed_at") or d.get("timestamp")),
            "received_at": d.get("processed_at"),
            "event_at": d.get("timestamp"),
            "device": d.get("device_name") or d.get("device_ip"),
            "device_ip": d.get("device_ip"),
            "device_type": d.get("device_type"),
            "host": d.get("host") or d.get("fromhost_ip") or d.get("device_ip"),
            "facility": d.get("facility"),
            "program": d.get("program"),
            "tag": d.get("tag") or d.get("program") or "",
            "message": d.get("message") or "",
            "severity": d.get("severity"),
            "severity_effective": d.get("severity_effective"),
            "sev": d.get("_sev"),
            "repeats": d.get("_repeats", 1),
            "first_seen": d.get("_first_seen"),
        })

    for group in groups.values():
        for bucket in group.values():
            bucket.sort(key=lambda e: (e.get("received_at") or ""), reverse=True)
            del bucket[per_bucket:]

    return {
        "ok": True,
        "window_minutes": minutes,
        "scanned": len(items),
        "truncated": len(items) >= _SCAN_LIMIT,
        "labels": _GROUP_LABELS,
        "groups": groups,
        "counts": counts,
    }


@router.get("/important")
def get_syslog_important(
    minutes: int = Query(360, ge=1, le=10080),
    limit: int = Query(100, ge=1, le=500),
    take: int = Query(50, ge=1, le=100),
):
    """Eventos filtrados como 'importantes' para mostrar en dashboard/LLM.

    ``take`` se aplica por columna del panel (noticias, warnings, errores)
    para que una tormenta de eventos informativos no desplace a los errores
    reales de la lista de los más recientes.
    """
    r = _get_redis()
    now = time.time()
    cutoff = now - minutes * 60
    try:
        items = r.zrangebyscore("syslog:recent", cutoff, now)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    buckets = {"news": [], "warnings": [], "errors": []}
    deduped = {}
    for s in items:
        try:
            d = json.loads(s)
        except Exception:
            continue
        _with_effective_severity(d)
        important, reason = is_important(d)
        if not important:
            continue
        d["_important_reason"] = reason or "pattern"

        key = _dedupe_key(d)
        prev = deduped.get(key)
        if prev is None:
            d["_repeat_count"] = 1
            d["_first_seen"] = d.get("timestamp")
            deduped[key] = d
        else:
            prev["_repeat_count"] = prev.get("_repeat_count", 1) + 1
            seen = [t for t in (prev.get("_first_seen"), d.get("timestamp")) if t]
            first_seen = min(seen) if seen else None
            if (d.get("timestamp") or "") >= (prev.get("timestamp") or ""):
                d["_repeat_count"] = prev["_repeat_count"]
                d["_first_seen"] = first_seen
                deduped[key] = d
            else:
                prev["_first_seen"] = first_seen

    for d in deduped.values():
        buckets[_severity_bucket(d)].append(d)

    out = []
    for bucket in buckets.values():
        bucket.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        out.extend(bucket[:take])
    out.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"ok": True, "count": len(out), "events": out}
