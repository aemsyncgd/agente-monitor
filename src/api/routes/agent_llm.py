# src/api/routes/agent_llm.py
"""
API routes for the LLM Agent.

Provides endpoints for:
- Status and configuration
- On-demand chat / analysis
- Model listing and connection test
- Event log for dashboard
"""
import os
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.config_manager import get_config_manager

logger = logging.getLogger(__name__)
router = APIRouter()

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# Lazy-loaded singleton for agent status
_llm_status = {
    "running": False,
    "mode": "autonomous",
    "provider": "openrouter",
    "model": "",
    "last_cycle": 0,
    "last_cycle_result": "",
    "last_cycle_tools": 0,
    "pid": None,
}


def _check_process():
    """Check if llm_agent process is running."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "src.ai.llm_agent.main"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, int(result.stdout.strip().split()[0])
    except Exception:
        pass
    return False, None


class LLMConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    check_interval_seconds: Optional[int] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    daily_budget_usd: Optional[float] = None
    analyst_report_hour: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled_tools: Optional[List[str]] = None
    keep_alive: Optional[str] = None
    num_ctx: Optional[int] = None
    num_thread: Optional[int] = None


class ChatRequest(BaseModel):
    question: str
    history: Optional[List[Dict[str, str]]] = None


@router.get("/api/agent/llm/status")
async def get_llm_status():
    """Get LLM agent status."""
    running, pid = _check_process()
    _llm_status["running"] = running
    _llm_status["pid"] = pid

    cm = get_config_manager()
    cfg = cm.config
    llm_cfg = cfg.llm

    # Merge with runtime overrides if file exists
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    project_root = PROJECT_ROOT
    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)
    effective = store.effective()

    return {
        "running": running,
        "pid": pid,
        "mode": effective.mode,
        "provider": effective.provider,
        "model": effective.model or llm_cfg.model,
        "check_interval_seconds": effective.check_interval_seconds,
        "max_tokens": effective.max_tokens,
        "temperature": effective.temperature,
        "daily_budget_usd": effective.daily_budget_usd,
        "keep_alive": effective.keep_alive,
        "num_ctx": effective.num_ctx,
        "num_thread": effective.num_thread,
        "system_prompt": effective.system_prompt,
        "last_cycle": _llm_status["last_cycle"],
        "last_cycle_result": _llm_status["last_cycle_result"],
        "last_cycle_tools": _llm_status["last_cycle_tools"],
        "cost": _get_cost_stats(effective),
        "events_count": _get_events_count(effective),
    }


def _get_cost_stats(config) -> Dict:
    from src.ai.llm_agent.memory import CostTracker
    tracker = CostTracker(
        config.state_file.replace("llm_state.json", "llm_cost.json"),
        daily_budget_usd=config.daily_budget_usd,
    )
    return tracker.stats()


def _get_events_count(config) -> int:
    from src.ai.llm_agent.memory import EventStore
    store = EventStore(config.events_file)
    return store.count()


@router.get("/api/agent/llm/config")
async def get_llm_config():
    """Get effective LLM configuration (yaml defaults + runtime overrides)."""
    cm = get_config_manager()
    llm_cfg = cm.config.llm

    project_root = PROJECT_ROOT
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)
    effective = store.effective()

    return {
        "yaml_defaults": llm_cfg.to_dict(),
        "runtime_overrides": store._overrides,
        "effective": effective.to_dict(),
    }


@router.put("/api/agent/llm/config")
async def update_llm_config(body: LLMConfigUpdate):
    """Update LLM runtime configuration (persists to llm_config.json)."""
    cm = get_config_manager()
    llm_cfg = cm.config.llm

    project_root = PROJECT_ROOT
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    effective = store.update(updates)

    logger.info(f"LLM config updated: {updates}")
    return {"ok": True, "config": effective.to_dict()}


@router.post("/api/agent/llm/test")
async def test_llm_connection():
    """Test connection to the LLM provider."""
    cm = get_config_manager()
    llm_cfg = cm.config.llm

    project_root = PROJECT_ROOT
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    from src.ai.llm_agent.providers import create_provider

    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)
    effective = store.effective()

    provider = create_provider(effective)
    ok = provider.test()

    return {"ok": ok, "provider": effective.provider, "model": effective.model}


# Lazy-loaded chat agent singleton (initialized once per process)
_chat_agent = None
_chat_agent_mtime: float = 0


def _get_chat_agent():
    """Return a LLMAgent configured for on-demand chat from the API process."""
    global _chat_agent, _chat_agent_mtime

    cm = get_config_manager()
    llm_cfg = cm.config.llm

    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    from src.ai.llm_agent.providers import create_provider
    from src.ai.llm_agent.tools import ToolRegistry, InfluxReader
    from src.ai.llm_agent.memory import EventStore, AlertState, CostTracker
    from src.ai.llm_agent.agent import LLMAgent
    from src.storage.influx_client import InfluxClient

    runtime_base = build_default_from_llm_config(llm_cfg, PROJECT_ROOT)
    store = RuntimeConfigStore(runtime_base)
    eff = store.effective()

    # Check if config changed — recreate provider only
    config_mtime = os.path.getmtime(store.path) if os.path.exists(store.path) else 0
    if _chat_agent is not None and config_mtime == _chat_agent_mtime:
        return _chat_agent

    provider = create_provider(config=eff)

    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", "http://localhost:8086"),
        token=os.environ.get("INFLUXDB_TOKEN", ""),
        org=os.environ.get("INFLUXDB_ORG", "vidanet"),
        bucket=os.environ.get("INFLUXDB_BUCKET", "monitoreo"),
    )
    influx.connect()

    from src.client_lookup import ClientLookup
    lookup = ClientLookup()
    csv_path = os.path.join(PROJECT_ROOT, "grid_servicios.csv")
    if os.path.exists(csv_path):
        lookup.load_from_csv(csv_path)

    events = EventStore(eff.events_file)
    registry = ToolRegistry(
        InfluxReader(influx),
        lookup,
        events,
        alert_sender=None,
    )
    alert_state = AlertState(eff.state_file)
    cost_tracker = CostTracker(
        eff.state_file.replace("llm_state.json", "llm_cost.json"),
        daily_budget_usd=eff.daily_budget_usd,
    )

    _chat_agent = LLMAgent(
        config_store=store,
        provider=provider,
        registry=registry,
        events=events,
        alert_state=alert_state,
        cost_tracker=cost_tracker,
    )
    _chat_agent_mtime = config_mtime
    logger.info(f"Chat agent initialized: provider={eff.provider}, model={eff.model}")
    return _chat_agent


@router.post("/api/agent/llm/chat")
async def llm_chat(body: ChatRequest):
    """On-demand chat/analysis with the LLM agent (uses LLMAgent.chat with tools)."""
    import asyncio
    try:
        def _run_chat():
            agent = _get_chat_agent()
            return agent.chat(body.question, body.history)
        result = await asyncio.get_event_loop().run_in_executor(None, _run_chat)
        return result
    except Exception as e:
        logger.error(f"LLM chat error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@router.get("/api/agent/llm/events")
async def get_llm_events(limit: int = 20, since_ts: float = 0):
    """Get recent LLM agent events (alerts/analyses)."""
    cm = get_config_manager()
    llm_cfg = cm.config.llm

    project_root = PROJECT_ROOT
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    from src.ai.llm_agent.memory import EventStore

    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)
    effective = store.effective()

    event_store = EventStore(effective.events_file)
    items = event_store.recent(limit=limit, since_ts=since_ts)

    return {"count": len(items), "events": items}


@router.get("/api/agent/llm/models")
async def list_llm_models():
    """List available models from the configured provider."""
    cm = get_config_manager()
    llm_cfg = cm.config.llm

    project_root = PROJECT_ROOT
    from src.ai.llm_agent.config import RuntimeConfigStore, build_default_from_llm_config
    from src.ai.llm_agent.providers import create_provider

    runtime_base = build_default_from_llm_config(llm_cfg, project_root)
    store = RuntimeConfigStore(runtime_base)
    effective = store.effective()

    provider = create_provider(effective)
    models = provider.list_models()

    return {"provider": effective.provider, "models": models}