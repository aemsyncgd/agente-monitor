#!/usr/bin/env python3
"""Replicador de syslog en tiempo real desde otro agente-monitor.

Los equipos pueden apuntar una sola instancia (agente-monitor-1). Este
proceso consulta su API de syslog y copia los eventos nuevos al Redis de
syslog de este LXC (127.0.0.1:6380), manteniendo el mismo esquema JSON que
usa `scripts/log_monitor.py`.

No reenvía paquetes syslog: replica eventos ya procesados por el otro
agente. La deduplicación se hace con un ZSET de hashes; el ZSET
`syslog:recent` de destino también deduplica por miembro (el JSON completo).

Variables de entorno:
    AGENT1_URL              URL base del otro agente (ej. http://IP_DEL_AGENTE:8000)
    AGENT1_API_KEY          API key para X-API-Key
    AGENT1_POLL_SECONDS     Intervalo de sondeo (default 5)
    AGENT1_WINDOW_MINUTES   Ventana consultada (default 10)
    AGENT1_LIMIT            Máximo de eventos por consulta (default 1000)
    SYSLOG_REDIS_HOST       default 127.0.0.1
    SYSLOG_REDIS_PORT       default 6380
"""

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

import redis

BASE_URL = os.getenv("AGENT1_URL", "").rstrip("/")
API_KEY = os.getenv("AGENT1_API_KEY", "")
POLL_SECONDS = int(os.getenv("AGENT1_POLL_SECONDS", "5"))
WINDOW_MINUTES = int(os.getenv("AGENT1_WINDOW_MINUTES", "10"))
LIMIT = int(os.getenv("AGENT1_LIMIT", "1000"))

REDIS_HOST = os.getenv("SYSLOG_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("SYSLOG_REDIS_PORT", "6380"))

SEEN_KEY = "syslog:replicator:seen"
RECENT_KEY = "syslog:recent"
SEVEN_DAYS = 7 * 24 * 3600
THIRTY_DAYS = 30 * 24 * 3600
ZSET_MAX = 10000
DEVICE_RECENT_MAX = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("syslog_replicator")


def fetch_events():
    """Devuelve los eventos del otro agente, o lista vacía si falla."""
    if not BASE_URL or not API_KEY:
        raise RuntimeError("Faltan AGENT1_URL o AGENT1_API_KEY")

    url = (
        f"{BASE_URL}/api/v1/syslog/recent"
        f"?minutes={WINDOW_MINUTES}&limit={LIMIT}"
    )
    req = urllib.request.Request(url, headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("events", [])


def event_key(event):
    """Hash estable del evento, ignorando processed_at (cambia al importar)."""
    stable = {k: v for k, v in event.items() if k not in ("processed_at", "score")}
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def event_score(index, total, now):
    """Score para el ZSET: momento de recepción en este LXC.

    No se usan los tiempos del agente origen: ``timestamp`` puede venir con
    la fecha del equipo (se vieron eventos de mayo) y ``processed_at`` usa la
    hora local del agente con desfase (UTC-4 etiquetado como UTC). Con hora de
    recepción el evento siempre cae dentro de la ventana de consulta local y
    el retention de 7 días funciona. El pequeño desplazamiento por índice
    conserva el orden relativo de los eventos de un mismo lote.
    """
    return now - (total - index) * 0.001


def import_events(redis_client, events):
    imported = 0
    now = time.time()

    for index, event in enumerate(events):
        key = event_key(event)
        if redis_client.zscore(SEEN_KEY, key) is not None:
            continue

        payload = json.dumps(event, ensure_ascii=False)
        score = event_score(index, len(events), now)
        device_ip = str(
            event.get("device_ip")
            or event.get("fromip")
            or event.get("srcip")
            or ""
        ).strip()

        pipe = redis_client.pipeline(transaction=False)
        pipe.zadd(SEEN_KEY, {key: score})
        pipe.zadd(RECENT_KEY, {payload: score})
        if device_ip:
            device_key = f"syslog:device:{device_ip}:recent"
            pipe.lpush(device_key, payload)
            pipe.ltrim(device_key, 0, DEVICE_RECENT_MAX - 1)
            pipe.expire(device_key, THIRTY_DAYS)
        pipe.execute()
        imported += 1

    if imported:
        pipe = redis_client.pipeline(transaction=False)
        pipe.zremrangebyscore(RECENT_KEY, 0, -(ZSET_MAX + 1))
        pipe.expire(RECENT_KEY, SEVEN_DAYS)
        pipe.zremrangebyscore(SEEN_KEY, 0, now - SEVEN_DAYS)
        pipe.expire(SEEN_KEY, SEVEN_DAYS)
        pipe.execute()

    return imported


def main():
    logger.info(
        "Replicador syslog iniciado | %s -> %s:%s | poll=%ss window=%sm limit=%s",
        BASE_URL or "(sin URL)",
        REDIS_HOST,
        REDIS_PORT,
        POLL_SECONDS,
        WINDOW_MINUTES,
        LIMIT,
    )

    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=0,
        decode_responses=True,
        socket_connect_timeout=5,
    )

    while True:
        try:
            events = fetch_events()
            imported = import_events(client, events)
            if imported:
                logger.info("Importados %s eventos nuevos", imported)
            else:
                logger.debug("Sin eventos nuevos (%s consultados)", len(events))
        except KeyboardInterrupt:
            logger.info("Replicador detenido")
            return 0
        except Exception as exc:
            logger.error("Error en ciclo de replicación: %s", exc)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
