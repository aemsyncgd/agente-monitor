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
