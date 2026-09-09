# src/scheduler_cli.py
"""
CLI entry point para el WispScheduler como servicio independiente.
"""
import os
import sys
import signal
import asyncio
import logging
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .config_ia import load_config_ia
from .scheduler import WispScheduler
from .ai.rag import get_rag_store
from .ai.memory_ext import WispMemory
from .audit import get_audit_logger

logger = logging.getLogger(__name__)


def _build_scheduler(config):
    rag = get_rag_store() if config.rag.enabled else None
    memory = WispMemory(redis_url=config.redis.url)
    audit = get_audit_logger()

    # reporter se conecta on-demand (lazy) para no requerir InfluxDB al inicio
    reporter = None
    try:
        from .reporte.reporter import DailyReporter
        from .storage.influx_client import InfluxClient
        influx = InfluxClient(
            url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
            token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
            org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
            bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
        )
        influx.connect()
        reporter = DailyReporter(
            influx=influx,
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id),
        )
    except Exception as e:
        logger.warning(f"Reporter unavailable: {e}")

    client_enricher, fault_detector = _build_analytics(config, influx=None)

    return WispScheduler(
        agent=None,
        reporter=reporter,
        rag_store=rag,
        memory=memory,
        config=config,
        audit=audit,
        client_enricher=client_enricher,
        fault_detector=fault_detector,
    )


def _build_analytics(config, influx=None):
    """Construye (client_enricher, fault_detector) en modo lazy (sin red)."""
    client_enricher, fault_detector = None, None
    if not (config.enrichment.enabled or config.fault_detection.enabled):
        return client_enricher, fault_detector
    try:
        from .client_enricher import ClientEnricher
        from .fault_detector import FaultDetector, OltStatusRefresher
        from .notifier_ai import TelegramNotifierAI, TelegramConfig
        from .storage.influx_client import InfluxClient
        from .wisp_client import WispClient

        if influx is None:
            influx = InfluxClient(
                url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
                token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
                org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
                bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
            )
            influx.connect()

        if config.enrichment.enabled:
            wisp = WispClient(config.wisp)
            client_enricher = ClientEnricher(
                influx=influx,
                wisp=wisp,
                config=config.enrichment,
            )

        if config.fault_detection.enabled:
            notifier = TelegramNotifierAI(
                TelegramConfig(
                    bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id),
                    cooldown_minutes=config.telegram.cooldown_minutes,
                    state_file=config.fault_detection.state_file,
                )
            )
            from .config_ia import load_nodes_config
            nodes = config.nodes or load_nodes_config(
                os.environ.get("NODES_PATH", "config/nodes.yaml")
            )
            olt_lookup = {
                olt.hostname: (olt.ip, olt.community)
                for node in nodes
                for olt in node.olts
            }
            fault_detector = FaultDetector(
                influx=influx,
                notifier=notifier,
                config=config.fault_detection,
                olt_lookup=olt_lookup,
                refresher=OltStatusRefresher(config.fault_detection.refresh_timeout_seconds),
            )
    except Exception as e:
        logger.warning(f"Analytics unavailable (enricher/fault detector): {e}")

    return client_enricher, fault_detector


async def _run() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    config = load_config_ia(config_path)

    scheduler = _build_scheduler(config)
    scheduler.configure()

    running = asyncio.Event()

    def request_stop(signum=None, frame=None):
        logger.info(f"Signal {getattr(signum, 'name', signum)}, stopping scheduler...")
        try:
            scheduler.stop()
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")
        running.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, request_stop)

    scheduler.start()
    logger.info("Scheduler running. Jobs:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job['id']} (next: {job['next_run']})")

    await running.wait()
    logger.info("Scheduler stopped")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
