# Despliegue en `test-monitor` (LXC Debian 12) — Dependencias y unidades `systemctl --user`

> Generado durante el despliegue de `agente-monitor` el 2026-10-15.
> Los `.service` de esta carpeta son las plantillas de `systemd/` con `@PROJECT_DIR@`
> sustituido por **`/home/monitor/agente-monitor`**.

## Host destino

| Dato | Valor |
|------|-------|
| Hostname | `test-monitor` |
| Tipo | Contenedor LXC (Proxmox), kernel 5.15.158-2-pve |
| OS | Debian GNU/Linux 12 (bookworm), amd64 |
| IP | `172.16.125.9` |
| SSH | puerto **22** (el 2022 está cerrado), usuario `monitor` (grupo `sudo`) |
| Recursos | 4 vCPU · 8 GB RAM · 74 GB disco libre |
| Python | 3.11.2 (`python3`) |
| Podman | 4.3.1 + `podman-compose` 1.0.3 |
| Proyecto | `/home/monitor/agente-monitor` |
| venv | `/home/monitor/agente-monitor/venv` (Python 3.11.2, 85 paquetes) |

---

## 1 · Dependencias APT (sistema)

Instaladas con `apt-get install`:

| Paquete | Motivo |
|---------|--------|
| `python3-venv` | crear el entorno virtual |
| `python3-pip` | instalador de paquetes Python |
| `curl` | health checks (InfluxDB `:8086/health`, API `:8000/health`) |
| `snmp` | provee `snmpbulkwalk` (NET-SNMP 5.9.3) — usado vía *subprocess* por el collector MikroTik y `snmp_live` |
| **`fping`** | usado por `src/collectors/ping_collector.py`. **Faltaba en `install/instalar-debian13.sh`**: sin él el collector loguea `fping not found`, todos los dispositivos parecen caídos y el `wisp-agent` dispara **falsas alertas `mass_down` a Telegram**. Instalado como corrección durante el despliegue. |

**Ya presentes en el LXC** (no requieren instalación): `podman`, `podman-compose`, `git`, `python3`, `ping`, `systemctl`, `loginctl`.

---

## 2 · Dependencias Python

### 2.1 `requirements.txt`

```
requests>=2.28.0
python-telegram-bot>=20.0
pysnmp>=6.0.0
routeros_api>=1.4.0          ← NO instalada (ver §5)
paramiko>=3.0.0
scikit-learn>=1.2.0
pandas>=2.0.0
pyyaml>=6.0
pytest>=7.0.0
responses>=0.23.0
influxdb-client>=1.36.0
matplotlib>=3.7.0
```

### 2.2 `install/requirements-extra.txt`

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
redis>=5.0.0
numpy>=1.26.0
scipy>=1.11.0
aiohttp>=3.9.0
orjson>=3.9.0
python-multipart>=0.0.9
jinja2>=3.1.0
pydantic>=2.0.0
websockets>=12.0
```

### 2.3 Extra

```
torch  (CPU, desde https://download.pytorch.org/whl/cpu)
```

### 2.4 Versiones instaladas

```
fastapi 0.143.2 · influxdb-client 1.50.0 · numpy 2.4.6 · pandas 3.0.6
paramiko 5.0.0 · pydantic 2.14.1 · pysnmp 7.1.30 · redis 8.1.0
scikit-learn 1.9.1 · torch 2.14.1+cpu · uvicorn 0.54.0   (85 paquetes totales)
```

---

## 3 · Imágenes de contenedor (Podman **rootful**)

| Imagen | Servicio | Puerto | Volumen |
|--------|----------|--------|---------|
| `docker.io/library/influxdb:2.7` | `influxdb-ia` | `8086` | `./influxdb/data`, `./influxdb/config` |
| `docker.io/library/redis:7-alpine` | `redis-ia` | `6379` | `./redis/data` |

**¡IMPORTANTE!** Podman **rootless** no funciona en este LXC (`newuidmap: write to uid_map failed: Operation not permitted`).
Se usan **contenedores rootful** con `sudo podman-compose`. Para auto-arranque en boot está habilitado `podman-restart.service`.

---

## 4 · Unidades `systemctl --user`

Instaladas en `~/.config/systemd/user/` (renderizadas desde `systemd/` por
`scripts/manage-services.sh install all`). Se habilitaron con `linger` activo.

| Unidad | Módulo (`ExecStart`) | Rol | Log |
|--------|----------------------|-----|-----|
| `agente-monitor-collector.service` | `src.collectors.main` | Colectores SNMP/ICMP (OLT, MikroTik) | `logs/collector.log` |
| `agente-monitor-api.service` | `uvicorn src.api.main:app :8000` | API + dashboard web | `logs/api.log` |
| `agente-monitor-ai-agent.service` | `src.ai.main` | Motor IA (autoencoder, anomalías, predicciones) | `logs/ai-agent.log` |
| `agente-monitor-llm-agent.service` | `src.ai.llm_agent.main` | Agente LLM (Ollama / OpenRouter / Anthropic) | `logs/llm-agent.log` |
| `agente-monitor-wisp-agent.service` | `src.ai.agent_loop` | Agente WISP 24/7 (fases 4-6) | `logs/wisp-agent.log` |
| `agente-monitor-scheduler.service` | `src.scheduler_cli` | Reportes, health checks, reindex RAG | `logs/scheduler.log` |
| `agente-monitor-bot.service` | `src.telegram_bot` | Bot Telegram interactivo | `logs/bot.log` |
| `agente-monitor.service` | `scripts/start-all.sh` | **Unidad unificada** (todo-en-uno; NO habilitada a la vez que las 7) | journal |

### Control

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

systemctl --user status  'agente-monitor-*' --no-pager
systemctl --user restart agente-monitor-api
./scripts/manage-services.sh status
```

---

## 5 · Notas / decisiones tomadas

1. **`routeros_api` NO instalada**: en PyPI la versión máxima es `0.21.0`, por lo que `routeros_api>=1.4.0` es insatisfacible y rompe el `pip install -r requirements.txt`. Ningún nodo de `config/nodes.yaml` usa `use_api: true` y el código lo importa de forma perezosa con manejo de ausencia (`_collect_routeros_api`). Se instaló `requirements.txt` **sin esa línea** (idéntico al venv de referencia de producción).
2. **`Linger=yes`** en `monitor` → las unidades `--user` arrancan en boot.
3. **InfluxDB bootstrap**: org `vidanet`, bucket `monitoreo`, token all-access = `INFLUXDB_ADMIN_TOKEN` (idéntico a `.env.ia::INFLUXDB_TOKEN`).
4. **Secretos**: se copiaron los configs reales (`.env`, `.env.ia`, `config/nodes.yaml`, `config/config_ia.yaml`, `config/instructions.json`, etc.). El remoto tiene las mismas credenciales/nodos que el origen → vigilar **alertas duplicadas** si ambos están activos.
5. **Puertos expuestos en `0.0.0.0`** (`8086` InfluxDB, `6379` Redis, `8000` API). Para restringir a localhost, cambiar `ports:` en `compose.yml` a `127.0.0.1:...`.

---

## 6 · Estado de despliegue (2026-10-15)

| Componente | Estado |
|------------|--------|
| `collector` · `api` · `ai-agent` · `llm-agent` · `wisp-agent` · `scheduler` · `bot` | `active` + `enabled` (0 reinicios) |
| InfluxDB (`influxdb-ia`) | `healthy` v2.7.12 · org `vidanet` · bucket `monitoreo` |
| Redis (`redis-ia`) | `healthy` PONG |
| API `:8000/health` | `{"status":"healthy","service":"monitoreo-ia","version":"1.1.0"}` |
| Auto-arranque en boot | `Linger=yes` (units `--user`) + `podman-restart.service` (contenedores) |
| Entregable `systemctl/` | 9 ficheros (8 units + `DEPENDENCIAS.md`) — idénticos a los desplegados |

### 6.1 Limitaciones pendientes de decisión

1. **Ollama no disponible**: `base_url` de `config/config_ia.yaml` es `http://localhost:11434` y ese puerto está cerrado tanto en el LXC como en el host de origen (`10.0.4.6:11434`). `llm-agent` y `wisp-agent` corren en **modo fallback-rules** (degradado, sin diagnóstico LLM).
2. **Conflicto de bot de Telegram**: el bot de origen (mismo token `8972872964:...`) ya hace `getUpdates`, así que el remoto recibe **409 Conflict** en bucle. Solo puede haber **un** consumidor de `getUpdates` por token.
3. **Alertas duplicadas**: origen y remoto usan los mismos nodos, secretos y chat de Telegram → las alertas reales se enviarían **dos veces**. Antes de corregir `fping` el remoto envió 1 alerta falsa (`mass_down`); ya no se repite, pero el riesgo de duplicación de alertas reales persiste.
