#!/usr/bin/env bash
#
# rotate_mikrotik_log.sh — Rotación del all.jsonl de MikroTik/OLTs.
#
# El archivo lo escribe rsyslog dentro del contenedor "mikrotik-syslog" con
# el usuario syslog. El directorio no es escribible por el usuario del host,
# así que la rotación se hace dentro del contenedor con `podman exec`.
#
# Uso:
#   ./scripts/rotate_mikrotik_log.sh
#
# Variables:
#   MAX_BYTES   Tamaño a partir del cual se rota (por defecto 1 GiB).
#   KEEP        Rotaciones históricas a conservar (por defecto 3).
#   COMPRESS    1 = gzip de la rotación (por defecto 1).
#   DRY_RUN     1 = solo mostrar lo que haría.
#
set -euo pipefail

CONTAINER="${CONTAINER:-mikrotik-syslog}"
LOG_DIR="/var/log/mikrotik"
LOG_NAME="all.jsonl"
LOG_PATH="${LOG_DIR}/${LOG_NAME}"

MAX_BYTES="${MAX_BYTES:-1073741824}"   # 1 GiB
KEEP="${KEEP:-3}"
COMPRESS="${COMPRESS:-1}"
DRY_RUN="${DRY_RUN:-0}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

command -v podman >/dev/null 2>&1 || { log "podman no disponible"; exit 0; }

if ! podman container exists "${CONTAINER}" >/dev/null 2>&1; then
    log "aviso: el contenedor ${CONTAINER} no existe; nada que rotar"
    exit 0
fi
if [ "$(podman inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" != "true" ]; then
    log "aviso: el contenedor ${CONTAINER} no está corriendo; nada que rotar"
    exit 0
fi

size="$(podman exec "${CONTAINER}" stat -c %s "${LOG_PATH}" 2>/dev/null || echo 0)"
if [ -z "${size}" ] || [ "${size}" -lt "${MAX_BYTES}" ]; then
    log "sin rotación: ${LOG_NAME} = ${size} bytes (umbral ${MAX_BYTES})"
    exit 0
fi

stamp="$(date '+%Y%m%d-%H%M%S')"
rotated="${LOG_PATH}.${stamp}"

if [ "${DRY_RUN}" = "1" ]; then
    log "DRY_RUN: rotaría ${LOG_NAME} (${size} bytes) -> ${rotated} (compress=${COMPRESS}, keep=${KEEP})"
    exit 0
fi

# 1) Renombrar y pedir a rsyslog que reabra el archivo (SIGHUP).
#    Se hace en ese orden para que deje de escribir en el inode antiguo
#    antes de comprimirlo.
podman exec "${CONTAINER}" sh -c "mv '${LOG_PATH}' '${rotated}' && kill -HUP 1"
log "rotado: ${LOG_NAME} (${size} bytes) -> $(basename "${rotated}")"

# 2) Comprimir la rotación (fuera del camino crítico de escritura).
if [ "${COMPRESS}" = "1" ]; then
    sleep 2
    podman exec "${CONTAINER}" sh -c "gzip -1 '${rotated}'" || log "aviso: no se pudo comprimir ${rotated}"
fi

# 3) Conservar solo las KEEP rotaciones más recientes.
podman exec "${CONTAINER}" sh -c \
    "ls -1t ${LOG_DIR}/${LOG_NAME}.* 2>/dev/null | tail -n +\$(( ${KEEP} + 1 )) | xargs -r rm -f" \
    || log "aviso: no se pudieron limpiar rotaciones antiguas"

log "rotación completada (conservadas: ${KEEP})"
