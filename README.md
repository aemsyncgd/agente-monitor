# Agente Monitor — Monitoreo de Red FTTH/GPON con IA

Sistema de monitoreo autónomo para proveedores de Internet FTTH/GPON. Recopila datos de equipos de red (OLTs y MikroTiks) vía **SNMP**, detecta **anomalías ópticas** con un **autoencoder** entrenado en PyTorch, correlaciona fallas con clientes afectados, y **genera análisis y alertas** automáticas con un **agente LLM** (Ollama / OpenRouter / Anthropic). Todo se muestra en un **dashboard web** (FastAPI + InfluxDB) y se notifica por **Telegram**.

> **Nota de seguridad:** este repositorio está pensado para publicarse. Los secretos (tokens, passwords, communities SNMP, topología real de red) **no** se incluyen: se cargan desde `.env` / `.env.ia` y archivos de configuración ignorados por `.gitignore`. Ver [Seguridad](#seguridad).

---

## Características

- **Colectores SNMP** de OLTs (potencia óptica ONU RX/TX en dBm, puertos GE/PON, conteos de ONUs, CPU/RAM/temp) y MikroTiks (tráfico de interfaces, errores, discards, MTU, PPPoE, temperatura, CPU por núcleo).
- **Monitoreo ICMP** de disponibilidad/ latencia de equipos y DNS.
- **Detección de anomalías con ML** — autoencoder sobre series de potencia óptica (umbral configurable, reentrenamiento diario).
- **Predicción de fallas** basada en tendencias de degradación de potencia (horas estimadas).
- **Agente LLM autónomo** — inspecciona la red con herramientas de solo lectura (`get_network_summary`, `get_anomalies`, `lookup_client`, `clients_on_pon`, `predict_failures`, …), correlaciona OLT→PON→NAP→clientes y genera informes o alertas.
- **Correlación de fallas con clientes** — a partir de un CSV de clientes (~millares de suscripciones), agrupa afectados por zona geográfica y por casilla PON.
- **Notificaciones Telegram** — alertas con cooldown, reportes diarios programados con gráficos, resúmenes e incidencias (LOS/DyingGap).
- **Dashboard web** (FastAPI + HTML/JS) — vista MikroTiks, OLTs con detalle por puerto PON/ONU, anomalías, predicciones, agentes IA/LLM, reportes y CRUD de nodos.
- **Almacenamiento en InfluxDB 2.x** y puente opcional a **Zabbix** (trapper).
- **API REST con autenticación** por API key (`admin` / `operator` / `viewer`), CORS configurable y rate limiting.

## Arquitectura

```
                    ┌─────────────────────────────────────────────┐
                    │                InfluxDB 2.x                 │
                    │        (almacén de métricas y eventos)      │
                    └───────▲───────────────────▲─────────────────┘
                            │                   │
     SNMP / ICMP            │                   │
  ┌─────────────────┐   escrituras          lecturas
  │   Collectors    │──────┘                   │
  │  (OLT + MikroTik)│                         │
  │  - potencia óptica│    ┌───────────────────┴──────────────────┐
  │  - tráfico/iface │    │             API (FastAPI)             │
  │  - ping targets  │    │  /api/metrics, /anomalies, /agent,    │
  └─────────────────┘    │  /reporte, /nodes, /devices, /ping     │
         │               │  → Dashboard web + API REST autenticada│
         ▼               └───────▲───────────────▲────────────────┘
  ┌─────────────────┐            │               │
  │  AI Engine      │        informes        estado / eventos
  │  (autoencoder + │            │               │
  │   predicciones) │    ┌───────┴───────────────┴────────┐
  └─────────────────┘    │   Agente LLM (autónomo/hybrid) │
                         │   Ollama · OpenRouter · Anthropic│
                         └──────────────▲──────────────────┘
                              alertas / análisis
                                        │
                              ┌─────────┴──────────┐
                              │   Telegram Bot     │
                              └────────────────────┘
```

Las aplicaciones se ejecutan como **servicios systemd de usuario**; InfluxDB puede correr en Docker (`compose.yml`).

## Estructura del proyecto

```
agente-monitor/
├── src/
│   ├── main.py                  # Agente base (Zabbix → eventos, legacy)
│   ├── config.py / config_ia.py # Carga de configuración + variables de entorno
│   ├── collectors/              # Colectores SNMP (OLT/MikroTik) + escaneo ONU
│   ├── ai/                      # Motor IA: autoencoder, detección, agente LLM
│   │   └── llm_agent/           # Agente LLM (herramientas de solo lectura)
│   ├── api/                     # API FastAPI + dashboard (static/ y templates/)
│   ├── reporte/                 # Generación de reportes y gráficos
│   ├── storage/                 # Cliente InfluxDB
│   ├── notifier*.py             # Notificaciones Telegram
│   ├── grouper.py               # Agrupación de clientes por zona (ML)
│   ├── client_lookup.py         # Lookup ONU → cliente
│   └── buffer.py / verifier.py  # Amortiguación de eventos y verificación
├── config/
│   ├── config.yaml              # Config base del agente
│   ├── config_ia.yaml           # Config IA/colectores/LLM/API
│   ├── nodes.yaml.example       # Plantilla de nodos (OLTs/MikroTiks)
│   ├── ping_targets.yaml.example# Plantilla de blancos ICMP
│   ├── instructions.json        # Instrucciones del agente IA
│   ├── llm_config.json          # Config runtime del agente LLM
│   └── telegram-report-format.conf
├── tests/                       # Suite de pruebas (pytest)
├── scripts/start-all.sh         # Arranque de todos los componentes
├── mantenimiento.sh             # Atajos systemctl --user (--ready/--now/--status)
├── compose.yml                  # InfluxDB en Docker
├── Dockerfile / Dockerfile.ai / Dockerfile.api / Dockerfile.collector
├── requirements.txt             # Dependencias base
└── requirements-ia.txt          # Dependencias IA/API/test
```

## Requisitos

- **Python 3.11+**
- **InfluxDB 2.x** (recomendado vía `compose.yml`)
- Acceso **SNMP** a OLTs y MikroTiks; **ICMP** a blancos de ping
- **Telegram Bot** (para notificaciones) — opcional si se deshabilita
- **Ollama** (o API OpenRouter/Anthropic) — solo si se usa el agente LLM

## Puesta en marcha

### 1. Clonar y preparar el entorno

```bash
git clone https://github.com/aemsyncgd/agente-monitor.git
cd agente-monitor

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-ia.txt
```

### 2. Configurar secretos

Nunca edites directamente los secretos en el código. Cópia las plantillas y completa:

```bash
cp .env.example .env            # secretos base (compose usa INFLUXDB_ADMIN_TOKEN)
cp .env.ia.example .env.ia      # secretos del sistema IA
cp config/nodes.yaml.example config/nodes.yaml          # topología real
cp config/ping_targets.yaml.example config/ping_targets.yaml
```

> `config/nodes.yaml` y `config/ping_targets.yaml` contienen IPs internas y
> communities SNMP: están excluidos de git para evitar su publicación.

| Variable | Descripción |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram para alertas/reportes |
| `TELEGRAM_CHAT_ID` | Chat o grupo al que se envían las notificaciones |
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` | Endpoint y token de InfluxDB (por defecto `http://localhost:8086`) |
| `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | Organización y bucket de InfluxDB |
| `INFLUXDB_ADMIN_TOKEN` | Token admin inicial de InfluxDB (usado por `compose.yml`) |
| `SNMP_COMMUNITY` | Community string SNMP de los equipos |
| `SSH_USERNAME` / `SSH_PASSWORD` | Credenciales SSH para verificación de fallas |
| `API_KEY` | API key **admin** de la API (obligatoria; lectura + escritura) |
| `API_KEY_VIEWER` | API key de solo lectura (opcional) |
| `API_KEY_OPERATOR` | API key lectura + configuración LLM (opcional) |
| `ALLOWED_ORIGINS` | Orígenes CORS permitidos (coma-separados; vacío = same-origin) |
| `ENABLE_HSTS` | HSTS solo si se sirve HTTPS con certificado válido |
| `OPENROUTER_API_KEY` | API key (si el LLM usa OpenRouter) |
| `CONFIG_PATH` | Ruta del archivo de config (`config/config_ia.yaml`) |

Genera claves seguras con:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Iniciar InfluxDB

```bash
docker compose up -d
# o bien, si usas systemd user:  systemctl --user start influxdb-ia.service
```

### 4. Ejecutar el agente

Con un solo comando (espera InfluxDB y lanza collector + API + AI + LLM):

```bash
./scripts/start-all.sh
```

Con systemd (`agente-monitor.service`) puedes usar los atajos:

```bash
./mantenimiento.sh --ready    # iniciar todo y esperar a que esté operativo
./mantenimiento.sh --status   # estado de servicios
./mantenimiento.sh --now      # detener todo
```

Componentes individuales:

```bash
python -m src.collectors.main          # colectores SNMP
uvicorn src.api.main:app --host 0.0.0.0 --port 8000   # dashboard/API
python -m src.ai.main                  # motor IA (anomalías/predicciones)
python -m src.ai.llm_agent.main        # agente LLM
```

El dashboard estará en `http://<host>:8000/` (protegido con API key vía cabecera `X-API-Key`).

### 5. Container (opcional)

Cada componente tiene su `Dockerfile`:

```bash
docker build -f Dockerfile.ai -t agente-monitor-ai .
docker build -f Dockerfile.collector -t agente-monitor-collector .
docker build -f Dockerfile.api -t agente-monitor-api .
```

## Pruebas

```bash
source venv/bin/activate
pytest
```

## Seguridad

- **Los secretos nunca se versionan.** `.env`, `.env.ia`, `backups/`, `logs/`,
  `apis.md` (tokens), `config/nodes.yaml` y `config/ping_targets.yaml`
  (topología + communities SNMP), `influxdb/` y `grid_servicios.csv`
  (datos de clientes) están en `.gitignore`.
- La API usa autenticación por API key con roles (`admin` / `operator` / `viewer`)
  y CORS restringido. Activa `ENABLE_HSTS` solo con HTTPS válido.
- Si un secreto se filtra alguna vez, **rótalo inmediatamente** (asume que está comprometido).
- Mantén los límites de tasa y monitorea `logs/` para detectar actividad anómala.

## Licencia

Proyecto licenciado bajo la **GNU General Public License v3.0** — ver [LICENSE](LICENSE).