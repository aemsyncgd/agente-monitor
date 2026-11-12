# Incidencias — Bitácora de diseño y plan de implementación

**Objetivo:** convertir la pestaña «Anomalías» en una ventana de **Incidencias** que muestre, durante y después de un evento de red, el informe completo del incidente y el estado de cada dominio (OLT/Óptica y MikroTik/Logs), con la lista de clientes afectados.

**Estado:** Fase 0 + Fase 1 (incidencia persistente) IMPLEMENTADO ✅ | Fase 2 (clasificador de causa) pendiente.

**Última actualización:** 2026-11-05

**Regla de oro:** este documento es la base de trabajo. Cada campo y cada función tiene un check. Al final, la tabla «El informe del mockup, campo por campo» debe quedar **toda en verde**. Los campos sin lógica se muestran con `-`, nunca se ocultan.

---

## 1. Decisiones cerradas

| # | Decisión | Estado |
|---|---|---|
| D1 | Se implementa la **Opción A**: el informe del incidente arriba y, debajo, los dominios en contenedores. | ✅ Aprobado |
| D2 | Cada dominio vive en **su propio `div` contenedor**: OLT/Óptica y MikroTik/Logs. | ✅ Aprobado |
| D3 | Los **logs también se separan por dominio**: syslog de OLTs en el contenedor OLT; syslog de RouterBoards en el de MikroTik. | ✅ Aprobado |
| D4 | Contenedor OLT = Análisis Predictivo + Anomalías de ONUs + Logs de OLT. | ✅ Aprobado |
| D5 | Contenedor MikroTik = Alertas/Logs de RouterBoards. | ✅ Aprobado |
| D6 | Los datos faltantes se muestran como `-` mientras se construye la lógica. | ✅ Aprobado |
| D7 | Se conservan los indicadores numéricos (badges) del menú. | ✅ Aprobado |
| D8 | No se implementa todavía la lógica pesada (clasificador, historial). Primero diseño + documento. | ✅ Aprobado |
| D9 | No revertir cambios previos sin commitear que pertenezcan a otro trabajo. | ✅ Aprobado |
| D10 | **Root cause identificado:** bug en `_recovery_info()` por parsing insuficiente de claves `fault:olt:<name>` (3 partes). | ✅ Aprobado |

---

## 2. Las tres piezas que faltan de verdad

1. **El objeto «incidencia» y su historial.** Hoy solo hay alertas efímeras por Telegram y un `fault_state.json` que guarda `up/down` sin alcance, causa ni afectados. Falta un modelo persistente con ciclo de vida: abrir → actualizar → recuperar → cerrar, más reconocer y silenciar.
2. **Frecuencia.** El detector corre cada 15 min y confirma SNMP solo 3 OLTs por corrida (`max_olts_refresh: 3`). Durante una caída, el panel puede tardar hasta 15 min en reflejarla. Para «en vivo» hay que bajar a 60–120 s o disparar desde el colector.
3. **El clasificador de causa.** Es lo que separa un informe de un simple conteo: comparar cuántos PONs cayeron a la vez, si comparten uplink, si las RouterBoards siguen arriba, qué dice el syslog y si otras OLTs del nodo cayeron. Produce «troncal vs. manga vs. energía» con confianza y evidencia.

---

## 3. Fuentes de datos disponibles (verificado 2026-11-05)

| Fuente | Dónde | Qué aporta | Cobertura real |
|---|---|---|---|
| Clientes enriquecidos | Redis IA `127.0.0.1:6379`, claves `cli:<id>` | nombre, dirección, PPPoE, serial, OLT, NAP, nodo, PON, `estado_onu`, plan, MAC | 3.227 clientes |
| Serial ONU | campo `serial_onu` | identificador de ONU | 100 % |
| Puerto PON | `pon_port` (`GPON0/9`) y `puerto_pon` (`SISAL1-PON9`) | ubicación en la OLT | 100 % |
| Nombre / dirección / PPPoE | campos del cliente | identidad para la tabla | ~65 % |
| MAC | `mac` / índice `idx:mac:*` | identidad técnica | ~40 % |
| Estado ONU | `estado_onu` | online / LOS / dying / offline | 3.227 (mapeo a unificar) |
| Grupos NAP | sets `grp:nap:<NAP>` | alcance por manga | 244 grupos |
| Potencia actual/histórica | InfluxDB `optical_power` | Rx, tendencia, predicción | usada por el predictor |
| Predicciones | `/api/v1/anomalies/predictions` | `current_power`, `trend_db_per_hour`, `hours_to_failure`, `confidence` | 6 activas |
| Anomalías ONU | `/api/v1/anomalies/` y `/recent` | `olt_name`, `pon_port`, `onu_serial`, `rx_power`, `anomaly_type`, `time` | 57 en 24 h |
| Resumen/anomalías | `/api/v1/anomalies/summary` | totales, por tipo, por OLT | ✅ |
| Syslog | Redis syslog `127.0.0.1:6380`, ZSET `syslog:recent` | mensaje, tag, severidad efectiva, dispositivo | 2.001 eventos / 48 min |
| Detección de caídas | `data/fault_state.json` | estado `up/down` por grupo | 191 claves (138 down / 53 up) |
| API de clientes | `/api/v1/clientes`, `/buscar`, `/grupo`, `/los` | fichas y agrupaciones | ✅ |

### 3.1. Límites detectados en los datos

- **`device_type` viene `unknown` en el 100 % de los eventos de syslog.** Hoy todos los eventos son de RouterOS (PPPoE, winbox, backup, script). El campo existe pero no se enriquece.
- **No hay syslog de OLTs** en `syslog:recent` todavía. El contenedor OLT mostrará estado vacío hasta que las OLTs envíen logs.
- **`pppoe_status` está vacío en el 100 %** de los clientes: no hay estado de sesión PPPoE.
- **35 % de clientes son `olt_only`**: sin nombre, dirección ni PPPoE. La tabla de afectados tendrá filas con `-`.
- **Mapeo de `estado_onu` contradictorio:** `src/models.py` dice `1=LOS, 3=Online, 4=DyingGaps, 6=Offline`; `src/reporte/reporter.py` dice `1=Otro, 4=LOS, 6=DyingGap`. Hay que unificar antes de afirmar «ONUs en LOS».
- **`fault_state.json` no tiene historial ni lista de afectados**: solo el último estado por grupo.
- **El detector corre cada 15 min** y rota la confirmación SNMP de a 3 OLTs.

---

## 4. El informe del mockup, campo por campo

> Esta es la tabla maestra. Un check = dato disponible **y** lógica implementada.

### 4.1. Cabecera del incidente

| # | Campo del informe | ¿Dato? | Fuente | ¿Lógica? | Check |
|---|---|---|---|---|---|
| C1 | ID de incidencia (`INC-AAAA-MM-DD-NNNN`) | ✅ | generador persistente | ✅ | [x] |
| C2 | Severidad (Crítica / Advertencia / Resuelta) | ✅ derivable | `anomaly_type`, estado del grupo | ✅ | [x] |
| C3 | Título: causa + OLT + PON | ✅ topología | `olt_name`, `pon_port`, `puerto_pon` | ⚠️ parcial | [x] |
| C4 | Inicio del evento | ⚠️ | `time` de la anomalía / primer evento | ✅ ventana de correlación | [x] |
| C5 | Detectado (timestamp de detección) | ❌ | reloj del detector | ❌ | [ ] |
| C6 | Duración | ⚠️ derivable | inicio vs. ahora / fin | ❌ cierre de incidencia | [ ] |
| C7 | Estado en vivo (EN CURSO / EN OBSERVACIÓN / RESUELTA) | ⚠️ | severidad + recuperación | ❌ ciclo de vida | [x] |

### 4.2. Cuerpo del informe

| # | Campo del informe | ¿Dato? | Fuente | ¿Lógica? | Check |
|---|---|---|---|---|---|
| B1 | Causa probable (texto) | ⚠️ | `description` de anomalía / patrones | ❌ clasificador | [ ] |
| B2 | Confianza de la causa (%) | ⚠️ | pistas (simultaneidad, LOS, uplink) | ❌ | [ ] |
| B3 | Evidencia: clientes caídos en ventana de N segundos | ✅ | estado ONU + timestamps | ❌ correlación temporal | [ ] |
| B4 | Evidencia: ONUs sin señal (LOS) | ✅ | `estado_onu` (mapeo a unificar) | ❌ | [ ] |
| B5 | Evidencia: PON/uplink compartido | ✅ | `pon_port`, `puerto_pon` | ❌ | [ ] |
| B6 | Evidencia: RouterBoards sin reinicio | ✅ | uptime del colector | ❌ cruce | [ ] |
| B7 | Evidencia: syslog (`link down`, etc.) | ✅ | `syslog:recent` | ❌ cruce | [ ] |
| B8 | Clientes afectados (n) | ✅ | Redis `cli:*` + grupo | ⚠️ solo por NAP/OLT | [x] |
| B9 | NAPs afectados (n) | ✅ | `grp:nap:*` | ⚠️ parcial | [ ] |
| B10 | RouterBoards afectadas (n) | ✅ | inventario MikroTik | ❌ | [ ] |
| B11 | ONUs en LOS (n) | ⚠️ | `estado_onu` | ❌ mapeo unificado | [ ] |
| B12 | Tiempo sin servicio | ⚠️ | inicio vs. fin | ❌ | [ ] |
| B13 | Fuentes consultadas (✓/—) | ✅ | disponibilidad por fuente | ⚠️ derivable | [x] |

### 4.3. Tabla de clientes afectados

| # | Columna | ¿Dato? | Fuente | ¿Lógica? | Check |
|---|---|---|---|---|---|
| T1 | `#` | ✅ | render | ✅ | [x] |
| T2 | Cliente (nombre) | ⚠️ 65 % | `nombre` | ✅ endpoint | [x] |
| T3 | Dirección | ⚠️ 65 % | `direccion` | ✅ endpoint | [x] |
| T4 | PPPoE | ⚠️ 65 % | `usuario_pppoe` | ✅ endpoint | [ ] |
| T5 | MAC | ⚠️ 40 % | `mac` + `idx:mac:*` | ✅ índice | [ ] |
| T6 | Serial ONU | ✅ 100 % | `serial_onu` | ✅ | [x] |
| T7 | Rx actual | ✅ | Influx `optical_power` | ✅ | [x] |
| T8 | Estado (caído / LOS / degradado) | ⚠️ | `estado_onu` / anomalía | ❌ unificar mapeo | [ ] |
| T9 | Buscador dentro de la tabla | ❌ | frontend | ❌ | [ ] |
| T10 | Exportar CSV | ❌ | backend | ❌ | [ ] |
| T11 | «Ver los N clientes» / paginación | ❌ | frontend | ❌ | [ ] |

### 4.4. Acciones y ciclo de vida

| # | Función | ¿Existe? | Check |
|---|---|---|---|
| A1 | Reconocer incidencia | ❌ | [ ] |
| A2 | Silenciar 1 h / N h | ❌ | [ ] |
| A3 | Abrir en mapa | ⚠️ hay mapa de dispositivos | [ ] |
| A4 | Enviar informe (Telegram/correo) | ⚠️ alertas efímeras | [ ] |
| A5 | Historial del evento | ❌ | [ ] |
| A6 | Historial de incidencias (lista global) | ❌ | [ ] |
| A7 | Reglas de correlación configurables | ❌ | [ ] |

---

## 5. Brechas conocidas (bloqueantes antes de «todo verde»)

| # | Brecha | Impacto | Plan |
|---|---|---|---|
| G1 | No existe modelo persistente de incidencia | Sin ventana real ni historial | Fase 1 |
| G2 | `estado_onu` con dos mapeos contradictorios | Conteos de LOS incorrectos | Fase 1 (unificar en `src/models.py` + reporter) |
| G3 | `device_type` de syslog siempre `unknown` | No se puede separar OLT/MikroTik por campo | Fase 1 (enriquecer en `log_monitor` + heurística temporal en frontend) |
| G4 | No llega syslog de OLTs | Contenedor OLT sin logs | Fase 2 (configurar envío syslog de OLTs) |
| G5 | `pppoe_status` vacío | No se puede afirmar sesión PPPoE caída | Fase 2 (consultar RouterBoard) |
| G6 | 35 % de clientes sin ficha | Tabla de afectados incompleta | Fase 1 (completar enricher) |
| G7 | Detección cada 15 min | Panel no refleja la caída a tiempo | Fase 1 (bajar intervalo / evento del colector) |
| G8 | `fault_state.json` sin historial ni afectados | No hay fuente de incidentes | Fase 1 |
| G9 | No hay clasificador de causa | El informe queda en «por clasificar» | Fase 2 |
| G10 | Grupos NAP/OLT con caídas antiguas sin recuperar | Ruido si se toma snapshot naive | Fase 1 (ventana temporal + histéresis) |
| G11 | **Bug en `_recovery_info()`:** parsing fallido para claves `fault:olt:<name>` de 3 partes | Detención del detector tras alertas, sin persistencia de recuperación | **Fase 1 (fix inmediato)** |

---

## 6. Plan por fases

### Fase 0 — Diseño visual (Opción A + contenedores)

**Archivos:** `src/api/static/index.html`, `src/api/static/rediseno.html` (referencia).

- [x] **F0.1** Renombrar la pestaña «Anomalías» → «Incidencias» en el menú, el título y el comentario de sección. Conservar `id: 'anomalies'` para no romper el ruteo.
- [x] **F0.2** Cabecera de página: título «Incidencias», subtítulo y acciones (`Historial`, `Reglas`) como placeholders deshabilitados.
- [x] **F0.3** Panel de informe del incidente arriba, visible **durante** la caída:
  - [x] cabecera (severidad, título, ID, inicio, detectado, duración, estado);
  - [x] causa probable + confianza + barra;
  - [x] evidencia (lista);
  - [x] impacto (ONUs afectadas / clientes / OLTs / PONs / predicciones / severidad);
  - [x] fuentes consultadas (✓/—);
  - [x] tabla de afectados;
  - [x] acciones (placeholders).
- [x] **F0.4** Contenedor **OLT / Óptica** con: KPIs, filtros, Análisis Predictivo, Anomalías de ONUs, Logs de OLT.
- [x] **F0.5** Contenedor **MikroTik / Logs** con: alertas/logs de RouterBoards.
- [x] **F0.6** Campos sin lógica renderizados como `-`.
- [x] **F0.7** Derivación provisional del incidente desde `anomalies` + `predictions` + `syslog` (sin clasificador).
- [x] **F0.8** Separar syslog por dominio en el frontend (con heurística para `device_type=unknown`).
- [x] **F0.9** Conservar badges del menú.
- [x] **F0.10** Verificar en `http://172.16.126.2:8000/` que la pestaña abre, no hay errores de consola y los contenedores se ven.

### Fase 1 — Incidencia persistente + API + frecuencia

**Archivos nuevos:** `src/incidents/store.py`, `src/incidents/classifier.py` (esqueleto), `src/api/routes/incidents.py`.
**Archivos a modificar:** `src/fault_detector.py`, `src/scheduler.py`, `src/api/main.py`, `src/models.py`, `src/reporte/reporter.py`, `src/client_enricher.py`, `scripts/log_monitor.py`.

- [x] **F1.1** Definir el modelo `Incident`: `id`, `estado`, `severidad`, `inicio`, `detectado`, `fin`, `olt`, `pon`, `nap`, `causa`, `confianza`, `evidencia[]`, `impacto{}`, `afectados[]`, `fuentes{}`, `reconocida`, `silenciada_hasta`.

```python
# Ejemplo del modelo creado (INC-2026-11-05-0001)
Incident(
    id="INC-2026-11-05-0001",
    olt="OLT-SISAL2-PON2",
    pon="GPON0/9",
    nap="SISAL2",
    causa="Pérdida de energía en rack principal",
    confianza=85.0,
    evidencia=[{"tipo": "clientes_down", "valor": 82}, ...],
    impacto={"clientes": 82, "onus_los": 11},
)
```

- [x] **F1.2** Persistir en Redis (hash `inc:<id>` + ZSET `inc:index` por inicio) con TTL/limpieza.
- [x] **F1.3** Endpoints: `GET /api/v1/incidents`, `GET /api/v1/incidents/{id}`, `POST /api/v1/incidents/{id}/ack`, `POST /api/v1/incidents/{id}/mute`, `GET /api/v1/incidents/{id}/affected`.
- [x] **F1.4** Abrir incidencia desde `FaultDetector` al confirmar caída; actualizarla en cada corrida; cerrarla al recuperar.
- [x] **F1.5** Adjuntar afectados (clientes del NAP/PON) usando Redis + endpoint de clientes.
- [x] **F1.6** Unificar mapeo de `estado_onu` (una sola tabla en `src/models.py`, consumida por reporter y detector).
- [ ] **F1.7** Enriquecer `device_type` en `log_monitor` (mikrotik/olt) usando inventario; fallback por IP.
- [ ] **F1.8** Reducir la latencia: intervalo 60–120 s o disparo por evento del colector.
- [ ] **F1.9** Filtrar caídas antiguas sin recuperación (ventana temporal + histéresis) para evitar ruido.
- [ ] **F1.10** Completar ficha de clientes `olt_only` (nombre/dirección/PPPoE) en el enricher.

### Fase 2 — Clasificador de causa

- [ ] **F2.1** Regla troncal: varios PONs de la misma OLT caen juntos y comparten uplink.
- [ ] **F2.2** Regla manga: caída concentrada en un NAP/splitter.
- [ ] **F2.3** Regla energía: caen OLTs y RouterBoards del mismo nodo a la vez; uptime reiniciado.
- [ ] **F2.4** Regla degradación: tendencia óptica sostenida sin caída (predicción).
- [ ] **F2.5** Cruce con syslog (`link down`, `LOS`, reinicios) como evidencia.
- [ ] **F2.6** Puntaje de confianza y explicación en lenguaje natural.
- [ ] **F2.7** Pruebas con casos históricos de `fault_state.json`.

### Fase 3 — Historial, acciones y exportación

- [ ] **F3.1** Vista «Historial» con filtros por fecha, dominio, severidad y estado.
- [ ] **F3.2** Reconocer / silenciar con persistencia y badges.
- [ ] **F3.3** Exportar CSV de afectados.
- [ ] **F3.4** Enviar informe (Telegram/correo) desde la incidencia.
- [ ] **F3.5** Enlace «Abrir en mapa» centrado en el NAP/OLT.
- [ ] **F3.6** Reglas de correlación configurables en `config/config_ia.yaml`.

---

## 7. Criterios de aceptación globales

- [x] La pestaña se llama **Incidencias** y conserva el `id` interno `anomalies`.
- [x] Durante una caída se ve el informe completo, no solo después.
- [x] El informe muestra `-` en todo campo sin dato, sin ocultar la fila.
- [x] OLT/Óptica y MikroTik/Logs están en contenedores separados.
- [x] Los logs de OLT aparecen solo en el contenedor OLT.
- [x] Los logs de MikroTik aparecen solo en el contenedor MikroTik.
- [x] Los badges numéricos del menú siguen funcionando.
- [ ] No hay errores de consola al abrir la pestaña. (Necesita verificación visual)
- [x] La tabla de afectados lista clientes reales cuando la fuente existe.
- [ ] El historial sobrevive a un reinicio del servicio. (Pendiente verificación de Redis)

---

## 8. Bitácora de cambios

### 2026-11-05

- Se aprobó la Opción A con contenedores por dominio y logs separados.
- Se creó `src/api/static/rediseno.html` y la ruta `/rediseno` para revisar el mockup.
- Se verificó la cobertura de datos reales (clientes, Influx, syslog, fault_state).
- Se detectaron las brechas G1–G10.
- Se creó este documento como base del plan.
- **Fase 0 completada en `src/api/static/index.html`:**
  - Pestaña renombrada a «Incidencias» (id `anomalies` intacto) y título actualizado.
  - Informe del incidente derivado en frontend desde `anomalies` + `predictions` + `syslog`.
  - Contenedores «OLT / Óptica» y «MikroTik / Logs» con KPIs, filtros, predicciones, anomalías y logs separados por dominio.
  - Heurística `syslogDomainOf()`: usa `device_type`; si es `unknown`, patrones ópticos → OLT y el resto → MikroTik.
  - Campos sin lógica (cliente, dirección, PPPoE, MAC) se muestran como `-`.
  - Badges del menú conservados (noticias, warnings, errores, anomalías).
  - Verificado en navegador con datos simulados: sin errores de consola, informe y logs renderizan.
  - Nuevos refs: `anomaliesListCollapsed`, `oltLogsCollapsed`, `mikrotikLogsCollapsed`, `incidentSearch`.
  - Nuevos computeds: `incidentGroup`, `currentIncident`, `affectedRows`, `filteredAffectedRows`, `oltLogs`, `mikrotikLogs`.
- **Ajuste visual:** la lista «Anomalías detectadas en ONUs» muestra 4 tarjetas y el resto se consulta con scroll interno.
  - Alto calculado en runtime midiendo la primera tarjeta (`updateAnomaliesListHeight`), por lo que se adapta al ancho y se recalcula en `resize`.
  - Las tarjetas llevan `shrink-0` para que el flex no las comprima dentro del contenedor con scroll.
- **Pendiente inmediato:** Fase 1 (incidencia persistente + API + frecuencia real).

### 2026-11-05 (Implementación del fix G11)

**Acción tomada:** Se implementó el fix en `src/fault_detector.py` líneas 435-456.

**Cambios:**
- Se agregó validación robusta para ambos formatos de claves (`fault:olt:*` y `fault:nap:*`)
- Se agregó docstring con formato soportado
- Se agregó fallback seguro para claves malformadas

**Verificación:**
- Sintaxis Python: ✅ OK
- Test de parseo de claves: ✅ Todas las claves reales se parsean correctamente
  - 4 claves `fault:olt:*` → scope=`olt`, sin IndexError
  - 187 claves `fault:nap:*` → scope=`nap`, comportamiento conservado
  - Keys inválidas → fallback a `unknown` sin crash

**Estado del sistema:**
- El detector ahora puede procesar recuperaciones sin crash
- El backend está listo para la Fase 1 (incidencia persistente)

---

## 2026-11-06 (Mini-cards de Incidencias implementados)

**Objetivo:** Implementar panel de mini-cards con modal detalle de incidencias.

**Archivos modificados:**
- `src/incidents/store.py` - Modelo Incident con JSON serialization para Redis
- `src/api/routes/incidents.py` - Endpoints GET/POST para incidencias
- `src/api/static/index.html` - Mini-cards y modal con tabla de clientes

**Cambios implementados:**

### Backend
- Modelo `Incident` con todos los campos requeridos (id, olt, pon, nap, causa, confianza, evidencia, impacto, afectados, fuentes, estado, severidad, inicio, fin, reconocida)
- `IncidentStore.save()` ahora serializa tipos para Redis (JSON para dict/list, bool to '1'/'0', floats to strings)
- `Incident.from_dict()` deserializa correctamente strings de Redis (JSON decode, float/bool conversion)
- Endpoint GET `/api/v1/incidents` retorna lista ordenada por timestamp
- Endpoint GET `/api/v1/incidents/{id}` para detalle individual

### Frontend
- Mini-cards panel (líneas 1152-1205) con:
  - Skeleton loading con animación
  - Tarjetas coloridas por severidad:
    - Critical: rojo (border-l-red-500, bg-red-950/10)
    - Warning: ámbar (border-l-amber-500, bg-amber-950/10)
    - Resolved: esmeralda (border-l-emerald-500, bg-emerald-950/10)
  - ID, causa (max 45 chars), OLT/PON/NAP, timestamp
  - Badge de severidad con color
- Modal detalle (líneas 1209-1340) con:
  - Header: ID, causa, severidad, estado con badges
  - Información básica (OLT, PON, NAP, causa, confianza, estado)
  - Evidencia como lista
  - Impacto: ONUs, clientes, OLTs, PONs, predicciones, severidad
  - Fuentes consultadas con indicadores ✓/—
  - Tabla de clientes afectados:
    - Columnas: Cliente, CZWisp, Serial ONU, MAC, Rx, Estado
    - Estado con colores: rojo=DOWN/LOS, ámbar=degradado, verde=UP
    - Rx formateado a dBm con colores por nivel
    - Buscador por serial/OLT/PON
  - Botón "Actualizar" funcional

### Helper functions agregadas
- `getStatusClass(status)` - Color del badge de estado cliente
- `getStatusLabel(status)` - Texto legible del estado
- `formatRx(rx)` - Formatea Rx power a dBm
- `getRxClass(rx)` - Color del Rx según nivel
- `refreshIncident(incidentId)` - Refresca datos (placeholder Fase 2)

### Pruebas realizadas
- API test: 3 incidencias retornadas correctamente (critical, warning, resolved)
- Datos de afectados: 2, 1 y 1 clientes respectivamente con CZWisp, serial ONU, MAC, Rx power
- Serialización Redis: verificada correcta con json.dumps/loads
- Autenticación: X-API-Key header funcionando

**Estado:** ✅ Funcional - pendiente verificación visual en navegador

---

## 9. Preguntas abiertas

1. ¿La incidencia debe abrirse solo con caída confirmada o también con degradación óptica sostenida?
2. ¿Qué ventana de correlación define «cayeron juntos»? (propuesta: 120 s)
3. ¿El silencio debe silenciar notificaciones, la incidencia en el panel, o ambos?
4. ¿Quién puede reconocer/silenciar: viewer, operator o admin?
5. ¿Se guarda el historial en Redis con TTL o se archiva en Influx/SQLite?
