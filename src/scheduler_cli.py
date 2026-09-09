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

    return WispScheduler(
        agent=None,
        reporter=reporter,
        rag_store=rag,
        memory=memory,
        config=config,
        audit=audit,
    )


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
