#!/usr/bin/env python3
"""
log_monitor.py - Monitor de logs MikroTik/OLTs para el Agente LLM
Lee en tiempo real los logs JSONL de rsyslog, filtra eventos relevantes
y los encola en Redis para procesamiento asíncrono por el LLM.
"""

import json
import time
import os
import sys
import logging
import redis
from redis.exceptions import ConnectionError as RedisConnectionError

# ==================== CONFIGURACIÓN ====================
LOG_FILE = "/home/server/agente-monitor/mikrotik-syslog/logs/all.jsonl"
REDIS_HOST = "10.89.6.84"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_QUEUE = "cola_logs_para_llm"
TAIL_SLEEP = 0.1  # segundos entre lecturas

# ==================== LOGGING DEL PROPIO SCRIPT ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("log_monitor")

# ==================== CONEXIÓN A REDIS ====================
def get_redis_client():
    """Crea un cliente Redis con reintentos automáticos"""
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
def es_evento_relevante(log_dict):
    """
    Determina si un log vale la pena ser enviado al LLM.
    Ajusta estas reglas según los patrones reales de tus equipos.
    """
    msg = log_dict.get("message", "").lower()
    severity = log_dict.get("severity", "").lower()
    host = log_dict.get("host", "").lower()
    
    # 1. Todo lo crítico/error/warning pasa directo
    if severity in ["critical", "error", "warning", "alert", "emergency"]:
        return True
    
    # 2. Palabras clave de seguridad y fallos
    keywords_seguridad = [
        "drop", "reject", "fail", "down", "reboot", "attack", 
        "auth", "login", "blocked", "invalid", "disconnect",
        "link down", "link up", "temperature", "overheat"
    ]
    if any(kw in msg for kw in keywords_seguridad):
        return True
    
    # 3. Eventos específicos por hostname (ej: OLTs sensibles)
    hosts_criticos = ["olt", "core", "distribution"]
    if any(hc in host for hc in hosts_criticos):
        return True
    
    return False

# ==================== PROCESAMIENTO DE LÍNEA ====================
def procesar_log(linea, redis_client):
    """Parsea, filtra y encola un log"""
    try:
        log_data = json.loads(linea.strip())
    except json.JSONDecodeError:
        logger.debug(f"Línea no-JSON ignorada: {linea[:80]}")
        return
    
    if es_evento_relevante(log_data):
        try:
            redis_client.lpush(REDIS_QUEUE, json.dumps(log_data))
            logger.info(
                f"🔍 [{log_data.get('host', '?')}] "
                f"{log_data.get('severity', '?').upper()} - "
                f"{log_data.get('message', '')[:60]}"
            )
        except RedisConnectionError:
            logger.error("❌ Redis desconectado. Recreando conexión...")
            raise

# ==================== TAIL CON DETECCIÓN DE ROTACIÓN ====================
def tail_log(redis_client):
    """
    Lee el archivo de log en tiempo real.
    Detecta si logrotate rotó el archivo (inode cambia) y reabre.
    """
    logger.info(f"🚀 Iniciando monitoreo de: {LOG_FILE}")
    
    # Esperar a que el archivo exista
    while not os.path.exists(LOG_FILE):
        logger.warning(f"⏳ Esperando a que {LOG_FILE} sea creado por rsyslog...")
        time.sleep(2)
    
    current_inode = None
    f = None
    
    try:
        while True:
            # Detectar rotación de archivo (logrotate cambió el inode)
            try:
                stat = os.stat(LOG_FILE)
                new_inode = stat.st_ino
            except FileNotFoundError:
                new_inode = None
            
            if new_inode != current_inode:
                # Archivo rotado o creado de nuevo
                if f:
                    f.close()
                f = open(LOG_FILE, 'r')
                f.seek(0, 2)  # Ir al final
                current_inode = new_inode
                logger.info(f"📄 Archivo abierto (inode: {current_inode})")
            
            linea = f.readline()
            if not linea:
                time.sleep(TAIL_SLEEP)
                continue
            
            try:
                procesar_log(linea, redis_client)
            except RedisConnectionError:
                f.close()
                raise
    
    finally:
        if f:
            f.close()

# ==================== BUCLE PRINCIPAL CON RECONECCIÓN ====================
def main():
    logger.info("🔧 Iniciando Log Monitor para Agente-Monitor")
    
    while True:
        try:
            redis_client = get_redis_client()
            tail_log(redis_client)
        except RedisConnectionError:
            logger.warning("🔄 Reconectando a Redis en 5 segundos...")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("🛑 Detenido por el usuario (SIGINT)")
            sys.exit(0)
        except Exception as e:
            logger.exception(f"❌ Error inesperado: {e}. Reiniciando en 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()
