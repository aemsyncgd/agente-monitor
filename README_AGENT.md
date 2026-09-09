# Agente IA Autónomo — Monitoreo WISP (Fases 4–6)

Agente autónomo 24/7 para el monitoreo de red WISP (FTTH/GPON) de VidaNet.
Consume eventos, los enriquece, correlaciona patrones, razona (LLM + reglas),
y actúa con niveles de seguridad (aprobación humana para acciones críticas).

## Arquitectura

```text
collector ──▶ InfluxDB ◀── api (dashboard)
   │
   └─▶ Redis Streams (event_bus) ──▶ wisp-agent (agent_loop)
                                        │
                           enrolla ▶ enriquece ▶ correlaciona ▶ razona ▶ actúa
                                        │
                                       RAG (ChromaDB)      LLM (openai/anthropic/ollama)
                                        │
                              lobby /commands (TelegramBot)
```

Procesos (4 → ahora 5+2):

| Servicio       | Comando                          | Rol |
|----------------|----------------------------------|-----|
| collector      | `python -m src.collectors.main`  | Recoge métricas y publica eventos |
| api            | `uvicorn src.api.main:app`       | Dashboard y API REST/WebSocket |
| ai-agent       | `python -m src.ai.main`          | Detección de anomalías históricas |
| llm-agent      | `python -m src.ai.llm_agent.main`| Agente LLM ligado a Telegram/API |
| wisp-agent     | `python -m src.ai.agent_loop`    | Loop autónomo 24/7 (Fases 4-6) |
| scheduler      | `python -m src.scheduler_cli`    | Tareas programadas (reportes, RAG) |
| bot            | `python -m src.telegram_bot`     | Bot Telegram interactivo |
| redis-ia (Podman) | `podman compose up -d redis-ia` | Event Bus + memoria session |

> **Contenedores: Podman, NO Docker.** InfluxDB corre como contenedor Podman
> (`influxdb-ia`, Up). Los nuevos servicios dependen de `redis-ia` vía Podman.
> Todos los services del agente corren como **systemd de usuario**
> (`systemctl --user`), sin sudo.

## Control de servicios/módulos

Desde el dashboard (`/api/services/*`) o CLI:

```bash
scripts/manage-services.sh status
scripts/manage-services.sh start wisp-agent
scripts/manage-services.sh restart scheduler
scripts/manage-services.sh stop bot
scripts/manage-services.sh enable wisp-agent   # systemd auto-start
scripts/manage-services.sh disable wisp-agent
```

### systemd de usuario (arranque automático, sin sudo)

Los unit files están en `systemd/` (nivel `WantedBy=default.target`).
Se instalan a nivel usuario con `manage-services.sh`:

```bash
scripts/manage-services.sh install all        # copia units a ~/.config/systemd/user/ + daemon-reload
scripts/manage-services.sh enable wisp-agent  # systemctl --user enable
scripts/manage-services.sh enable scheduler
scripts/manage-services.sh enable bot
systemctl --user status agente-monitor-wisp-agent
```

También se pueden instalar a mano:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agente-monitor-{wisp-agent,scheduler,bot}.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agente-monitor-wisp-agent agente-monitor-scheduler agente-monitor-bot
```

La API expone:

- `GET  /api/services` — estado de todos los servicios
- `GET  /api/services/{name}/status` — estado de uno
- `POST /api/services/{name}/action` — body/action: `start|stop|restart|enable|disable`
- `GET  /api/modules` — módulos controlables
- `GET  /api/config` — configuración actual (secretos redactados)

## Endpoints del agente

- `GET /api/agent/health` — heartbeat del wisp-agent
- `GET /api/agent/stats` — estadísticas (eventos, alertas, acciones, LLM)
- `GET /api/audit` — log de auditoría de acciones/alertas/config
- `GET /api/eventbus` — estado del event bus
- `POST /api/eventbus/replay/{count}` — re-procesar eventos no acked
- `GET /api/rag` + `POST /api/rag/reindex` — estado/reindexado del RAG
- `WS  /ws/events` — streaming de eventos en tiempo real

## Bot Telegram

Comandos: `/status`, `/alertas`, `/reporte`, `/topologia`, `/accion <nombre> [device]`,
`/silenciar <zona> <min>` y `/ayuda`.

Las acciones de nivel 3 (reboot OLT, cambiar config, bloquear cliente) envían
botones inline ✅/❌ al chat; quien responda aprueba/rechaza la acción.

## Seguridad de acciones

Niveles 0–3 (configurable en `config/actions`):

| Nivel | Ejemplo                             | Aprobación |
|------:|-------------------------------------|------------|
| 0     | Diagnóstico, consultar topología    | No         |
| 1     | Enviar alerta, abrir ticket         | No         |
| 2     | Ping, traceroute, reboot interfaz   | No (validado) |
| 3     | Reboot OLT, cambiar config, bloquear| **Sí (Telegram)** |

## Configuración

`config/config_ia.yaml` → `src/config_ia.py` (dataclasses).

Secciones nuevas: `redis`, `event_bus`, `rag`, `agent_loop`, `correlator`,
`policy`, `scheduler`, `audit`, `actions`. Credenciales vía `.env.ia`/ambiente.

## RAG

```bash
scripts/seed_rag.py        # carga inicial (docs/, topología, fallas)
curl -X POST API /api/rag/reindex
```

## Tests

```bash
venv/bin/python -m pytest tests -x -q
```

## Notas

- En modo degradado (LLM cae N veces) el agente sigue con reglas determinísticas.
- Las alertas críticas pasan 24/7; warning/info en horario no laboral se acumulan
  y resumen cada 4h (anti-spam por zona).