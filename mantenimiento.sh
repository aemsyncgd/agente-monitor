#!/usr/bin/env bash
#
# mantenimiento.sh — Atajos prácticos para el agente-monitor.
#
# Uso:
#   ./mantenimiento.sh --now     Detener TODO el agente (incluido InfluxDB).
#   ./mantenimiento.sh --ready   Iniciar TODO el agente (Incluido InfluxDB) y
#                                esperar a que quede operativo.
#   ./mantenimiento.sh --status  Mostrar el estado de todos los servicios.
#   ./mantenimiento.sh --help    Mostrar esta ayuda.
#
# Todo se maneja con systemctl a nivel de usuario (systemctl --user),
# por lo que NO se requieren permisos de administrador.
#
set -uo pipefail

SERVICE_AGENTE="agente-monitor.service"
SERVICE_INFLUX="influxdb-ia.service"

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
  ./mantenimiento.sh --now     Detener TODO el agente (incluido InfluxDB).
  ./mantenimiento.sh --ready   Iniciar TODO el agente (incluido InfluxDB) y
                               esperar a que quede operativo.
  ./mantenimiento.sh --status  Mostrar el estado de todos los servicios.
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
    echo "--------------------------------------------------"

    local influx_ok=no
    if curl -s -m 3 "http://localhost:8086/health" 2>/dev/null | grep -q '"status":"pass"'; then
        influx_ok=si
    fi
    echo "  InfluxDB (8086)      -> healthy: ${influx_ok}"
    local api_ok=no
    if curl -s -m 3 -o /dev/null -w "%{http_code}" "http://localhost:8000/api/reporte/status" 2>/dev/null | grep -q "200"; then
        api_ok=si
    fi
    echo "  API (8000)           -> respondiendo: ${api_ok}"
}

do_stop() {
    log "Deteniendo el agente-monitor..."
    systemctl --user stop "${SERVICE_AGENTE}" || echo "  aviso: fallo al detener ${SERVICE_AGENTE}"
    log "Deteniendo InfluxDB..."
    systemctl --user stop "${SERVICE_INFLUX}" || echo "  aviso: fallo al detener ${SERVICE_INFLUX}"
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
    log "API lista. Todo iniciado."
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
