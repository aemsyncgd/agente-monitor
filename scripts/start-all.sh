#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/server/agente-monitor"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python3"

cd "${PROJECT_DIR}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

log "=== Iniciando Agente Monitor (servicio unificado) ==="

export CONFIG_PATH=config/config_ia.yaml
export NODES_PATH=config/nodes.yaml
export CSV_PATH=/home/server/agente-monitor/grid_servicios.csv
export INSTRUCTIONS_PATH=/home/server/agente-monitor/config/instructions.json
export MODEL_PATH=/home/server/agente-monitor/models/optical_autoencoder.pt

# Load secrets from .env.ia (NEVER hardcode tokens, passwords, or keys here)
if [ -f "${PROJECT_DIR}/.env.ia" ]; then
    set -a
    source "${PROJECT_DIR}/.env.ia"
    set +a
else
    log "ERROR: .env.ia not found. Copy .env.ia.example to .env.ia and fill in secrets."
    exit 1
fi

wait_for_influxdb() {
    log "Esperando a que InfluxDB esté healthy (unit influxdb-ia.service)..."
    local max_attempts=60
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        if curl -s -m 3 "http://localhost:8086/health" 2>/dev/null | grep -q '"status":"pass"'; then
            log "InfluxDB está healthy y respondiendo"
            return 0
        fi
        attempt=$((attempt + 1))
        log "Intento ${attempt}/${max_attempts} - esperando InfluxDB..."
        sleep 2
    done
    
    log "ERROR: InfluxDB no respondió tras ${max_attempts} intentos. ¿Está activo el unit influxdb-ia.service?"
    return 1
}

start_component() {
    local name="$1"
    local cmd="$2"
    local pid_file="/tmp/agente-monitor-${name}.pid"
    
    log "Iniciando ${name}..."
    cd "${PROJECT_DIR}"
    eval "${cmd}" &
    local pid=$!
    echo $pid > "${pid_file}"
    log "${name} iniciado con PID ${pid}"
}

cleanup() {
    log "Recibida señal de terminación, deteniendo componentes..."
    
    for pid_file in /tmp/agente-monitor-*.pid; do
        if [ -f "$pid_file" ]; then
            local pid=$(cat "$pid_file" 2>/dev/null || echo "")
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                log "Deteniendo PID $pid..."
                kill -TERM "$pid" 2>/dev/null || true
            fi
            rm -f "$pid_file"
        fi
    done
    
    log "Componentes detenidos"
    exit 0
}

trap cleanup SIGTERM SIGINT

wait_for_influxdb

log "InfluxDB listo, iniciando componentes del agente..."

start_component "collector" "${VENV_PYTHON} -m src.collectors.main"
sleep 2
start_component "api" "${VENV_PYTHON} -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000"
sleep 2
start_component "ai-agent" "${VENV_PYTHON} -m src.ai.main"
sleep 2
start_component "llm-agent" "${VENV_PYTHON} -m src.ai.llm_agent.main"

log "Todos los componentes iniciados. Esperando señales..."

wait