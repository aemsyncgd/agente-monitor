# src/scheduler.py
"""
Scheduler de reportes y mantenimiento del agente WISP.

Usa APScheduler con AsyncIOScheduler:
- Reporte diario a las 06:00.
- Reporte semanal los lunes a las 07:00.
- Reporte mensual el día 1 a las 08:00.
- Health check del agente cada 5 min.
- Re-indexado RAG semanal.
- Limpieza de memoria de largo plazo (retención configurable).
"""
import os
import time
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

# Higiene de scheduling: un disparo que cae dentro de una corrida larga se
# colapsa (coalesce) en vez de encolarse, y nunca corren dos instancias del
# mismo job a la vez. misfire_grace_time evita que APScheduler descarte
# disparos que se retrasaron menos de 1 minuto.
_JOB_KWARGS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 60,
}


def _phase_start(offset_minutes: int) -> datetime:
    """Ancla de fase: hoy a las 00:{offset}, en el pasado.

    IntervalTrigger alinea sus disparos al start_date: con interval=15 y
    ancla 00:04, los jobs corren a las :04, :19, :34 y :49 — desfasados de
    los demás jobs y de la hora redonda en la que suelen reiniciarse los
    servicios tras un reboot.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(minutes=offset_minutes)

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False


class WispScheduler:
    """Programador de tareas del sistema WISP."""

    def __init__(self, agent=None, reporter=None, rag_store=None,
                 memory=None, config=None, audit=None,
                 client_enricher=None, fault_detector=None):
        self.agent = agent
        self.reporter = reporter
        self.rag = rag_store
        self.memory = memory
        self.config = config
        self.audit = audit
        self.client_enricher = client_enricher
        self.fault_detector = fault_detector

        self.scheduler = AsyncIOScheduler() if APSCHEDULER_AVAILABLE else None
        self._enabled = False

    def configure(self) -> None:
        if not APSCHEDULER_AVAILABLE:
            logger.warning("APScheduler not installed — scheduling disabled")
            return

        if not self.config:
            logger.warning("No config provided for scheduler")
            return

        sc = self.config.scheduler

        self._add_daily_report(sc)
        self._add_weekly_report(sc)
        self._add_monthly_report(sc)
        self._add_health_check(sc)
        self._add_rag_reindex(sc)
        self._add_memory_cleanup(sc)
        self._add_enrichment(sc)
        self._add_fault_detection(sc)

        self._enabled = True
        logger.info("WispScheduler configured")

    def _add_daily_report(self, sc) -> None:
        hour, minute = self._parse_time(sc.daily_report_hour, "06:00")
        try:
            self.scheduler.add_job(
                self._run_daily_report,
                CronTrigger(hour=hour, minute=minute),
                id="daily_report",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Daily report scheduled at {sc.daily_report_hour}")
        except Exception as e:
            logger.error(f"Daily report scheduling failed: {e}")

    def _add_weekly_report(self, sc) -> None:
        hour, minute = self._parse_time(sc.weekly_report_hour, "07:00")
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
        }
        day = day_map.get(sc.weekly_report_day.lower(), 0)
        try:
            self.scheduler.add_job(
                self._run_weekly_report,
                CronTrigger(day_of_week=day, hour=hour, minute=minute),
                id="weekly_report",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Weekly report scheduled for {sc.weekly_report_day} at {sc.weekly_report_hour}")
        except Exception as e:
            logger.error(f"Weekly report scheduling failed: {e}")

    def _add_monthly_report(self, sc) -> None:
        hour, minute = self._parse_time(sc.monthly_report_hour, "08:00")
        try:
            self.scheduler.add_job(
                self._run_monthly_report,
                CronTrigger(day=sc.monthly_report_day, hour=hour, minute=minute),
                id="monthly_report",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(
                f"Monthly report scheduled on day {sc.monthly_report_day} at "
                f"{sc.monthly_report_hour}"
            )
        except Exception as e:
            logger.error(f"Monthly report scheduling failed: {e}")

    def _add_health_check(self, sc) -> None:
        interval = max(1, sc.health_check_interval_minutes)
        try:
            self.scheduler.add_job(
                self._run_health_check,
                IntervalTrigger(minutes=interval, start_date=_phase_start(0)),
                id="health_check",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Health check scheduled every {interval} minutes")
        except Exception as e:
            logger.error(f"Health check scheduling failed: {e}")

    def _add_rag_reindex(self, sc) -> None:
        interval_hours = sc.rag_reindex_interval_hours
        try:
            self.scheduler.add_job(
                self._run_rag_reindex,
                IntervalTrigger(hours=interval_hours, start_date=_phase_start(30)),
                id="rag_reindex",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"RAG reindex scheduled every {interval_hours} hours")
        except Exception as e:
            logger.error(f"RAG reindex scheduling failed: {e}")

    def _add_memory_cleanup(self, sc) -> None:
        interval_hours = sc.memory_cleanup_interval_hours
        try:
            self.scheduler.add_job(
                self._run_memory_cleanup,
                IntervalTrigger(hours=interval_hours, start_date=_phase_start(45)),
                id="memory_cleanup",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Memory cleanup scheduled every {interval_hours} hours")
        except Exception as e:
            logger.error(f"Memory cleanup scheduling failed: {e}")

    # ---- Jobs ----

    def _add_enrichment(self, sc: Any) -> None:
        if not self.client_enricher:
            return
        interval = self.config.enrichment.interval_minutes
        try:
            self.scheduler.add_job(
                self._run_enrichment,
                IntervalTrigger(minutes=interval, start_date=_phase_start(4)),
                id="enrichment",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Enrichment scheduled every {interval} minutes")
        except Exception as e:
            logger.error(f"Enrichment scheduling failed: {e}")

    def _add_fault_detection(self, sc: Any) -> None:
        if not self.fault_detector:
            return
        interval = self.config.fault_detection.interval_minutes
        try:
            self.scheduler.add_job(
                self._run_fault_detection,
                IntervalTrigger(minutes=interval, start_date=_phase_start(9)),
                id="fault_detection",
                replace_existing=True,
                **_JOB_KWARGS,
            )
            logger.info(f"Fault detection scheduled every {interval} minutes")
        except Exception as e:
            logger.error(f"Fault detection scheduling failed: {e}")

    def _run_daily_report(self) -> None:
        logger.info("Running daily report job")
        if self.reporter:
            try:
                self.reporter.run()
                self._log_success("daily_report")
            except Exception as e:
                logger.error(f"Daily report failed: {e}")
                self._log_failure("daily_report", str(e))

    def _run_weekly_report(self) -> None:
        logger.info("Running weekly report job")
        if self.reporter:
            try:
                self.reporter.run(period="weekly")
                self._log_success("weekly_report")
            except Exception as e:
                logger.error(f"Weekly report failed: {e}")
                self._log_failure("weekly_report", str(e))

    def _run_monthly_report(self) -> None:
        logger.info("Running monthly report job")
        if self.reporter:
            try:
                self.reporter.run(period="monthly")
                self._log_success("monthly_report")
            except Exception as e:
                logger.error(f"Monthly report failed: {e}")
                self._log_failure("monthly_report", str(e))

    def _run_health_check(self) -> None:
        if not self.agent:
            return

        health = self.agent.get_health()
        logger.info(f"Health check: {health}")

        if health.get("status") == "degraded":
            logger.warning("Agent in degraded mode")

    def _run_rag_reindex(self) -> None:
        if not self.rag:
            return
        try:
            if self.rag.ready:
                logger.info("Running RAG reindex")
                docs_dir = self.config.rag.docs_directory if self.config else "docs/"
                self.rag.index_directory(docs_dir)
                self._log_success("rag_reindex")
        except Exception as e:
            logger.error(f"RAG reindex failed: {e}")
            self._log_failure("rag_reindex", str(e))

    def _run_memory_cleanup(self) -> None:
        if not self.memory:
            return
        try:
            removed = self.memory.cleanup(
                self.config.audit.retention_days if self.config else 90
            )
            logger.info(f"Memory cleanup: removed {removed} files")
            self._log_success("memory_cleanup")
        except Exception as e:
            logger.error(f"Memory cleanup failed: {e}")
            self._log_failure("memory_cleanup", str(e))

    def _run_enrichment(self) -> None:
        if not self.client_enricher:
            return
        try:
            stats = self.client_enricher.run()
            logger.info(
                f"Enrichment job: {stats.matched} enriquecidos, "
                f"{stats.pending} pendientes, {stats.mismatched} descuadrados"
            )
            self._rebuild_client_store()
            self._log_success("enrichment")
        except Exception as e:
            logger.error(f"Enrichment failed: {e}")
            self._log_failure("enrichment", str(e))

    def _rebuild_client_store(self) -> None:
        """Refresca el cache Redis de fichas (no debe romper el job si falla)."""
        try:
            from .redis_store import get_client_store
            influx = getattr(self.client_enricher, "influx", None)
            if influx is None:
                return
            get_client_store().rebuild_from_influx(influx)
        except Exception as e:
            logger.warning(f"ClientStore rebuild failed: {e}")

    def _run_fault_detection(self) -> None:
        if not self.fault_detector:
            return
        try:
            stats = self.fault_detector.run()
            logger.info(
                f"Fault detection job: {stats.clients} clientes, "
                f"{stats.nap_candidates} NAPs candidatas, "
                f"{stats.broadcast_olts} OLTs broadcast"
            )
            self._log_success("fault_detection")
        except Exception as e:
            logger.error(f"Fault detection failed: {e}")
            self._log_failure("fault_detection", str(e))

    # ---- Control ----

    def start(self) -> None:
        if self.scheduler and not self.scheduler.running:
            self.scheduler.start()
            logger.info("WispScheduler started")

    def stop(self) -> None:
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("WispScheduler stopped")

    def get_jobs(self) -> List[Dict]:
        if not self.scheduler:
            return []
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "pending": job.pending,
            }
            for job in self.scheduler.get_jobs()
        ]

    def is_running(self) -> bool:
        return bool(self.scheduler and self.scheduler.running)

    # ---- Helpers ----

    def _log_success(self, job_name: str) -> None:
        if self.audit:
            self.audit.log(
                actor="scheduler", action=f"job:{job_name}",
                result="success",
            )

    def _log_failure(self, job_name: str, error: str) -> None:
        if self.audit:
            self.audit.log(
                actor="scheduler", action=f"job:{job_name}",
                result="error", details=error,
            )

    @staticmethod
    def _parse_time(time_str: str, default: str) -> tuple:
        try:
            parts = time_str.split(":")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            parts = default.split(":")
            return int(parts[0]), int(parts[1])
