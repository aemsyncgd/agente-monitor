#!/usr/bin/env bash
# scripts/syslog_source.sh
# Conmuta la fuente de eventos syslog de este LXC.
#
#   replica  -> modo normal: agente-monitor-1 reenvía sus eventos por API y el
#               replicador los copia a redis-syslog:6380.
#   local    -> modo mantenimiento: los equipos apuntan directo a este LXC
#               (UDP/TCP 5514). Se detiene el replicador para no mezclar
#               eventos antiguos del agente 1 con los recibidos acá.
#   status   -> muestra el modo activo y el estado de los servicios.
#   clear    -> borra los eventos ya almacenados (syslog:recent + dedup).
#
# Uso típico:
#   scripts/syslog_source.sh local     # antes de cambiar la IP en los equipos
#   scripts/syslog_source.sh replica   # al devolver los equipos al agente 1
#
# No modifica la configuración de los equipos ni del otro agente.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPLICA_UNIT="agente-monitor-syslog-replicator.service"
RELAY_UNIT="agente-monitor-syslog-relay.service"
LOGMON_UNIT="agente-log-monitor.service"
COMPOSE_UNIT="agente-monitor-compose.service"

setup_runtime_env() {
    if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    fi
}

current_ip() {
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1
}

unit_state() {
    systemctl --user is-active "$1" 2>/dev/null || true
}

show_status() {
    local mode
    if [ "$(systemctl --user is-enabled --quiet "${REPLICA_UNIT}" 2>/dev/null && echo yes || echo no)" = "yes" ]; then
        mode="replica (agente-monitor-1)"
    else
        mode="local (recepción directa)"
    fi

    echo "Fuente syslog actual: ${mode}"
    echo "  IP de este LXC:      $(current_ip)"
    echo "  ${REPLICA_UNIT}: $(unit_state "${REPLICA_UNIT}")"
    echo "  ${RELAY_UNIT}:        $(unit_state "${RELAY_UNIT}")"
    echo "  ${LOGMON_UNIT}:          $(unit_state "${LOGMON_UNIT}")"
    echo "  ${COMPOSE_UNIT}:       $(unit_state "${COMPOSE_UNIT}")"
    echo "  eventos en Redis:    $(podman exec redis-syslog redis-cli -p 6380 zcard syslog:recent 2>/dev/null || echo 'n/d')"
}

mode_local() {
    echo "== Modo LOCAL: este LXC recibe syslog de los equipos =="
    systemctl --user stop "${REPLICA_UNIT}" 2>/dev/null || true
    systemctl --user disable "${REPLICA_UNIT}" 2>/dev/null || true
    systemctl --user start "${COMPOSE_UNIT}"
    systemctl --user start "${RELAY_UNIT}"
    systemctl --user start "${LOGMON_UNIT}"
    echo
    echo "Replicador detenido y deshabilitado."
    echo "Apuntar los equipos a: $(current_ip):5514  (UDP; el relay también acepta TCP)"
    echo
    echo "Verificación:"
    echo "  ss -lunp | grep 5514"
    echo "  podman exec redis-syslog redis-cli -p 6380 zrange syslog:recent -1 -1"
    echo "  curl -s -H \"X-API-Key: \$API_KEY\" 'http://127.0.0.1:8000/api/v1/syslog/recent?minutes=10&limit=5'"
    show_status
}

mode_replica() {
    echo "== Modo REPLICA: eventos desde agente-monitor-1 =="
    systemctl --user enable "${REPLICA_UNIT}"
    systemctl --user start "${REPLICA_UNIT}"
    echo
    echo "Replicador habilitado. Devuelve los equipos a agente-monitor-1 cuando termines."
    show_status
}

clear_events() {
    echo "Borrando eventos syslog almacenados..."
    systemctl --user stop "${REPLICA_UNIT}" 2>/dev/null || true
    podman exec redis-syslog redis-cli -p 6380 del syslog:recent syslog:replicator:seen
    echo "Listo. Las listas por dispositivo (syslog:device:<ip>:recent) se conservan."
}

usage() {
    cat <<EOF
Uso: $(basename "$0") {local|replica|status|clear}

  local    Mantenimiento: parar el replicador y recibir syslog directo en 5514
  replica  Modo normal: reactivar la replicación desde agente-monitor-1
  status   Mostrar modo activo, servicios y cantidad de eventos
  clear    Borrar syslog:recent y el dedup (no borra listas por dispositivo)
EOF
}

setup_runtime_env

case "${1:-status}" in
    local)   mode_local ;;
    replica) mode_replica ;;
    status)  show_status ;;
    clear)   clear_events ;;
    *)       usage; exit 2 ;;
esac
