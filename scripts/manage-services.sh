#!/usr/bin/env bash
# scripts/manage-services.sh
# Control de servicios/módulos del Agente Monitor.
# Usado por la API del dashboard para iniciar/detener/reiniciar/estado.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/server/agente-monitor}"
VENV_PYTHON="${VENV_PYTHON:-${PROJECT_DIR}/venv/bin/python3}"
SYSTEMD_PREFIX="agente-monitor-"

usage() {
    cat <<'EOF'
Uso: manage-services.sh <comando> [servicio|all]

Comandos:
  install <servicio|all>  Instalar unit systemd de usuario (~/.config/systemd/user)
  start   <servicio>   Iniciar un servicio (collector, api, ai-agent, llm-agent, wisp-agent, scheduler, bot, redis)
  stop    <servicio>   Detener un servicio
  restart <servicio>   Reiniciar un servicio
  status  [servicio]   Estado de servicios (por defecto: todos)
  enable  <servicio>   Habilitar inicio automático (systemctl --user)
  disable <servicio>   Deshabilitar inicio automático (systemctl --user)

Servicios: collector, api, ai-agent, llm-agent, wisp-agent, scheduler, bot

Nota: los contenedores (InfluxDB, Redis) corren con Podman, NO Docker.
EOF
}

ensure_env() {
    if [ -f "${PROJECT_DIR}/.env.ia" ]; then
        set -a
        source "${PROJECT_DIR}/.env.ia"
        set +a
    fi
    export CONFIG_PATH="${CONFIG_PATH:-config/config_ia.yaml}"
    export NODES_PATH="${NODES_PATH:-config/nodes.yaml}"
    export INSTRUCTIONS_PATH="${INSTRUCTIONS_PATH:-${PROJECT_DIR}/config/instructions.json}"
    export MODEL_PATH="${MODEL_PATH:-${PROJECT_DIR}/models/optical_autoencoder.pt}"
}

get_pid() {
    local name="$1"
    local pid_file="/tmp/agente-monitor-${name}.pid"
    if [ -f "${pid_file}" ]; then
        cat "${pid_file}" 2>/dev/null || echo ""
    fi
}

is_running() {
    local name="$1"
    local pid
    pid=$(get_pid "${name}")
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    # Fallback: unit systemd de usuario
    local unit="${SYSTEMD_PREFIX}${name}.service"
    if [ -f "${HOME}/.config/systemd/user/${unit}" ] \
        && systemctl --user is-active --quiet "${unit}" 2>/dev/null; then
        return 0
    fi
    return 1
}

service_command() {
    local name="$1"
    local command=""
    case "${name}" in
        collector)
            command="${VENV_PYTHON} -m src.collectors.main"
            ;;
        api)
            command="${VENV_PYTHON} -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000"
            ;;
        ai-agent)
            command="${VENV_PYTHON} -m src.ai.main"
            ;;
        llm-agent)
            command="${VENV_PYTHON} -m src.ai.llm_agent.main"
            ;;
        wisp-agent)
            command="${VENV_PYTHON} -m src.ai.agent_loop"
            ;;
        scheduler)
            command="${VENV_PYTHON} -m src.scheduler_cli"
            ;;
        bot)
            command="${VENV_PYTHON} -m src.telegram_bot"
            ;;
        *)
            echo "ERROR: Servicio desconocido: ${name}" >&2
            return 2
            ;;
    esac
    echo "${command}"
}

start_service() {
    local name="$1"
    if is_running "${name}"; then
        echo "SKIP: ${name} ya está corriendo"
        return 0
    fi
    ensure_env
    local command
    command=$(service_command "${name}")
    cd "${PROJECT_DIR}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando ${name}..." >&2
    ${command} >> "${PROJECT_DIR}/logs/${name}.log" 2>&1 &
    local pid=$!
    echo "${pid}" > "/tmp/agente-monitor-${name}.pid"
    echo "OK: ${name} iniciado con PID ${pid}"
}

stop_service() {
    local name="$1"
    if ! is_running "${name}"; then
        echo "SKIP: ${name} no está corriendo"
        return 0
    fi
    local pid
    pid=$(get_pid "${name}")
    kill -TERM "${pid}" 2>/dev/null || true
    # Esperar hasta 10 segundos
    for _ in $(seq 1 10); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill -9 "${pid}" 2>/dev/null || true
    fi
    rm -f "/tmp/agente-monitor-${name}.pid"
    echo "OK: ${name} detenido"
}

status_service() {
    local name="$1"
    local unit="${SYSTEMD_PREFIX}${name}.service"
    if is_running "${name}"; then
        local pid
        pid=$(get_pid "${name}")
        if [ -n "${pid}" ]; then
            echo "RUNNING ${name} pid=${pid}"
        elif [ -f "${HOME}/.config/systemd/user/${unit}" ]; then
            echo "RUNNING ${name} unit=${unit} (systemctl --user)"
        else
            echo "RUNNING ${name}"
        fi
    else
        echo "STOPPED ${name}"
    fi
}

status_all() {
    local services=(collector api ai-agent llm-agent wisp-agent scheduler bot)
    local any_running=0
    for s in "${services[@]}"; do
        status_service "${s}"
        if is_running "${s}"; then
            any_running=1
        fi
    done
    exit 0
}

enable_service() {
    local name="$1"
    local unit="${SYSTEMD_PREFIX}${name}.service"
    # Los units viven a nivel usuario: ~/.config/systemd/user/
    local user_units="${HOME}/.config/systemd/user"
    if [ ! -f "${user_units}/${unit}" ]; then
        echo "ERROR: ${unit} no está instalado. Ejecuta: 'manage-services.sh install ${name}'" >&2
        return 1
    fi
    if systemctl --user is-enabled "${unit}" 2>/dev/null >/dev/null; then
        echo "SKIP: ${unit} ya está habilitado"
        return 0
    fi
    systemctl --user enable "${unit}" 2>/dev/null || {
        echo "ERROR: No se pudo habilitar ${unit}" >&2
        return 1
    }
    echo "OK: ${unit} habilitado (systemctl --user)"
}

disable_service() {
    local name="$1"
    local unit="${SYSTEMD_PREFIX}${name}.service"
    if ! systemctl --user is-enabled "${unit}" 2>/dev/null >/dev/null; then
        echo "SKIP: ${unit} no está habilitado"
        return 0
    fi
    systemctl --user disable "${unit}" 2>/dev/null || {
        echo "ERROR: No se pudo deshabilitar ${unit}" >&2
        return 1
    }
    echo "OK: ${unit} deshabilitado (systemctl --user)"
}

install_units() {
    local service="$1"
    local src_dir="${PROJECT_DIR}/systemd"
    local user_units="${HOME}/.config/systemd/user"
    mkdir -p "${user_units}"

    install_one() {
        local name="$1"
        local unit="${SYSTEMD_PREFIX}${name}.service"
        if [ ! -f "${src_dir}/${unit}" ]; then
            echo "ERROR: no existe ${src_dir}/${unit}" >&2
            return 1
        fi
        cp "${src_dir}/${unit}" "${user_units}/${unit}"
        echo "OK: ${unit} instalado en ${user_units}/"
    }

    if [ -n "${service}" ] && [ "${service}" != "all" ]; then
        install_one "${service}"
    else
        install_one collector || true
        install_one api || true
        install_one ai-agent || true
        install_one llm-agent || true
        install_one wisp-agent || true
        install_one scheduler || true
        install_one bot || true
    fi

    systemctl --user daemon-reload
    echo "OK: daemon-reload (systemctl --user)"
}

CMD="${1:-}"
SERVICE="${2:-}"

if [ -z "${CMD}" ]; then
    usage
    exit 1
fi

ensure_env

case "${CMD}" in
    install)
        install_units "${SERVICE}"
        ;;
    start)
        [ -z "${SERVICE}" ] && { echo "ERROR: servicio requerido"; exit 1; }
        start_service "${SERVICE}"
        ;;
    stop)
        [ -z "${SERVICE}" ] && { echo "ERROR: servicio requerido"; exit 1; }
        stop_service "${SERVICE}"
        ;;
    restart)
        [ -z "${SERVICE}" ] && { echo "ERROR: servicio requerido"; exit 1; }
        stop_service "${SERVICE}"
        sleep 1
        start_service "${SERVICE}"
        ;;
    status)
        if [ -n "${SERVICE}" ]; then
            status_service "${SERVICE}"
        else
            status_all
        fi
        ;;
    enable)
        [ -z "${SERVICE}" ] && { echo "ERROR: servicio requerido"; exit 1; }
        enable_service "${SERVICE}"
        ;;
    disable)
        [ -z "${SERVICE}" ] && { echo "ERROR: servicio requerido"; exit 1; }
        disable_service "${SERVICE}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
