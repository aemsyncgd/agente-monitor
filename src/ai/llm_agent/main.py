# src/ai/llm_agent/main.py
"""
Entry point for the standalone LLM Agent background service.
"""
import os
import sys
import time
import signal
import logging
from typing import Optional

from src.config_ia import load_config_ia
from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
from src.ai.llm_agent.providers import create_provider
from src.ai.llm_agent.tools import ToolRegistry, InfluxReader
from src.ai.llm_agent.memory import EventStore, AlertState, CostTracker
from src.ai.llm_agent.agent import LLMAgent
from src.storage.influx_client import InfluxClient

logger = logging.getLogger("llm_agent")

_running = True
_agent_instance: Optional[LLMAgent] = None


def signal_handler(signum, frame):
    global _running
    logger.info(f"Received signal {signum}, stopping LLM Agent...")
    _running = False


def get_llm_agent() -> Optional[LLMAgent]:
    """Helper for API routes running in the same process or worker."""
    return _agent_instance


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def create_agent(config_yaml_path: str = "config/config_ia.yaml",
                 overrides_json_path: str = "config/llm_config.json") -> LLMAgent:
    yaml_config = load_config_ia(config_yaml_path)
    runtime = build_default_from_llm_config(yaml_config.llm, project_root())
    runtime.runtime_config_file = overrides_json_path
    store = RuntimeConfigStore(runtime)

    eff = store.effective()
    provider = create_provider(config=eff)

    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", "http://localhost:8086"),
        token=os.environ.get("INFLUXDB_TOKEN", ""),
        org=os.environ.get("INFLUXDB_ORG", "vidanet"),
        bucket=os.environ.get("INFLUXDB_BUCKET", "monitoreo"),
    )
    influx.connect()
    registry = ToolRegistry(
        InfluxReader(influx),
        _client_lookup(),
        EventStore(eff.events_file),
        alert_sender=None,
    )
    events = EventStore(eff.events_file)
    alert_state = AlertState(eff.state_file)
    cost_tracker = CostTracker(
        eff.state_file.replace("llm_state.json", "llm_cost.json"),
        daily_budget_usd=eff.daily_budget_usd,
    )

    agent = LLMAgent(
        config_store=store,
        provider=provider,
        registry=registry,
        events=events,
        alert_state=alert_state,
        cost_tracker=cost_tracker,
    )
    return agent


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _client_lookup():
    from src.client_lookup import ClientLookup
    lookup = ClientLookup()
    root = project_root()
    lookup.load_from_csv(os.path.join(root, "grid_servicios.csv"))
    return lookup


def main():
    global _agent_instance, _running
    setup_logging()
    logger.info("Starting LLM Agent Service...")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        _agent_instance = create_agent()
    except Exception as e:
        logger.critical(f"Failed to initialize LLM Agent: {e}", exc_info=True)
        sys.exit(1)

    logger.info(f"LLM Agent initialized. Provider={_agent_instance.provider.name}, Mode={_agent_instance.config_store.effective().mode}")

    while _running:
        try:
            eff = _agent_instance.config_store.effective()
            if _agent_instance.config_store.changed():
                logger.info("Config change detected, reloading...")
                _agent_instance.config_store.reload()
                eff = _agent_instance.config_store.effective()
                # Re-instantiate provider if changed
                _agent_instance.provider = create_provider(config=eff)

            if not eff.enabled:
                logger.debug("LLM Agent is disabled. Sleeping...")
                time.sleep(10)
                continue

            if eff.mode == "autonomous":
                logger.info("Running autonomous cycle...")
                _agent_instance.run_cycle(reason="autonomous")
                sleep_time = max(30, eff.check_interval_seconds)
                for _ in range(sleep_time):
                    if not _running:
                        break
                    time.sleep(1)
            elif eff.mode == "hybrid":
                logger.info("Running hybrid cycle...")
                _agent_instance.run_cycle(reason="hybrid")
                sleep_time = max(30, eff.check_interval_seconds)
                for _ in range(sleep_time):
                    if not _running:
                        break
                    time.sleep(1)
            elif eff.mode == "analyst":
                now = time.strftime("%H:%M")
                if now == eff.analyst_report_hour:
                    logger.info("Running daily analyst report...")
                    _agent_instance.run_cycle(reason="analyst_scheduled")
                time.sleep(10)
            else:
                logger.warning(f"Unknown mode '{eff.mode}'. Defaulting to autonomous.")
                _agent_instance.run_cycle(reason="autonomous")
                time.sleep(60)

        except Exception as e:
            logger.error(f"Error in LLM Agent loop: {e}", exc_info=True)
            time.sleep(10)

    logger.info("LLM Agent Service stopped cleanly.")


if __name__ == "__main__":
    main()
