# Instalación de Agente Monitor en Debian 13 (trixie)

Guía completa para desplegar **Agente Monitor** en una máquina con Debian 13
amd64 limpio: monitor de red WISP (OLTs GPON/VSOL, MikroTiks, ping), FastAPI con
ficha de clientes, bot de Telegram, agentes de IA y autoencoder óptico.

Todo lo que necesitas está en esta carpeta `install/`: el instalador
automatizado, la plantilla de cada `.env` y un ejemplo de cada archivo
esencial del sistema.

---

## 1. Contenido de esta carpeta (`install/`)

| Archivo | Propósito |
|---|---|
| `instalar-debian13.sh` | Instalador todo-en-uno (ver §6) |
| `instalacion-debian13.md` | Esta guía |
| `.env.ia.example` | Plantilla del `.env` de la **aplicación** (token Influx, Telegram, API keys, credenciales) |
| `.env.example` | Plantilla del `.env` de **podman-compose** (token admin de InfluxDB) |
| `compose.yml.example` | Servicios contenedores: InfluxDB 2.7 + Redis 7 |
| `requirements.txt` | Dependencias base (las mismas del repo) |
| `requirements-extra.txt` | Dependencias extras que el código usa (FastAPI, uvicorn, Redis, numpy, torch…) |
| `config/*.example` | Plantillas de todos los configs esenciales |
| `systemd/*.service` | Unidades `systemctl --user` (con placeholder `@PROJECT_DIR@`) |

---

## 2. Requisitos de la máquina

- **Sistema:** Debian 13 (trixie) amd64. Verificado en: systemd 257, Python del
  sistema 3.13.5, Podman 5.4.2, podman-compose 1.3.0.
- **Usuario:** una cuenta normal con `sudo` (el instalador lo exige y **no** debe
  ejecutarse con sudo/root).
- **Memoria:** 2–4 GB libres de RAM recomendados.
- **Red:** acceso a las redes de gestión (OLTs, MikroTiks), a Internet
  (apt/pip/pull de imágenes) y —según lo que quieras activar— a Telegram y a
  los servicios de un WISP (API czwisp).
- **Puertos usados:** `8086` (InfluxDB), `6379` (Redis), `8000` (API FastAPI),
  `9108` (Prometheus, opcional).

---

## 3. Paquetes del sistema (apt)

Instala automáticamente `instalar-debian13.sh`, o a mano:

```bash
sudo apt-get update
sudo apt-get install -y podman podman-compose git curl \
    python3 python3-venv python3-pip snmp snmpd
```

Por qué cada uno:

| Paquete | Necesario para |
|---|---|
| `podman` | Contenedores rootless de InfluxDB y Redis |
| `podman-compose` | Levantar los contenedores desde `compose.yml` |
| `git` | Clonar/actualizar el repositorio |
| `curl` | Healthchecks y pruebas de la API |
| `python3` `python3-venv` `python3-pip` | Crear el entorno Python (Debian 13 trae Python 3.13) |
| `snmp` `snmpd` | Binarios `snmpget`, `snmpwalk`, `snmpbulkwalk` que los colectores invocan por subprocess |

---

## 4. Aplicación Python (venv)

Todo el código corre en un `venv/` dentro del proyecto:

```bash
cd /ruta/al/proyecto
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt          # base
venv/bin/pip install -r install/requirements-extra.txt  # extras
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu  # autoencoder óptico (~200 MB)
```

> `torch` CPU es opcional pero recomendado: el autoencoder de potencia óptica
> (detección de degradaciones) lo requiere. Sin él, esa parte queda
> deshabilitada.
>
> `routeros_api` aparece en `requirements.txt` pero solo se usa si algún nodo
> MikroTik tiene `use_api: true` (para obtener `clave_pppoe`). Si su instalación
> falla, instala el resto con `|| true` y omítelo si no lo vas a usar.

---

## 5. Infraestructura: InfluxDB + Redis (contenedores Podman)

En lugar de instalar paquetes en el host, InfluxDB y Redis corren como
contenedores Podman rootless del usuario, descritos en `compose.yml`:

```yaml
# extracto de compose.yml
services:
  influxdb-ia:
    image: docker.io/library/influxdb:2.7
    ports: ["8086:8086"]
    environment:
      - DOCKER_INFLUXDB_INIT_SETUP=false
      - DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=${INFLUXDB_ADMIN_TOKEN}
    volumes:
      - ./influxdb/data:/var/lib/influxdb2
      - ./influxdb/config:/etc/influxdb2

  redis-ia:
    image: docker.io/library/redis:7-alpine
    ports: ["6379:6379"]
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru --appendonly yes
    volumes:
      - ./redis/data:/data
```

### Levantar los contenedores

```bash
cd /ruta/al/proyecto
# 1º: crear .env del proyecto con el token admin de InfluxDB
cp install/.env.example .env
$EDITOR .env          # completa INFLUXDB_ADMIN_TOKEN

podman-compose up -d          # o: podman compose up -d
podman-compose ps
curl -s http://localhost:8086/health   # espera {"status":"pass"}
podman exec redis-ia redis-cli ping    # espera PONG
```

### Setup inicial de InfluxDB (una vez)

`DOCKER_INFLUXDB_INIT_SETUP=false` desactiva el auto-setup: hay que crear
org/bucket/token manualmente. Nombres usados por la instalación de referencia:
**org `vidanet`**, **bucket `monitoreo`**.

```bash
podman exec influxdb-ia influx setup --force \
    --username admin --password CAMBIA-ME \
    --org vidanet --bucket monitoreo \
    --token "$(grep INFLUXDB_ADMIN_TOKEN .env | cut -d= -f2)"
```

El token all-access usado aquí es el que luego pones como `INFLUXDB_TOKEN` en
`.env.ia`. El instalador automatizado intenta este paso si detecta que la org
aún no existe (idempotente).

> **Redis es opcional:** si no está disponible, `ClientStore` cae en consultas
> directas a InfluxDB (más lentas) sin perder funcionalidad.

---

## 6. Instalación automatizada

```bash
# Desde el clon del repositorio (carpeta install/)
# Usando /opt/agente-monitor como ubicación (default):
./install/instalar-debian13.sh

# o en otra ruta:
./install/instalar-debian13.sh /ruta/al/proyecto
```

Qué hace (idempotente — se puede re-ejecutar):

1. Instala paquetes apt (§3) y habilita `loginctl enable-linger`.
2. Clona el repositorio en el directorio indicado (si falta).
3. Crea `venv/` e instala `requirements.txt` + extras + torch CPU.
4. Crea `logs/ data/ models/ influxdb/{data,config} redis/data`.
5. Copia `.env.ia`, `.env`, `compose.yml` y cada config desde sus `.example`
   **solo si no existen** (no pisa configs ya editados).
6. Levanta InfluxDB + Redis y espera sus healthchecks.
7. Intenta el onboarding de InfluxDB (org/bucket/token si faltan).
8. Instala las units `systemctl --user` (resuelve `@PROJECT_DIR@`),
   las habilita y las arranca.

Tras la instalación **EDITA `.env.ia`** con tus secretos reales (§7) y reinicia
los servicios.

---

## 7. Configuración esencial del sistema

### `.env.ia` — secretos de la aplicación

Copia de `.env.ia.example` y completa. Variables (con su sección original):

```bash
# === InfluxDB ===
INFLUXDB_URL=http://localhost:8086
INFLUXDB_TOKEN=                       # token all-access creado en el setup (§5)
INFLUXDB_ORG=vidanet
INFLUXDB_BUCKET=monitoreo
INFLUXDB_PASSWORD=

# === Telegram ===
TELEGRAM_BOT_TOKEN=                   # token del bot @BotFather
TELEGRAM_CHAT_ID=                     # chat/grupo donde llegarán alertas

# === SNMP ===
SNMP_COMMUNITY=                       # community de lectura de OLTs/MikroTiks

# === SSH (verificación de fallas) ===
SSH_USERNAME=
SSH_PASSWORD=

# === API Authentication ===
# Generar con: python -c "import secrets; print(secrets.token_urlsafe(32))"
API_KEY=                              # admin: lectura + escritura (OBLIGATORIO)
API_KEY_VIEWER=                       # solo lectura (opcional)
API_KEY_OPERATOR=                     # lectura + config LLM (opcional)

# === CORS ===
ALLOWED_ORIGINS=                      # lista separada por comas; vacío = mismo origen

# === HSTS ===
ENABLE_HSTS=false                     # true solo con HTTPS válido

# === API czwisp (base de clientes del WISP) ===
WISP_API_KEY=                         # obligatorio si se habilita la ficha de clientes
WISP_ISP_SLUG=                        # ej: vidanet
WISP_LIST_ENDPOINT=/api/webhooks/cliente/{slug}/clientes
WISP_DOCUMENT_ENDPOINT=/api/webhooks/cliente/{slug}/cliente?documento={documento}
WISP_SERIAL_ENDPOINT=/api/webhooks/cliente/{slug}/servicio?onu={serial}
WISP_DOCUMENTOS_CSV=data/wisp_documentos.csv   # semilla local de clientes

# === OLT Web (VSOL) — respaldo MAC por ONU ===
OLT_WEB_ENABLED=false                 # credenciales del panel web VSOL
OLT_WEB_USER=root
OLT_WEB_PASSWORD=

# === Redis (ClientStore) ===
REDIS_URL=redis://localhost:6379/0

# === Rutas (defaults que los scripts ya aplican) ===
CONFIG_PATH=config/config_ia.yaml
MODEL_PATH=models/optical_autoencoder.pt
NODES_PATH=config/nodes.yaml
PING_TARGETS_PATH=config/ping_targets.yaml
PROMETHEUS_PORT=9108
```

### `config/nodes.yaml` — topología de la red

Copia de `config/nodes.yaml.example`. Define zonas, MikroTiks y OLTs:

```yaml
nodes:
- name: NODO-EJEMPLO
  description: Nodo de ejemplo - remplaza con un nombre real
  interval_seconds: 60
  mikrotiks:
  - ip: 10.0.0.1
    hostname: MIKROTIK-EJEMPLO
    community: CHANGEME_SNMP_COMMUNITY
    role: distribucion          # principal | distribucion | secundario
    modelo: RB4011iGS+RM
    interfaces: []
    conectado_a: 10.0.0.254
  olts:
  - ip: 10.0.0.2
    hostname: OLT-EJEMPLO
    community: CHANGEME_SNMP_COMMUNITY
    modelo: V1600G2B            # o VSOL (G2B/G3B/V2802)
    pon_count: 16
    descripcion: OLT de ejemplo
    conectado_a: 10.0.0.1
monitoring:
  default_interval: 60
  default_snmp_timeout: 15
  device_timeouts: {mikrotik: 10, olt: 15, olt_g2b: 20}
```

> Para obtener `clave_pppoe` en la ficha de clientes, el nodo MikroTik debe
> incluir además `use_api: true` y credenciales de la API de RouterOS. El
> collector escribe el measurement `routeros_pppoe` solo en ese caso.

### `config/config_ia.yaml` — configuración del motor de IA

Copia de `config/config_ia.yaml.example`. Valores clave de la instalación de
referencia (no los cambies sin saber): colectores cada 900 s; scheduler
`enrichment` cada 15 min con `lookback_hours: 6` (168 h provocaba OOM-kill del
scheduler por materializar ~10.7 M filas); `fault_detection` cada 15 min;
LLM provider `ollama` (o el que definas). Ruta de nodos: `config/nodes.yaml`.

> `olt_config_path: config/olts.conf` se referencia pero hoy la configuración de
> OLTs vive en `nodes.yaml`; el archivo no es necesario.

### `config/ping_targets.yaml` — monitoreo ICMP

Copia de `ping_targets.yaml.example`. Lista de blancos (DNS públicos, MikroTiks,
OLTs) con tipo, timeouts y conteos.

### `config/instructions.json` — instrucciones del agente

Copia de `instructions.json.example`. Lista de tareas del agente IA (detección
de anomalías, alertas Telegram, resumen diario, reentrenamiento, lookup de
clientes, reportes…). El ejemplo viene con estado en cero; el sistema actualiza
`last_run`, `last_result` y `run_count` automáticamente.

### Otros configs incluidos

- `config/config.yaml` — monitoreo de nivel 1 (intervalos, telegram, ml).
- `config/llm_config.json` — config del agente LLM (habilitado/silenciado…).
- `config/telegram-report-format.conf` — formato del reporte de Telegram.

### Directorios de runtime

```text
logs/          # logs de servicios y apps
data/          # estado (alert_state, enrichment_state, fault_state, chromadb…)
models/        # optical_autoencoder.pt + optical_autoencoder_threshold.txt
influxdb/data  # datos de InfluxDB (volumen del contenedor)
influxdb/config
redis/data     # datos de Redis (volumen del contenedor)
```

> El `optical_autoencoder.pt` y el `_threshold.txt` se regeneran solos al
> arrancar el ai-agent si faltan (tras acumular suficientes muestras).

---

## 8. Servicios `systemctl --user` — ubicación y control

Todas las unidades residen en **`~/.config/systemd/user/`** del usuario que
instala. Fuente: `install/systemd/*.service` (y el repo en `systemd/`), con el
placeholder `@PROJECT_DIR@` que `manage-services.sh install all` reemplaza por
la ruta real del proyecto.

| Servicio | Unit | Qué ejecuta |
|---|---|---|
| Todo-en-uno | `agente-monitor.service` | `scripts/start-all.sh` (collector + API + AI + LLM) |
| Colectores SNMP | `agente-monitor-collector.service` | `python3 -m src.collectors.main` |
| API FastAPI | `agente-monitor-api.service` | `uvicorn src.api.main:app --port 8000` |
| Motor IA | `agente-monitor-ai-agent.service` | `python3 -m src.ai.main` |
| Agente LLM | `agente-monitor-llm-agent.service` | `python3 -m src.ai.llm_agent.main` |
| Agente WISP 24/7 | `agente-monitor-wisp-agent.service` | `python3 -m src.ai.agent_loop` |
| Scheduler | `agente-monitor-scheduler.service` | `python3 -m src.scheduler_cli` |
| Bot Telegram | `agente-monitor-bot.service` | `python3 -m src.telegram_bot` |

Los contenedores de InfluxDB y Redis, si decides correrlos como quadlet de
Podman (en vez de `podman-compose`), tienen sus archivos en
**`~/.config/containers/systemd/`** (`influxdb-ia.container`,
`redis-ia.container`) y generan las units `influxdb-ia.service` y
`redis-ia.service`.

### Instalar / habilitar / arrancar

```bash
# Para que systemctl --user funcione sin sesión gráfica:
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=${XDG_RUNTIME_DIR}/bus

# Instalar units (desde el repo, reemplaza @PROJECT_DIR@):
scripts/manage-services.sh install all

# Habilitar arranque automático:
scripts/manage-services.sh enable all        # o unit a unit

# Arrancar / detener / reiniciar / estado:
systemctl --user start   agente-monitor-collector
systemctl --user stop    agente-monitor-api
systemctl --user restart agente-monitor-scheduler
systemctl --user status  agente-monitor-bot --no-pager
systemctl --user status  --no-pager           # todos

# Alternativa con el gestor del proyecto:
scripts/manage-services.sh status all
scripts/manage-services.sh restart api
```

### Arranque automático al encender

```bash
sudo loginctl enable-linger $USER       # ya lo hace el instalador
```

### Variables de entorno de las units

Cada unit define `WorkingDirectory`, `Environment=CONFIG_PATH=…`,
`Environment=NODES_PATH=…` y `EnvironmentFile=<proyecto>/.env.ia`. Los logs van a
`logs/<servicio>.log` (api → `api.log`+`api-error.log`, etc.). Ejemplo real:

```ini
[Unit]
Description=Agente Monitor - Colectores SNMP (OLT + MikroTik + ICMP)
After=influxdb-ia.service
Wants=influxdb-ia.service

[Service]
Type=simple
WorkingDirectory=/opt/agente-monitor
Environment=CONFIG_PATH=config/config_ia.yaml
Environment=NODES_PATH=config/nodes.yaml
EnvironmentFile=/opt/agente-monitor/.env.ia
ExecStart=/opt/agente-monitor/venv/bin/python3 -m src.collectors.main
Restart=always
RestartSec=10
TimeoutStopSec=30
KillSignal=SIGTERM
StandardOutput=append:/opt/agente-monitor/logs/collector.log
StandardError=append:/opt/agente-monitor/logs/collector-error.log

[Install]
WantedBy=default.target
```

---

## 9. Verificación después de instalar

```bash
# Contenedores
podman-compose ps
curl -s http://localhost:8086/health
podman exec redis-ia redis-cli ping

# Units
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=${XDG_RUNTIME_DIR}/bus
systemctl --user status --no-pager

# API
curl -s http://localhost:8000/health
# Ficha de cliente (ej), con cabecera X-API-Key (admin/viever/operator):
curl -s -H "X-API-Key: TU_API_KEY" \
  "http://localhost:8000/api/clientes/vital/batch" \
  -d '{"queries":[{"query":"PDVSA"}],"include_history":false,"include_infra":true}' \
  -H "Content-Type: application/json"

# Tests (392)
cd /ruta/al/proyecto && venv/bin/python -m pytest tests -q

# Logs
tail -f logs/collector.log logs/api.log
```

---

## 10. Troubleshooting

| Síntoma | Causa / solución |
|---|---|
| `401 Unauthorized` en InfluxDB | `INFLUXDB_TOKEN` vacío o incorrecto en `.env.ia`. Usa el token all-access del setup. |
| API responde `403/401` | Falta `API_KEY` (admin) en `.env.ia`, o la cabecera `X-API-Key` es incorrecta. |
| Scheduler muere por OOM | Mantén `enrichment.lookback_hours: 6` (no subirlo a 168: ~10.7 M filas → pico 7 GB). |
| Units "not found" tras reclonar | `manage-services.sh install all` — el `daemon-reload` silencioso del install olvida units huérfanas; reinstalar las deja visibles. |
| `podman-compose` falla | Verifica `.env` con `INFLUXDB_ADMIN_TOKEN`; imágenes: `podman pull docker.io/library/influxdb:2.7`. |
| Puerto 8000 ocupado | Cambia `api.port` en `config_ia.yaml` (y el `--port` de la unit) o libera el puerto. |
| Las fichas no traen MAC | El CPE en esa PON no transmitió aún (FDB sin MAC aprendida). Los seriales se normalizan a mayúsculas al buscar (`(?i)`). |
| `clave_pppoe` vacía | Se necesita `use_api: true` + credenciales RouterOS en `config/nodes.yaml`; sin eso no se escribe `routeros_pppoe`. |
| Torch no instalado | Detecta errores al entrenar el autoencoder; instálalo con `pip install torch --index-url https://download.pytorch.org/whl/cpu`. |

---

## 11. Resumen del flujo de instalación manual (sin script)

```bash
# 1. apt
sudo apt-get update && sudo apt-get install -y podman podman-compose git curl python3 python3-venv python3-pip snmp snmpd
sudo loginctl enable-linger $USER

# 2. código
cd /opt && sudo git clone <repo> agente-monitor && sudo chown -R $USER:$USER agente-monitor

# 3. venv
cd /opt/agente-monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r install/requirements-extra.txt
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu

# 4. configs y contenedores
mkdir -p logs data models influxdb/{data,config} redis/data
cp install/.env.ia.example .env.ia && $EDITOR .env.ia
cp install/.env.example .env && $EDITOR .env
cp install/config/*.example config/    # y edita nodes.yaml, config_ia.yaml...
podman-compose up -d
# setup InfluxDB (§5)

# 5. units
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=${XDG_RUNTIME_DIR}/bus
scripts/manage-services.sh install all
scripts/manage-services.sh enable all
scripts/manage-services.sh start all
```