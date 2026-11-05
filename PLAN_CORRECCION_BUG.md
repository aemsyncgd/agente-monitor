# Plan de Corrección — Bug `_recovery_info()` en fault_detector.py

**Basado estrictamente en el manual `incidencias_docs.md`**

---

## Estado actual: ✅ COMPLETADO

### Acción tomada
Se implementó el fix en `src/fault_detector.py` líneas 435-456.

### Verificación realizada
- Sintaxis Python: ✅ OK
- Test de parseo de claves: ✅ Todas las claves reales se parsean correctamente
  - 4 claves `fault:olt:*` → scope=`olt`, sin IndexError
  - 187 claves `fault:nap:*` → scope=`nap`, comportamiento conservado
  - Keys inválidas → fallback a `unknown` sin crash

---

## ¿Qué queda por hacer? (Según `incidencias_docs.md`)

El manual define **Fase 1 — Incidencia persistente + API + frecuencia**

### Archivos a modificar
- `src/fault_detector.py` ✅ (ya hecho - fix G11)
- `src/scheduler.py`
- `src/api/main.py`
- `src/models.py`
- `src/reporte/reporter.py`
- `src/client_enricher.py`
- `scripts/log_monitor.py`

### Tareas pendientes (F1.1 - F1.10)

| Tarea | Descripción | Estado |
|-------|-------------|--------|
| F1.1 | Definir el modelo `Incident` | [ ] |
| F1.2 | Persistir en Redis (hash + ZSET) | [ ] |
| F1.3 | Endpoints API de incidencias | [ ] |
| F1.4 | Abrir/cerrar incidencias desde FaultDetector | [ ] |
| F1.5 | Adjuntar afectados (clientes del NAP/PON) | [ ] |
| F1.6 | Unificar mapeo de `estado_onu` | [ ] |
| F1.7 | Enriquecer `device_type` en log_monitor | [ ] |
| F1.8 | Reducir intervalo detección a 60-120s | [ ] |
| F1.9 | Filtrar caídas antiguas sin recuperación | [ ] |
| F1.10 | Completar ficha clientes `olt_only` | [ ] |

---

## Próximo paso recomendado

Según el manual, **G11 está marcada como "Fase 1 (fix inmediato)"**. El fix está completo y verificado.

**El sistema ya no se detiene al procesar `fault:olt:*` keys.**

El detector puede continuar funcionando. La Fase 0 del frontend está lista para recibir datos reales cuando se complete la Fase 1.