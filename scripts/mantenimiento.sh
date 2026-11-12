#!/usr/bin/env bash
#
# mantenimiento.sh — Arranque/parada ordenada de todo el stack agente-monitor.
#
# Uso:
#   ./scripts/mantenimiento.sh --start   [--with-llm]  Iniciar todo y esperar.
#   ./scripts/mantenimiento.sh --stop                 Detener todo.
#   ./scripts/mantenimiento.sh --restart [--with-llm] Parar y volver a iniciar.
#   ./scripts/mantenimiento.sh --status               Estado de todo el stack.
#   ./scripts/mantenimiento.sh --help                 Mostrar esta ayuda.
#
# Alias heredados: --ready = --start, --now = --stop.
#
# El stack tiene dos capas:
#   1) Infraestructura: agente-monitor-compose.service (podman-compose)
#      -> influxdb-ia, redis-ia, redis-syslog, mikrotik-syslog
#      + relay syslog del host (agente-monitor-syslog-relay.service), que conserva
#        la IP real de cada router al reenviar al contenedor.
#   2) Aplicación: agente-monitor (collector+API+ai-agent), log-monitor,
#      scheduler y bot. El agente LLM (wisp-agent) es opcional (--with-llm).
#
# Todo se maneja con systemctl a nivel de usuario: NO requiere root.
#
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

COMPOSE_UNIT="agente-monitor-compose.service"

# Relay syslog del host: recibe el 5514 de los MikroTik y reenvía al contenedor
# por 127.0.0.1:5515 conservando la IP real del equipo en el mensaje.
RELAY_UNIT="agente-monitor-syslog-relay.service"
RELAY_PORT="5514"

# Orden de arranque: primero la app principal, luego los auxiliares.
APP_CORE="agente-monitor.service"
APP_AUX=(
    "agente-log-monitor.service"
    "agente-monitor-scheduler.service"
    "agente-monitor-bot.service"
)

# Orden de parada: consumidores primero, app principal después.
APP_STOP=(
    "agente-monitor-bot.service"
    "agente-monitor-scheduler.service"
    "agente-log-monitor.service"
    "agente-monitor.service"
)

LLM_UNIT="agente-monitor-wisp-agent.service"
LLM_CONFIG="${PROJECT_DIR}/config/llm_config.json"

CONTAINERS=(
    "influxdb-ia"
    "redis-ia"
    "redis-syslog"
    "mikrotik-syslog"
)

WITH_LLM=0
CMD=""

log() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "ERROR: no se encontró systemctl" >&2
        exit 1
    fi
}

unit_active() {
    systemctl --user is-active "$1" >/dev/null 2>&1
}

container_running() {
    [ "$(podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
}

redis_ping() {
    podman exec "$1" redis-cli ping 2>/dev/null | grep -q "PONG"
}

http_code() {
    python3 - "$1" <<'PY' 2>/dev/null || echo "000"
import sys, urllib.request, urllib.error
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
PY
}

llm_enabled() {
    [ -f "${LLM_CONFIG}" ] || return 1
    grep -Eq '"enabled"[[:space:]]*:[[:space:]]*true' "${LLM_CONFIG}"
}

usage() {
    cat <<'EOF'
mantenimiento.sh — Arranque/parada ordenada de todo el stack agente-monitor.

Uso:
  ./scripts/mantenimiento.sh --start   [--with-llm]  Iniciar todo y esperar.
  ./scripts/mantenimiento.sh --stop                 Detener todo.
  ./scripts/mantenimiento.sh --restart [--with-llm] Parar y volver a iniciar.
  ./scripts/mantenimiento.sh --status               Estado de todo el stack.
  ./scripts/mantenimiento.sh --help                 Mostrar esta ayuda.

Alias heredados: --ready = --start, --now = --stop.

El stack tiene dos capas:
  1) Infraestructura: agente-monitor-compose.service (podman-compose)
     -> influxdb-ia, redis-ia, redis-syslog, mikrotik-syslog
     + relay syslog del host (agente-monitor-syslog-relay.service): recibe el
       5514 de los routers y reenvía al contenedor conservando la IP real.
  2) Aplicación: agente-monitor (collector+API+ai-agent), log-monitor,
     scheduler y bot. El agente LLM (wisp-agent) es opcional (--with-llm).

Todo se maneja con systemctl a nivel de usuario: NO requiere root.
EOF
}

do_status() {
    log "Estado del stack agente-monitor"
    echo "--- Infraestructura (podman-compose) ---"
    if unit_active "${COMPOSE_UNIT}"; then
        echo "  ${COMPOSE_UNIT} -> ACTIVO"
    else
        echo "  ${COMPOSE_UNIT} -> INACTIVO"
    fi

    local c
    for c in "${CONTAINERS[@]}"; do
        if container_running "${c}"; then
            echo "  contenedor ${c} -> UP"
        else
            echo "  contenedor ${c} -> DOWN"
        fi
    done

    local influx_ok="no"
    [ "$(http_code 'http://127.0.0.1:8086/health')" = "200" ] && influx_ok="si"
    echo "  InfluxDB (8086)        -> healthy: ${influx_ok}"

    local redis_ok="no"
    redis_ping "redis-ia" && redis_ok="si"
    echo "  Redis-ia (6379)        -> PING: ${redis_ok}"

    local rusyslog_ok="no"
    redis_ping "redis-syslog" && rusyslog_ok="si"
    echo "  Redis-syslog (6380)    -> PING: ${rusyslog_ok}"

    if unit_active "${RELAY_UNIT}"; then
        echo "  relay syslog (${RELAY_PORT})   -> ACTIVO (IP de origen preservada)"
    else
        echo "  relay syslog (${RELAY_PORT})   -> INACTIVO (los eventos llegan como IP del contenedor)"
    fi

    echo "--- Aplicación ---"
    local u
    for u in "${APP_CORE}" "${APP_AUX[@]}" "${LLM_UNIT}"; do
        local extra=""
        if [ "${u}" = "${LLM_UNIT}" ]; then
            if llm_enabled; then extra=" (config LLM: habilitado)"; else extra=" (config LLM: deshabilitado)"; fi
        fi
        if unit_active "${u}"; then
            echo "  ${u} -> ACTIVO${extra}"
        else
            echo "  ${u} -> INACTIVO${extra}"
        fi
    done

    local api_code
    api_code="$(http_code 'http://127.0.0.1:8000/health')"
    if [ "${api_code}" = "200" ]; then
        echo "  API (8000)             -> respondiendo: si (${api_code})"
    else
        echo "  API (8000)             -> respondiendo: no (${api_code})"
    fi
}

wait_infra() {
    log "Esperando a que la infraestructura esté lista..."
    local attempts=0
    until [ "$(http_code 'http://127.0.0.1:8086/health')" = "200" ] \
        && redis_ping "redis-ia" \
        && redis_ping "redis-syslog" \
        && container_running "mikrotik-syslog"; do
        attempts=$((attempts + 1))
        if [ "${attempts}" -ge 60 ]; then
            echo "ERROR: la infraestructura no quedó lista tras 120s" >&2
            exit 1
        fi
        sleep 2
    done
    log "Infraestructura lista (Influx, Redis-ia, Redis-syslog, syslog)."
}

wait_api() {
    log "Esperando a que la API responda..."
    local attempts=0
    until [ "$(http_code 'http://127.0.0.1:8000/health')" = "200" ]; do
        attempts=$((attempts + 1))
        if [ "${attempts}" -ge 90 ]; then
            echo "ERROR: la API no respondió tras 180s" >&2
            exit 1
        fi
        sleep 2
    done
    log "API lista."
}

do_start() {
    log "Iniciando relay syslog del host (${RELAY_UNIT})..."
    systemctl --user start "${RELAY_UNIT}" 2>/dev/null \
        || echo "  aviso: no se pudo iniciar ${RELAY_UNIT} (manage-services.sh install syslog-relay)"

    log "Iniciando infraestructura (podman-compose)..."
    systemctl --user start "${COMPOSE_UNIT}" \
        || { echo "ERROR: no se pudo iniciar ${COMPOSE_UNIT}" >&2; exit 1; }
    wait_infra

    log "Iniciando aplicación (collector+API+ai-agent)..."
    systemctl --user start "${APP_CORE}" \
        || { echo "ERROR: no se pudo iniciar ${APP_CORE}" >&2; exit 1; }
    wait_api

    local u
    for u in "${APP_AUX[@]}"; do
        systemctl --user start "${u}" 2>/dev/null \
            || echo "  aviso: no se pudo iniciar ${u} (revisa manage-services.sh install all)"
    done
    log "Auxiliares iniciados (log-monitor, scheduler, bot)."

    if [ "${WITH_LLM}" = "1" ]; then
        if llm_enabled; then
            systemctl --user start "${LLM_UNIT}" 2>/dev/null \
                || echo "  aviso: no se pudo iniciar ${LLM_UNIT}"
            log "Agente LLM iniciado (${LLM_UNIT})."
        else
            systemctl --user start "${LLM_UNIT}" 2>/dev/null \
                || echo "  aviso: no se pudo iniciar ${LLM_UNIT}"
            log "AVISO: ${LLM_UNIT} iniciado, pero config/llm_config.json tiene enabled=false."
        fi
    else
        log "Agente LLM no iniciado (usa --with-llm para incluirlo)."
    fi

    log "Todo iniciado."
    do_status
}

do_stop() {
    log "Deteniendo agente LLM (si estaba activo)..."
    systemctl --user stop "${LLM_UNIT}" 2>/dev/null || true

    log "Deteniendo aplicación y consumidores..."
    local u
    for u in "${APP_STOP[@]}"; do
        systemctl --user stop "${u}" 2>/dev/null \
            || echo "  aviso: fallo al detener ${u}"
    done

    log "Deteniendo infraestructura (podman-compose)..."
    systemctl --user stop "${COMPOSE_UNIT}" 2>/dev/null \
        || echo "  aviso: ${COMPOSE_UNIT} no estaba activo"

    log "Deteniendo relay syslog del host..."
    systemctl --user stop "${RELAY_UNIT}" 2>/dev/null || true

    log "Detenido. Revisa el estado con ./scripts/mantenimiento.sh --status"
}

main() {
    check_systemctl

    local arg
    for arg in "$@"; do
        case "${arg}" in
            --start|--ready) CMD="start" ;;
            --stop|--now)    CMD="stop" ;;
            --restart)       CMD="restart" ;;
            --status)        CMD="status" ;;
            --with-llm)      WITH_LLM=1 ;;
            --help|-h|help)  usage; exit 0 ;;
            *)
                echo "Argumento no reconocido: ${arg}" >&2
                echo "Prueba './scripts/mantenimiento.sh --help'." >&2
                exit 1
                ;;
        esac
    done

    case "${CMD}" in
        start)   do_start ;;
        stop)    do_stop ;;
        restart) do_stop; do_start ;;
        status)  do_status ;;
        *)
            echo "Uso: ./scripts/mantenimiento.sh {--start|--stop|--restart|--status} [--with-llm]" >&2
            echo "Prueba './scripts/mantenimiento.sh --help'." >&2
            exit 1
            ;;
    esac
}

main "$@"
