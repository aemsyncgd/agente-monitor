#!/usr/bin/env bash
# =============================================================================
# instalar-debian13.sh - Agente Monitor (instalador todo-en-uno para Debian 13)
# =============================================================================
# Prepara el ambiente completo: paquetes apt, Podman rootless, venv Python,
# InfluxDB + Redis (contenedores), configs de ejemplo, units systemd --user
# y arranque de los servicios.
#
# Uso:
#   ./instalar-debian13.sh [DIR_PROYECTO]
#
#   DIR_PROYECTO  donde quedara el codigo (default: /opt/agente-monitor).
#
# Requisitos:
#   - Debian 13 (trixie) amd64, usuario con sudo (NO ejecutar con sudo).
#   - Internet para apt/pip/pull de imagenes.
#   - Redes de gestion alcanzables (OLTs/MikroTiks) si se configuran nodos.
#
# Idempotente: puede reejecutarse. No toca configs existentes (.env.ia,
# config/nodes.yaml, etc.) a menos que no existan.
# =============================================================================
set -euo pipefail

# --- Rutas ---------------------------------------------------------------
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-/opt/agente-monitor}"
RUN_AS_USER="${SUDO_USER:-${USER:-$(whoami 2>/dev/null || echo root)}}"
if [ "${RUN_AS_USER}" = "root" ]; then
    RUN_AS_USER="root"
fi

VENV_DIR="${PROJECT_DIR}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

# Servicios del modulo individual (coleccion que maneja manage-services.sh)
SERVICES=("collector" "api" "ai-agent" "llm-agent" "wisp-agent" "scheduler" "bot")

log()    { echo -e "\033[1;32m[OK]\033[0m   $*"; }
info()   { echo -e "\033[1;34m[...]\033[0m $*"; }
warn()   { echo -e "\033[1;33m[WARN]\033[0m $*"; }
die()    { echo -e "\033[1;31m[FAIL]\033[0m $*" >&2; exit 1; }

# systemctl --user para un usuario sin sesion grafica requiere estos exports
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

# =============================================================================
# 1) Paquetes del sistema
# =============================================================================
install_system_packages() {
    info "Actualizando apt e instalando paquetes del sistema..."
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
        podman podman-compose git curl python3 python3-venv python3-pip \
        snmp snmpd
    log "Paquetes apt instalados (podman, podman-compose, python3, snmp...)"
}

# =============================================================================
# 2) Arranque en boot: linger para systemd --user
# =============================================================================
enable_linger() {
    info "Habilitando linger (arranque de unidades --user al encender)..."
    sudo loginctl enable-linger "${RUN_AS_USER}" 2>/dev/null \
        && log "loginctl enable-linger ${RUN_AS_USER}" \
        || warn "No se pudo habilitar linger; revisar manualmente."
}

# =============================================================================
# 3) Codigo fuente
# =============================================================================
ensure_code() {
    if [ ! -f "${PROJECT_DIR}/scripts/start-all.sh" ]; then
        info "Descargando codigo en ${PROJECT_DIR}..."
        sudo mkdir -p "$(dirname "${PROJECT_DIR}")"
        GIT_REPO="${GIT_REPO:-https://github.com/aemsyncgd/agente-monitor.git}"
        if sudo git clone "${GIT_REPO}" "${PROJECT_DIR}" 2>/dev/null; then
            sudo chown -R "${RUN_AS_USER}:${RUN_AS_USER}" "${PROJECT_DIR}"
        else
            warn "No pude clonar ${GIT_REPO}. Descarga el codigo manualmente"
            warn "(git clone) o ajusta GIT_REPO y vuelve a correr el instalador."
        fi
    fi
    [ -d "${PROJECT_DIR}/src" ] || die "Falta src/ en ${PROJECT_DIR} (clon incompleto)."
    log "Proyecto listo en ${PROJECT_DIR}"
}

# =============================================================================
# 4) Scripts de instalacion de la aplicacion
# =============================================================================
copy_script_helpers() {
    # scripts/manage-services.sh y start-all.sh deben existir en el repo.
    [ -x "${PROJECT_DIR}/scripts/manage-services.sh" ] \
        || die "Falta scripts/manage-services.sh (instalar con permisos x)."
    log "scripts/manage-services.sh disponible"
}

# =============================================================================
# 5) venv + dependencias Python
# =============================================================================
create_venv() {
    if [ -x "${VENV_PYTHON}" ]; then
        info "venv ya existe en ${VENV_DIR}; se reutiliza."
    else
        info "Creando venv en ${VENV_DIR} (Python 3.13 de Debian 13)..."
        python3 -m venv "${VENV_DIR}"
        [ -x "${VENV_PYTHON}" ] || die "Fallo al crear el venv."
    fi

    info "Instalando requirements.txt + requirements-extra.txt..."
    "${VENV_PYTHON}" -m pip install --upgrade pip
    "${VENV_PYTHON}" -m pip install -r "${PROJECT_DIR}/requirements.txt"
    if [ -f "${INSTALL_DIR}/requirements-extra.txt" ]; then
        "${VENV_PYTHON}" -m pip install -r "${INSTALL_DIR}/requirements-extra.txt"
    else
        info "requirements-extra.txt no encontrado en ${INSTALL_DIR}"
    fi
    log "Paquetes Python base instalados"

    # Torch CPU (autoencoder optico). ~200 MB. Opcional pero recomendado.
    if ! "${VENV_PYTHON}" -c "import torch" 2>/dev/null; then
        info "Instalando torch (CPU) para el autoencoder optico..."
        "${VENV_PYTHON}" -m pip install torch --index-url https://download.pytorch.org/whl/cpu \
            && log "torch CPU instalado" \
            || warn "torch no se instalo; el detector optico quedara deshabilitado."
    else
        info "torch ya presente en el venv."
    fi
}

# =============================================================================
# 6) Directorios de runtime y configs de ejemplo
# =============================================================================
ensure_dirs_and_configs() {
    info "Creando directorios de runtime..."
    mkdir -p "${PROJECT_DIR}"/logs "${PROJECT_DIR}"/data "${PROJECT_DIR}"/models \
             "${PROJECT_DIR}"/influxdb/data "${PROJECT_DIR}"/influxdb/config \
             "${PROJECT_DIR}"/redis/data
    log "Directorios logs/ data/ models/ influxdb/ redis/ listos"

    info "Copiando configs de ejemplo si no existen..."
    # .env de la aplicacion
    if [ ! -f "${PROJECT_DIR}/.env.ia" ]; then
        cp "${INSTALL_DIR}/.env.ia.example" "${PROJECT_DIR}/.env.ia"
        warn "Creado .env.ia desde ejemplo: EDITALO con tus secretos y rerun o arranca."
    fi
    # .env para podman-compose (token admin Influx)
    if [ ! -f "${PROJECT_DIR}/.env" ]; then
        cp "${INSTALL_DIR}/.env.example" "${PROJECT_DIR}/.env"
        warn "Creado .env desde ejemplo: completa INFLUXDB_ADMIN_TOKEN."
    fi
    # compose.yml
    if [ ! -f "${PROJECT_DIR}/compose.yml" ]; then
        cp "${INSTALL_DIR}/compose.yml.example" "${PROJECT_DIR}/compose.yml"
        log "compose.yml creado"
    fi
    # configs yaml/json
    for f in config_ia config nodes ping_targets llm_config instructions telegram-report-format; do
        case "${f}" in
            config_ia)            src="config_ia.yaml.example";            dst="config/config_ia.yaml" ;;
            config)               src="config.yaml.example";               dst="config/config.yaml" ;;
            nodes)                src="nodes.yaml.example";                dst="config/nodes.yaml" ;;
            ping_targets)         src="ping_targets.yaml.example";         dst="config/ping_targets.yaml" ;;
            llm_config)           src="llm_config.json.example";           dst="config/llm_config.json" ;;
            instructions)         src="instructions.json.example";         dst="config/instructions.json" ;;
            telegram-report-format) src="telegram-report-format.conf.example"; dst="config/telegram-report-format.conf" ;;
        esac
        if [ ! -f "${PROJECT_DIR}/${dst}" ]; then
            cp "${INSTALL_DIR}/config/${src}" "${PROJECT_DIR}/${dst}"
            log "Creado ${dst} desde ejemplo"
        fi
    done
}

# =============================================================================
# 7) Contenedores Podman: InfluxDB 2.7 + Redis 7
# =============================================================================
start_containers() {
    info "Levantando InfluxDB + Redis con podman-compose..."
    cd "${PROJECT_DIR}"
    podman-compose up -d 2>/dev/null || podman compose up -d 2>/dev/null \
        || die "podman-compose fallo. Verificar .env (INFLUXDB_ADMIN_TOKEN)."
    log "Contenedores definidos por compose.yml iniciados"
}

wait_for_influxdb() {
    info "Esperando a que InfluxDB responda..."
    local ok=0
    for _ in $(seq 1 60); do
        if curl -fsS -m 3 "http://localhost:8086/health" 2>/dev/null | grep -q '"status":"pass"'; then
            ok=1; break
        fi
        sleep 2
    done
    [ "${ok}" -eq 1 ] || die "InfluxDB no respondio en /health tras 120s."
    log "InfluxDB healthy (http://localhost:8086/health)"
}

wait_for_redis() {
    info "Esperando a que Redis responda..."
    local ok=0
    for _ in $(seq 1 30); do
        if podman exec redis-ia redis-cli ping 2>/dev/null | grep -q PONG; then
            ok=1; break
        fi
        sleep 2
    done
    [ "${ok}" -eq 1 ] && log "Redis responde PONG" || warn "Redis no respondio (opcional; el codigo cae a RAM)."
}

setup_influx() {
    # Onboarding inicial idempotente. Usa INFLUXDB_ADMIN_TOKEN (de .env del proyecto)
    # como token all-access; el mismo valor puede usarse como INFLUXDB_TOKEN en .env.ia.
    local adm_token org bucket
    # Fuente de variables: .env del proyecto (podman-compose) y .env.ia
    if [ -f "${PROJECT_DIR}/.env" ]; then
        set -a; # shellcheck disable=SC1091
        source "${PROJECT_DIR}/.env"; set +a
    fi
    if [ -f "${PROJECT_DIR}/.env.ia" ]; then
        set -a; # shellcheck disable=SC1091
        source "${PROJECT_DIR}/.env.ia"; set +a
    fi
    adm_token="${INFLUXDB_ADMIN_TOKEN:-}"
    org="${INFLUXDB_ORG:-vidanet}"
    bucket="${INFLUXDB_BUCKET:-monitoreo}"
    local pw="${INFLUXDB_PASSWORD:-cambia-me-admin}"
    local user="${INFLUXDB_ADMIN_USER:-admin}"

    if [ -z "${adm_token}" ]; then
        warn "INFLUXDB_ADMIN_TOKEN vacio: se omite onboarding automatico de InfluxDB."
        warn "Crea org/bucket/token manualmente y dejala en .env.ia (INFLUXDB_TOKEN)."
        return 0
    fi

    # Si ya hay setup (org existe con ese token), no reintentar onboarding.
    if podman exec influxdb-ia influx org list --token "${adm_token}" 2>/dev/null | grep -q "${org}"; then
        log "InfluxDB ya configurado (org=${org})"
    else
        info "Ejecutando onboarding inicial de InfluxDB (org=${org}, bucket=${bucket})..."
        podman exec influxdb-ia influx setup --force \
            --host http://127.0.0.1:8086 \
            --username "${user}" \
            --password "${pw}" \
            --org "${org}" \
            --bucket "${bucket}" \
            --token "${adm_token}" 2>/dev/null \
            && log "Onboarding completado; token all-access = INFLUXDB_ADMIN_TOKEN" \
            || warn "Onboarding fallo; configura InfluxDB manualmente (influx setup)."
    fi
}

# =============================================================================
# 8) Units systemd de usuario + arranque
# =============================================================================
install_and_start_units() {
    info "Instalando units systemd de usuario (~/.config/systemd/user/)..."
    "${PROJECT_DIR}/scripts/manage-services.sh" install all
    log "Units instalados (8): agente-monitor{,-collector,-api,-ai-agent,-llm-agent,-wisp-agent,-scheduler,-bot}.service"

    info "Habilitando y arrancando servicios systemctl --user..."
    for svc in "${SERVICES[@]}"; do
        "${PROJECT_DIR}/scripts/manage-services.sh" enable "${svc}" 2>/dev/null \
            && "${PROJECT_DIR}/scripts/manage-services.sh" start "${svc}" 2>/dev/null \
            && log "  ${svc}: habilitado + arrancado" \
            || warn "  ${svc}: no inicio (revisar logs/ o si requiere credenciales)."
    done
    systemctl --user daemon-reload 2>/dev/null || true
}

# =============================================================================
# 9) Resumen / verificacion
# =============================================================================
print_summary() {
    echo
    echo "================================================================"
    echo " Agente Monitor instalado. Rutas y control"
    echo "================================================================"
    echo "  Proyecto:      ${PROJECT_DIR}"
    echo "  venv Python:   ${VENV_PYTHON}"
    echo "  InfluxDB:      http://localhost:8086"
    echo "  Redis:         redis://localhost:6379/0"
    echo "  API:           http://localhost:8000"
    echo
    echo "  Units systemctl --user:"
    echo "    ~/.config/systemd/user/agente-monitor*.service"
    echo "    ~/.config/containers/systemd/*.container (si usas quadlet)"
    echo
    echo "  Iniciar/Detener/Reiniciar/Estado:"
    echo "    systemctl --user start   agente-monitor-collector"
    echo "    systemctl --user stop    agente-monitor-api"
    echo "    systemctl --user restart agente-monitor-scheduler"
    echo "    systemctl --user status  agente-monitor-bot"
    echo "    systemctl --user status  --no-pager"
    echo "  o via:  ${PROJECT_DIR}/scripts/manage-services.sh {status|start|stop|restart|enable|disable} <servicio|all>"
    echo
    echo "  Para arranque en boot ya quedo habilitado (enable-linger)."
    echo
    echo "  Verificacion rapida:"
    echo "    export XDG_RUNTIME_DIR=/run/user/\$(id -u)"
    echo "    export DBUS_SESSION_BUS_ADDRESS=unix:path=\${XDG_RUNTIME_DIR}/bus"
    echo "    curl -s http://localhost:8000/health"
    echo "    podman-compose ps"
    echo "    ${PROJECT_DIR}/venv/bin/python -m pytest ${PROJECT_DIR}/tests -q"
    echo "================================================================"
}

# =============================================================================
# main
# =============================================================================
main() {
    [ "$(id -u)" -ne 0 ] || die "NO ejecutar con sudo/root: corre como usuario normal."
    command -v sudo >/dev/null || die "sudo requerido."

    install_system_packages
    enable_linger
    ensure_code
    copy_script_helpers
    create_venv
    ensure_dirs_and_configs
    start_containers
    wait_for_influxdb
    wait_for_redis
    setup_influx
    install_and_start_units
    print_summary
}

main "$@"