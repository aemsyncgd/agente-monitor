# Bitácora — Rediseño dashboard Vidanet (/newdesign)

## Objetivo
Crear versión 2 del dashboard (`agente-monitor`) con sistema de diseño **NEXO NOC** bajo el nombre **Vidanet**, accesible en `172.16.126.2:8000/newdesign`. No modificar dashboard principal hasta que esté completa, funcional y revisada. Usar únicamente datos reales.

## Restricciones
- Solo datos reales. Lo inexistente → `requiere backend` (sin datos ficticios).
- Sin modificar dashboard principal. Activación tras validación completa.
- Replicar estilo NEXO NOC; mejorar sección Configuración. Tokens CSS `:root` + `html[data-theme="light"]`. Tipografías Inter + JetBrains Mono.
- Usar stack existente (FastAPI, sin nuevos frameworks/build tools si no existen). Preferir CSS/JS nativos.
- Español (es-PE). Progressive enhancement, accesibilidad (foco visible, teclado completo, ARIA), rendimiento (paginación servidor para tablas grandes, SVG para gráficos, reducción motion), estados vacíos/carga/error.
- Tecnologías: MikroTik, OLT VSOL, WISP. Fichas de clientes → solo si backend puede exponer datos reales con mapeo cliente–PPPoE–ONU.

## Estado actual (2026-10-08)
- [x] Ruta `/newdesign` creada en `src/api/main.py:238-257`.
- [x] Archivo base `src/api/static/newdesign.html` creado (tema oscuro/claro, layout NEXO NOC, tokens CSS, Inter+JetBrains Mono).
- [x] Servicio reiniciado (agente-monitor.service). `/newdesign` responde 200.
- [ ] Completar Fase 1: tabla de correspondencia completa con citas archivo:línea + listado "requiere backend" con estimación.
- [ ] Aprobación Fase 1 → pasar a Fase 2 (Plan) antes de código funcional por secciones.

## Referencias clave
- `src/api/main.py` — rutas FastAPI, sirve estáticos desde `src/api/static/`.
- `src/api/influx_helper.py` — capa datos InfluxDB (caché TTL, single-flight, timeouts).
- `src/api/routes/*` — clientes (`clientes.py`), nodos (`nodes.py`), métricas (`metrics.py`), health/agent_llm/anomalies.
- `src/api/auth.py` — roles/admin/operator/viewer + enmascarado.
- `src/redis_store.py`, `src/wisp_client.py`, `src/models.py` — datos enriquecidos.
- Dashboard actual: `src/api/static/index.html`, `backbone.html`, `olts.html`.

## Tabla de correspondencia (preliminar)

| Sección | Estado | Ruta/Plantilla real | Fuente datos real | Observaciones |
|---|---|---|---|---|
| Panel NOC (KPIs globales) | Parcial | `src/api/static/index.html` (actual) | InfluxDB via `influx_helper` + métricas varias | Faltan KPIs globales agregados específicos NEXO NOC. `requiere backend` para agregados centralizados. |
| Alertas | Parcial | API `routes/anomalies.py`, UI en index.html | InfluxDB/anomalías/autoencoder | Listado existente; ajustar a layout NEXO NOC. `requiere backend` si se desea estructura distinta. |
| Mapa/Topología | Parcial | `backbone.html`, `index.html` (vis-network) | `config/nodes.yaml`, `conectado_a`, estado ping/telemetría | Existe topología interactiva; replicar estilo NEXO NOC. `requiere backend` según campos adicionales. |
| MikroTik | Existe | `index.html` (tarjetas+modal), rutas métricas | `routes/metrics.py`, SNMP/InfluxDB | Ya funcional; migrar a nueva UI. Usar datos reales existentes. |
| OLT VSOL | Existe | `olts.html`, index.html | `routes/nodes.py`, `metrics.py`, InfluxDB | Ya implementado. Migrar con cuidado (ge/PON/ONUs). Datos reales. |
| ONUs/clientes GPON | Parcial | `routes/clientes.py` | clientes enriquecidos (InfluxDB + Redis) | Existe API enriquecida con enmascarado por rol. UI nueva requiere integración. |
| WISP | Parcial | Depende de nodos/MikroTik | nodos configurados | Definir alcance exacto con datos reales existentes. `requiere backend` si faltan vistas específicas. |
| Fichas de clientes | No existe (nueva) | — | — | **NUEVA**. Solo implementar si backend expone datos reales con mapeo cliente–PPPoE–ONU. `requiere backend`. |
| Trabajos programados | Parcial | `src/scheduler.py`, `agente-monitor-scheduler.service` | scheduler existente | Evaluar si exponer UI. `requiere backend` endpoints para listar/gestionar si no existen. |
| Reportes/SLA | Parcial | `src/reporte/` | reportes existentes | Migrar vistas. Usar datos reales. `requiere backend` según requerimientos UI. |
| Auditoría | Parcial | `src/audit.py` (módulos fases 4-6) / logs | auditoría existente | Verificar endpoints/UI. `requiere backend`. |
| Configuración | Parcial | `routes/nodes.py` (CRUD nodos/MikroTik/OLT) | config_manager/config_ia | Mejorar organización (General/Integraciones/Seguridad/Roles/Históricos/Retención/Alertas). CRUD real existe parcialmente. `requiere backend` para resto.

## Pendientes inmediatos
- Completar tabla con citas archivo:línea detalladas.
- Listar explícito `requiere backend` con ítems, justificación, estimación (horas).
- Entregar Fase 1 completa para aprobación → pasar a Fase 2 (Plan).
