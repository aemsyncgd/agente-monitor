#!/usr/bin/env python3
"""
Log Monitor para Agente-Monitor
Lee logs JSONL generados por rsyslog (MikroTik/OLTs) y los envía filtrados a Redis
para que el Agente LLM pueda analizarlos en tiempo real.
"""

import json
import time
import os
import re
from datetime import datetime
from pathlib import Path
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
import yaml
import logging
import sys

# systemd ejecuta este script directamente (/usr/bin/python3 scripts/log_monitor.py),
# así que sys.path[0] es scripts/: hay que añadir la raíz del repo para importar src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.syslog_severity import effective_severity

# ==================== CONFIGURACIÓN ====================
LOG_FILE = "/home/server/agente-monitor/mikrotik-syslog/logs/all.jsonl"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6380
REDIS_DB = 0
REDIS_QUEUE = "cola_logs_para_llm"
TAIL_SLEEP = 0.1
NODES_YAML = "/home/server/agente-monitor/config/nodes.yaml"
LLM_CONFIG = "/home/server/agente-monitor/config/llm_config.json"

# Rendimiento: agrupa escrituras en Redis y evita trabajo por línea
BATCH_MAX = 500                    # eventos máximos por pipeline
ZSET_MAX = 10000                   # tamaño de syslog:recent
DEVICE_RECENT_MAX = 1000           # tamaño de syslog:device:<ip>:recent
DEVICE_MAP_RELOAD_SECONDS = 60     # recarga nodes.yaml como mucho 1 vez/min

# ==================== LOGGING DEL PROPIO SCRIPT ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("log_monitor")

# ==================== MAPEO DE DISPOSITIVOS DESDE NODES.YAML ====================
DEVICE_MAP = {}
LAST_NODES_MTIME = 0

def load_device_map():
    global DEVICE_MAP, LAST_NODES_MTIME
    try:
        if not os.path.exists(NODES_YAML):
            logger.warning(f"No se encuentra {NODES_YAML}, usando mapeo vacío")
            return
        mtime = os.path.getmtime(NODES_YAML)
        if mtime == LAST_NODES_MTIME and DEVICE_MAP:
            return
        with open(NODES_YAML, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        new_map = {}
        for node_group in data.get('nodes', []):
            group_name = node_group.get('name') or ''
            # MikroTiks
            for mk in node_group.get('mikrotiks', []) or []:
                ip = str(mk.get('ip', '')).strip()
                if not ip:
                    continue
                new_map[ip] = {
                    'device_ip': ip,
                    'device_name': mk.get('hostname') or mk.get('name') or ip,
                    'device_type': 'mikrotik',
                    'node_id': mk.get('hostname') or ip,
                    'site': group_name,
                    'role': mk.get('role') or '',
                    'modelo': mk.get('modelo') or '',
                    'conectado_a': mk.get('conectado_a') or ''
                }
            # OLTs
            for olt in node_group.get('olts', []) or []:
                ip = str(olt.get('ip', '')).strip()
                if not ip:
                    continue
                new_map[ip] = {
                    'device_ip': ip,
                    'device_name': olt.get('hostname') or olt.get('descripcion') or ip,
                    'device_type': 'olt',
                    'node_id': olt.get('hostname') or ip,
                    'site': group_name,
                    'modelo': olt.get('modelo') or '',
                    'pon_count': olt.get('pon_count'),
                    'conectado_a': olt.get('conectado_a') or ''
                }
        DEVICE_MAP = new_map
        LAST_NODES_MTIME = mtime
        logger.info(f"Mapa de dispositivos cargado: {len(DEVICE_MAP)} entradas desde nodes.yaml")
    except Exception as e:
        logger.warning(f"Error cargando nodes.yaml: {e}")

LAST_MAP_CHECK = 0.0

def maybe_reload_device_map():
    """Recarga nodes.yaml como máximo cada DEVICE_MAP_RELOAD_SECONDS.

    Antes se llamaba a load_device_map() por cada línea (un os.stat por línea,
    ~424/s). Ahora el stat se hace como mucho una vez por minuto.
    """
    global LAST_MAP_CHECK
    ahora = time.monotonic()
    if ahora - LAST_MAP_CHECK < DEVICE_MAP_RELOAD_SECONDS:
        return
    LAST_MAP_CHECK = ahora
    load_device_map()

# ==================== ESTADO DEL LLM ====================
LLM_STATE = {"mtime": None, "enabled": True}

def llm_habilitado():
    """Lee config/llm_config.json (con caché por mtime).

    Si el LLM está deshabilitado no se encola nada en cola_logs_para_llm.
    """
    try:
        mtime = os.path.getmtime(LLM_CONFIG)
        if LLM_STATE["mtime"] != mtime:
            with open(LLM_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            LLM_STATE["enabled"] = bool(cfg.get("enabled", True))
            LLM_STATE["mtime"] = mtime
            logger.info(f"LLM {'habilitado' if LLM_STATE['enabled'] else 'deshabilitado'} (config)")
        return LLM_STATE["enabled"]
    except Exception:
        # Ante la duda, preservar el comportamiento anterior (no perder eventos)
        return True

# ==================== CONEXIÓN A REDIS ====================
def get_redis_client():
    while True:
        try:
            client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5,
                retry_on_timeout=True
            )
            client.ping()
            logger.info(f"✅ Conectado a Redis en {REDIS_HOST}:{REDIS_PORT}")
            return client
        except RedisConnectionError as e:
            logger.warning(f"⚠️  No se pudo conectar a Redis ({e}). Reintentando en 5s...")
            time.sleep(5)

# ==================== FILTRO DE EVENTOS RELEVANTES ====================
KEYWORDS_RELEVANTES = [
    'error', 'fail', 'failed', 'failure',
    'warning', 'warn',
    'critical', 'alert',
    'down', 'up', 'link', 'flap',
    'denied', 'drop', 'blocked',
    'login', 'logout', 'auth', 'authentication', 'invalid', 'wrong password',
    'dhcp', 'lease',
    'pppoe', 'radius',
    'interface', 'ether', 'sfp', 'fiber',
    'temperature', 'voltage', 'fan',
    'reboot', 'restart', 'shutdown',
    'backup', 'config', 'changed',
    'ospf', 'bgp', 'routing',
    'firewall', 'nat',
    'user', 'account'
]

SEVERIDADES_RELEVANTES = ['warning', 'err', 'crit', 'alert', 'emerg', 'error', 'critical']

# Tags de depuración (p.ej. "pppoe,ppp,debug,packet"): ruido de paquetes PPPoE.
# Se descartan salvo que la severidad sea realmente relevante.
DEBUG_TAG_RE = re.compile(r"(^|,)debug(,|$)")

# El relay del host (scripts/syslog_relay.py) antepone la IP real del equipo al
# mensaje porque el NAT de podman rootless la pierde. Se extrae aquí y se usa
# como device_ip; la marca se elimina para no ensuciar el panel.
SRCIP_MARK_RE = re.compile(r"^\s*__SRCIP__=([0-9A-Fa-f:.]+)\s?")

def es_linea_debug(log_data):
    tag = str(log_data.get('tag', '')).lower()
    if not DEBUG_TAG_RE.search(tag):
        return False
    # RouterOS manda severity=notice en casi todo: el nivel real está en el tag.
    severity = effective_severity(tag, log_data.get('severity'))
    return severity not in SEVERIDADES_RELEVANTES

def es_evento_relevante(log_data):
    tag = str(log_data.get('tag', '')).lower()
    severity = effective_severity(tag, log_data.get('severity'))
    message = str(log_data.get('message', '')).lower()
    program = str(log_data.get('program', '')).lower()
    
    if severity in SEVERIDADES_RELEVANTES:
        return True
    if any(kw in message for kw in KEYWORDS_RELEVANTES):
        return True
    if any(kw in tag for kw in KEYWORDS_RELEVANTES):
        return True
    if 'denied' in message or 'denied' in tag:
        return True
    if log_data.get('device_type') == 'mikrotik' and ('system' in tag or 'account' in tag):
        if any(kw in message for kw in ['login', 'logout', 'changed', 'added', 'removed']):
            return True
    return False

# ==================== PROCESAMIENTO DE LÍNEA ====================
def procesar_log(linea):
    """Parsea y enriquece una línea. Devuelve un evento listo para Redis o None.

    Descarta el ruido de debug y los eventos no relevantes antes de tocar Redis.
    """
    linea = linea.strip()
    if not linea:
        return None
    try:
        log_data = json.loads(linea)
    except json.JSONDecodeError:
        logger.debug(f"Línea no es JSON válido: {linea[:100]}")
        return None

    # El relay añade "__SRCIP__=<ip real>" al inicio del mensaje. Se restaura
    # como IP de origen antes de enriquecer y se quita del texto mostrado.
    message = str(log_data.get("message") or "")
    srcip_match = SRCIP_MARK_RE.match(message)
    if srcip_match:
        src_ip = srcip_match.group(1)
        log_data["srcip"] = src_ip
        log_data["fromip"] = src_ip
        log_data["fromhost_ip"] = src_ip
        log_data["message"] = SRCIP_MARK_RE.sub("", message, count=1)

    # rsyslog puede conservar los dos puntos finales en syslogtag
    # ("script,error:"). Se normaliza para que panel, filtros y deduplicación
    # vean siempre el mismo tag.
    for campo in ("tag", "program"):
        valor = log_data.get(campo)
        if isinstance(valor, str) and valor.endswith(":"):
            log_data[campo] = valor.rstrip(":")

    # 95%+ del syslog es "pppoe,ppp,debug,packet": no aporta a la detección
    if es_linea_debug(log_data):
        return None

    fromip = log_data.get('fromip') or log_data.get('host') or ''
    device_info = DEVICE_MAP.get(fromip, {})
    enriched = dict(log_data)
    enriched['device_ip'] = fromip
    enriched['device_name'] = device_info.get('device_name') or fromip
    enriched['device_type'] = device_info.get('device_type') or 'unknown'
    enriched['node_id'] = device_info.get('node_id') or fromip
    enriched['site'] = device_info.get('site') or ''
    enriched['role'] = device_info.get('role') or ''
    enriched['modelo'] = device_info.get('modelo') or ''
    enriched['processed_at'] = datetime.now().isoformat()
    enriched['severity_effective'] = effective_severity(
        enriched.get('tag'), enriched.get('severity')
    )

    # Solo se guardan eventos relevantes (antes se guardaba todo el flujo)
    if not es_evento_relevante(enriched):
        return None

    return {
        "json": json.dumps(enriched, ensure_ascii=False),
        "ts": time.time(),
        "ip": fromip,
    }


def flush_lote(redis_client, lote):
    """Escribe un lote de eventos en un único pipeline de Redis."""
    if not lote:
        return
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.zadd('syslog:recent', {ev["json"]: ev["ts"] for ev in lote})
        pipe.zremrangebyrank('syslog:recent', 0, -(ZSET_MAX + 1))  # últimos ZSET_MAX
        pipe.expire('syslog:recent', 60*60*24*7)  # 7 días

        por_dispositivo = {}
        for ev in lote:
            if ev["ip"]:
                key = f'syslog:device:{ev["ip"]}:recent'
                por_dispositivo.setdefault(key, []).append(ev["json"])
        for key, items in por_dispositivo.items():
            pipe.lpush(key, *items[:DEVICE_RECENT_MAX])
            pipe.ltrim(key, 0, DEVICE_RECENT_MAX - 1)  # últimos 1000 por dispositivo
            pipe.expire(key, 60*60*24*30)  # 30 días

        # Solo encolar para el LLM si está habilitado: si está apagado no hay
        # consumidor y la cola crecía sin límite.
        if llm_habilitado():
            pipe.lpush(REDIS_QUEUE, *(ev["json"] for ev in lote))

        pipe.execute()
    except Exception as e:
        logger.error(f"Error guardando lote en Redis: {e}")

# ==================== TAIL CON DETECCIÓN DE ROTACIÓN ====================
def tail_log(redis_client):
    logger.info(f"🚀 Iniciando monitoreo de: {LOG_FILE}")
    if not os.path.exists(LOG_FILE):
        logger.warning(f"Archivo {LOG_FILE} no existe aún. Esperando...")
        while not os.path.exists(LOG_FILE):
            time.sleep(2)
    
    f = open(LOG_FILE, 'r', encoding='utf-8', errors='ignore')
    # Ir al final
    f.seek(0, 2)
    inode = os.fstat(f.fileno()).st_ino
    logger.info(f"📄 Archivo abierto (inode: {inode})")

    lote = []
    while True:
        try:
            linea = f.readline()
            if not linea:
                # Vaciar lo acumulado y, ya en reposo, recargar el mapa de nodos
                flush_lote(redis_client, lote)
                lote = []
                maybe_reload_device_map()
                # Verificar rotación
                try:
                    new_inode = os.stat(LOG_FILE).st_ino
                    if new_inode != inode:
                        logger.info("🔄 Rotación de log detectada, reabriendo archivo")
                        f.close()
                        f = open(LOG_FILE, 'r', encoding='utf-8', errors='ignore')
                        inode = new_inode
                        logger.info(f"📄 Archivo reabierto (inode: {inode})")
                except FileNotFoundError:
                    logger.warning("Archivo no encontrado, esperando...")
                    time.sleep(2)
                    continue
                time.sleep(TAIL_SLEEP)
                continue
            ev = procesar_log(linea)
            if ev is not None:
                lote.append(ev)
                if len(lote) >= BATCH_MAX:
                    flush_lote(redis_client, lote)
                    lote = []
        except Exception as e:
            lote = []
            logger.error(f"Error en tail_log: {e}")
            time.sleep(5)

# ==================== BUCLE PRINCIPAL CON RECONECCIÓN ====================
def main():
    logger.info("🔧 Iniciando Log Monitor para Agente-Monitor")
    load_device_map()
    while True:
        try:
            redis_client = get_redis_client()
            tail_log(redis_client)
        except KeyboardInterrupt:
            logger.info("⛔ Monitor detenido por usuario")
            break
        except Exception as e:
            logger.error(f"Error fatal: {e}. Reintentando en 10s...")
            time.sleep(10)

if __name__ == '__main__':
    main()
