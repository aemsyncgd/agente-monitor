# Pruebas de Mini-Tarjetas de Incidencias

## Estado: IMPLEMENTADO ✅

### Archivos modificados

1. **src/incidents/store.py**
   - Modelo `Incident` con todos los campos requeridos
   - `IncidentStore.save()` ahora serializa tipos para Redis (JSON para dict/list, bool to '1'/'0', floats to strings)
   - `Incident.from_dict()` deserializa correctamente strings de Redis (JSON decode, float/bool conversion)

2. **src/api/routes/incidents.py**
   - Endpoint GET `/api/v1/incidents` (con paginación)
   - Endpoint GET `/api/v1/incidents/{id}`
   - Endpoint POST para acciones (ack, resolve)
   - Autenticación con `X-API-Key` header

3. **src/api/main.py**
   - Redis: misma base que el resto del sistema (DB 0, `REDIS_URL` de `.env.ia`)
   - Carga automática de `.env.ia`/`.env` al arrancar (token de InfluxDB y API keys)
   - Router registration

4. **src/api/static/index.html**
   - Mini-cards panel (líneas 1152-1205)
   - Modal detalle incidencia con tabla de clientes (líneas 1209-1340)
   - Funciones Vue.js: `fetchIncidents()`, `openIncidentModal()`, `closeIncidentModal()`, `refreshIncident()`
   - Helper functions: `severityColorClass()`, `severityBadgeClass()`, `severityLabel()`, `getStatusClass()`, `getStatusLabel()`, `formatRx()`, `getRxClass()`
   - CSS skeleton loading
   - Botón "Actualizar" funcional

## Pruebas manuales

### Paso 1: Iniciar Redis
```bash
redis-cli ping  # Debería retornar "PONG"
```

### Paso 2: Verificar datos de prueba
```bash
redis-cli -n 0 HGETALL inc:INC-2026-11-05-0001
```

### Paso 3: Probar API
```bash
# Usar la clave de Admin desde docs/apis.md
curl -H "X-API-Key: DrSdDObyj-uCoFv3HG9zaqxJkwoc-giuaIhxY6w29WE" http://localhost:8000/api/v1/incidents/ | jq
```

### Paso 4: Verificar frontend
- Navegar a `http://localhost:8000/`
- Hacer click en pestaña "Incidencias"
- Ver mini-cards con colores:
  - Rojo (border-l-red-500) para crítica
  - Ámbar (border-l-amber-500) para warning
  - Esmeralda (border-l-emerald-500) para resuelto
- Click en cualquier card → abre modal con:
  - Header con ID, causa, severidad
  - Estado, confianza, timestamps
  - Evidencia
  - Tabla de clientes con columnas:
    - Cliente, CZWisp, Serial ONU, MAC, Rx, Estado
  - Botón "Actualizar"

## Correcciones tras prueba E2E (2026-11-06)

La prueba en navegador real destapó tres fallos de frontend que las pruebas de API no podían ver. Todos
están corregidos en `src/api/static/index.html`.

| # | Síntoma | Causa | Corrección |
|---|---|---|---|
| 1 | Al abrir la pestaña Incidencias, **todo el dashboard quedaba en blanco** (`#app` = `<!---->`) | La plantilla usaba `incidentCardsDisplay`, `loadingIncidents`, `openIncidentModal`, `severityColorClass`, `severityBadgeClass`, `severityLabel`, `getStatusClass`, `getStatusLabel`, `formatRx`, `getRxClass`, `selectedIncident`, `showIncidentModal`, `fetchIncidents`, `refreshIncident` y `formatIncidentTimeSimple`, pero ninguno estaba en el `return` de `setup()`; Vue abortaba el render | Se expusieron todos los símbolos en el `return` de `setup()` |
| 2 | El skeleton de carga quedaba infinito, aunque el servidor registraba `200` | `fetchIncidents()` llamaba `/api/v1/incidents?limit=20`; la ruta está declarada como `"/"`, así que FastAPI responde `307` a `/api/v1/incidents/?limit=20` y el `fetch` del navegador quedaba pendiente | Se llama directamente a `/api/v1/incidents/?limit=20`, sin redirect |
| 3 | Fechas con año `58818` (`30/10/58818`) | `fetchIncidents()` ya convierte `inicio`/`fin` de segundos a milisegundos (`* 1000`), pero `formatIncidentTimeSimple()` volvía a multiplicar por 1000 | `new Date(Number(ts))` (los valores ya llegan en ms) + guarda de fecha inválida |

**Botón "Actualizar":** ya no es el placeholder de Fase 2. `refreshIncident()` vuelve a pedir la lista,
busca la incidencia por `id` y actualiza `selectedIncident`, de modo que el modal abierto se refresca de
verdad; además muestra un toast de confirmación.

**Estado verificado (E2E, datos reales):**

| Comprobación | Resultado |
|---|---|
| OLTs / ONUs / interfaces | 11 / 2.885 (74,1%) / 3.035 |
| MikroTik / nodos | 13 (7 con datos de sistema) / 7 |
| Mini-cards | 3: CRÍTICA (rojo), ADVERTENCIA (ámbar), RESUELTA (esmeralda) |
| Modal | abre con OLT/PON/NAP, evidencia, confianza y tabla de 2 clientes (Rx/estado) |
| "Actualizar" | refresca la incidencia abierta |
| Consola JS | sin errores |

> **Nota de acceso:** el dashboard principal sigue exigiendo `X-API-Key`. Si el navegador conserva una
> clave vieja en `localStorage.api_key`, verá `401` y paneles vacíos aunque el API esté sano. Recargar y
> reingresar la clave Admin de `docs/apis.md`.

## Verificación de campos

### Mini-cards (línea 1152)
- ✅ ID de incidencia
- ✅ Causa (max 45 chars)
- ✅ OLT / PON / NAP
- ✅ Timestamp de inicio
- ✅ Severidad con badge color

### Modal Cliente (línea 1209)
- ✅ Header: ID, causa, severidad, estado
- ✅ Información básica (OLT, PON, NAP, causa, confianza)
- ✅ Evidencia (lista)
- ✅ Impacto (clientes, ONUs, etc.)
- ✅ Tabla de clientes:
  - ✅ Cliente (con fallback a name)
  - ✅ CZWisp
  - ✅ Serial ONU
  - ✅ MAC
  - ✅ Rx (formateado a dBm)
  - ✅ Estado (con color: rojo=DOWN/LOS, ámbar=degradado, verde=UP)
- ✅ Botón "Actualizar" (Fase 2)

## Colores de severidad
| Severidad | Border Left | Background | Badge |
|-----------|-------------|------------|-------|
| critical  | red-500     | red-950/10 | red badge |
| warning   | amber-500   | amber-950/10 | amber badge |
| resolved  | emerald-500 | emerald-950/10 | emerald badge |

## Estados de cliente (en tabla)
| Estado | Color | Badge |
|--------|-------|-------|
| DOWN / LOS | Rojo | bg-red-500/20 |
| DEGRADED / WARNING | Ámbar | bg-amber-500/20 |
| UP / OK | Verde | bg-emerald-500/20 |

---

## API Response Example

```json
{
  "incidents": [
    {
      "id": "INC-2026-11-05-0001",
      "olt": "OLT-SISAL2-PON2",
      "pon": "GPON0/9",
      "nap": "SISAL2",
      "causa": "Pérdida de energía en rack principal",
      "confianza": 85.0,
      "estado": "open",
      "severidad": "critical",
      "inicio": 1730784000.0,
      "detectado": 1730783900.0,
      "fin": null,
      "evidencia": [...],
      "impacto": {"clientes": 82, "onus_los": 11, "predicciones": 0},
      "afectados": [
        {
          "cliente": "CLIENTE-001",
          "czwisp": "CZW-12345",
          "serial_onu": "ONU-ABC123",
          "mac": "00:11:22:33:44:55",
          "rx_power": "-5.2",
          "estatus": "DOWN"
        }
      ],
      "fuentes": {"anomalies": true, "predictions": true, "syslog": false},
      "reconocida": false
    }
  ]
}
```