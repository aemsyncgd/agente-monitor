#!/usr/bin/env bash
#
# mantenimiento.sh — Atajos prácticos para el agente-monitor.
#
# Uso:
#   ./mantenimiento.sh --now     Detener TODO el agente (incluidos InfluxDB y Redis).
#   ./mantenimiento.sh --ready   Iniciar TODO el agente (incluidos InfluxDB y Redis)
#                                y esperar a que quede operativo.
#   ./mantenimiento.sh --status  Mostrar el estado de todos los servicios
#                               (agente unificado, extras, InfluxDB y Redis).
#   ./mantenimiento.sh --help    Mostrar esta ayuda.
#
# Todo se maneja con systemctl a nivel de usuario (systemctl --user),
# por lo que NO se requieren permisos de administrador.
#
set -uo pipefail

SERVICE_AGENTE="agente-monitor.service"          # unidad unificada (collector+api+ai+llm)
SERVICE_INFLUX="influxdb-ia.service"
SERVICE_REDIS="redis-ia.service"

# Servicios que corren por separado de la unidad unificada.
EXTRA_SERVICES=(
    "agente-monitor-wisp-agent.service"
    "agente-monitor-scheduler.service"
    "agente-monitor-bot.service"
)

log() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "ERROR: no se encontró systemctl" >&2
        exit 1
    fi
}

usage() {
    cat <<'EOF'
mantenimiento.sh — Atajos prácticos para el agente-monitor.

Uso:
  ./mantenimiento.sh --now     Detener TODO el agente (incluidos InfluxDB y Redis).
  ./mantenimiento.sh --ready   Iniciar TODO el agente (incluidos InfluxDB y Redis)
                               y esperar a que quede operativo.
  ./mantenimiento.sh --status  Mostrar el estado de todos los servicios
                               (agente unificado, extras, InfluxDB y Redis).
  ./mantenimiento.sh --help    Mostrar esta ayuda.

Todo se maneja con systemctl a nivel de usuario (systemctl --user),
por lo que NO se requieren permisos de administrador.
EOF
    exit 0
}

do_status() {
    log "Estado de los servicios:"
    echo "--------------------------------------------------"
    systemctl --user is-active "${SERVICE_INFLUX}" >/dev/null 2>&1 \
        && echo "  ${SERVICE_INFLUX}  -> ACTIVO" \
        || echo "  ${SERVICE_INFLUX}  -> INACTIVO"
    systemctl --user is-active "${SERVICE_AGENTE}" >/dev/null 2>&1 \
        && echo "  ${SERVICE_AGENTE}  -> ACTIVO" \
        || echo "  ${SERVICE_AGENTE}  -> INACTIVO"
    local svc
    for svc in "${EXTRA_SERVICES[@]}"; do
        systemctl --user is-active "${svc}" >/dev/null 2>&1 \
            && echo "  ${svc}  -> ACTIVO" \
            || echo "  ${svc}  -> INACTIVO"
    done
    systemctl --user is-active "${SERVICE_REDIS}" >/dev/null 2>&1 \
        && echo "  ${SERVICE_REDIS}  -> ACTIVO" \
        || echo "  ${SERVICE_REDIS}  -> INACTIVO"
    echo "--------------------------------------------------"

    local influx_ok=no
    if curl -s -m 3 "http://localhost:8086/health" 2>/dev/null | grep -q '"status":"pass"'; then
        influx_ok=si
    fi
    echo "  InfluxDB (8086)      -> healthy: ${influx_ok}"
    local redis_ok=no
    if redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q "PONG"; then
        redis_ok=si
    elif podman exec redis-ia redis-cli ping 2>/dev/null | grep -q "PONG"; then
        redis_ok=si
    fi
    echo "  Redis (6379)         -> PING: ${redis_ok}"
    local api_ok=no
    if curl -s -m 3 -o /dev/null -w "%{http_code}" "http://localhost:8000/api/reporte/status" 2>/dev/null | grep -q "200"; then
        api_ok=si
    fi
    echo "  API (8000)           -> respondiendo: ${api_ok}"
}

do_stop() {
    log "Deteniendo el agente-monitor..."
    systemctl --user stop "${SERVICE_AGENTE}" || echo "  aviso: fallo al detener ${SERVICE_AGENTE}"
    local svc
    for svc in "${EXTRA_SERVICES[@]}"; do
        systemctl --user stop "${svc}" 2>/dev/null || true
    done
    log "Deteniendo InfluxDB..."
    systemctl --user stop "${SERVICE_INFLUX}" || echo "  aviso: fallo al detener ${SERVICE_INFLUX}"
    log "Deteniendo Redis..."
    systemctl --user stop "${SERVICE_REDIS}" 2>/dev/null || echo "  aviso: ${SERVICE_REDIS} no está gestionado por systemd"
    log "Detenido. Revisa el estado con ./mantenimiento.sh --status"
}

do_start() {
    log "Iniciando InfluxDB..."
    systemctl --user start "${SERVICE_INFLUX}" || { echo "ERROR: no se pudo iniciar ${SERVICE_INFLUX}" >&2; exit 1; }

    log "Esperando a que InfluxDB esté healthy..."
    local attempts=0
    until curl -s -m 3 "http://localhost:8086/health" 2>/dev/null | grep -q '"status":"pass"'; do
        attempts=$((attempts + 1))
        if [ "${attempts}" -ge 60 ]; then
            echo "ERROR: InfluxDB no respondió tras 60 intentos" >&2
            exit 1
        fi
        sleep 2
    done
    log "InfluxDB healthy."

    log "Iniciando Redis (ClientStore)..."
    systemctl --user start "${SERVICE_REDIS}" 2>/dev/null \
        || podman start redis-ia 2>/dev/null \
        || echo "  aviso: no se pudo iniciar Redis (el buscador caerá a InfluxDB)"

    log "Iniciando agente-monitor..."
    systemctl --user start "${SERVICE_AGENTE}" || { echo "ERROR: no se pudo iniciar ${SERVICE_AGENTE}" >&2; exit 1; }

    log "Esperando a que la API responda..."
    local attempts=0
    until curl -s -m 3 -o /dev/null -w "%{http_code}" "http://localhost:8000/api/reporte/status" 2>/dev/null | grep -q "200"; do
        attempts=$((attempts + 1))
        if [ "${attempts}" -ge 90 ]; then
            echo "ERROR: la API no respondió tras 90 intentos" >&2
            exit 1
        fi
        sleep 2
    done
    log "API lista."

    local svc
    for svc in "${EXTRA_SERVICES[@]}"; do
        systemctl --user start "${svc}" 2>/dev/null \
            || echo "  aviso: no se pudo iniciar ${svc} (instala con manage-services.sh install all)"
    done

    log "Todo iniciado."
    do_status
}

main() {
    check_systemctl
    case "${1:-}" in
        --now)    do_stop ;;
        --ready)  do_start ;;
        --status) do_status ;;
        --help|-h|help) usage ;;
        *)
            echo "Uso: ./mantenimiento.sh {--now | --ready | --status | --help}" >&2
            echo "Prueba './mantenimiento.sh --help' para más detalle." >&2
            exit 1
            ;;
    esac
}

main "$@"
