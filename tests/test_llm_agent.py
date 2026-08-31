# tests/test_llm_agent.py
"""
Unit and integration tests for the LLM Agent module.
"""
import os
import json
import pytest
from unittest.mock import MagicMock, patch

from src.config_ia import LLMConfig, load_config_ia
from src.ai.llm_agent.config import RuntimeConfigStore, LLMRuntimeConfig
from src.ai.llm_agent.providers import OpenRouterProvider, OllamaProvider, create_provider, LLMResult, LLMToolCall
from src.ai.llm_agent.tools import ToolRegistry, InfluxReader
from src.ai.llm_agent.memory import EventStore, AlertState, CostTracker
from src.ai.llm_agent.agent import LLMAgent


# --- 1. Config Tests ---

def test_llm_config_dataclass():
    cfg = LLMConfig(enabled=True)
    assert cfg.enabled is True
    assert cfg.mode == "hybrid"
    assert cfg.provider == "ollama"
    assert cfg.model == "qwen2.5:3b-instruct"
    assert cfg.temperature == 0.0
    assert cfg.keep_alive == "5m"
    assert cfg.num_ctx == 4096
    assert cfg.num_thread == 4
    d = cfg.to_dict()
    assert d["enabled"] is True
    assert d["mode"] == "hybrid"
    assert d["keep_alive"] == "5m"


def test_runtime_config_store(tmp_path):
    base = LLMRuntimeConfig(mode="autonomous", check_interval_seconds=300,
                            runtime_config_file=str(tmp_path / "llm_config.json"))

    store = RuntimeConfigStore(base)
    eff = store.effective()
    assert eff.mode == "autonomous"

    # Update override
    store.update({"mode": "hybrid", "check_interval_seconds": 120})
    eff2 = store.effective()
    assert eff2.mode == "hybrid"
    assert eff2.check_interval_seconds == 120

    # Ensure JSON file was saved
    json_file = str(tmp_path / "llm_config.json")
    assert os.path.exists(json_file)
    with open(json_file, "r") as f:
        data = json.load(f)
    assert data["mode"] == "hybrid"


# --- 2. Provider Tests ---

def test_provider_factory():
    p1 = create_provider(provider_name="openrouter", model="test-model", api_key="sk-test")
    assert isinstance(p1, OpenRouterProvider)
    assert p1.model == "test-model"

    p2 = create_provider(provider_name="ollama", model="qwen2.5:3b", ollama_url="http://localhost:11434")
    assert isinstance(p2, OllamaProvider)
    assert p2.model == "qwen2.5:3b"


@patch("requests.post")
def test_openrouter_chat_text(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "choices": [{
            "message": {"role": "assistant", "content": "Todo en orden en la red."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "model": "mistralai/mistral-7b-instruct",
    }

    provider = OpenRouterProvider(base_url="https://openrouter.ai/api/v1",
                                  api_key="dummy_key",
                                  model="mistralai/mistral-7b-instruct")
    res = provider.chat([{"role": "user", "content": "hola"}])
    assert res.ok is True
    assert res.text == "Todo en orden en la red."
    assert res.prompt_tokens == 10
    assert res.completion_tokens == 5


@patch("requests.post")
def test_openrouter_chat_tool_call(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_network_summary",
                        "arguments": "{}"
                    }
                }]
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 15, "completion_tokens": 10},
    }

    provider = OpenRouterProvider(base_url="https://openrouter.ai/api/v1", api_key="dummy_key")
    res = provider.chat([{"role": "user", "content": "estado"}], tools=[{"type": "function", "function": {"name": "get_network_summary"}}])
    assert res.ok is True
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "get_network_summary"


# --- 3. Memory & Deduplication Tests ---

def test_event_store(tmp_path):
    fpath = str(tmp_path / "llm_events.json")
    store = EventStore(fpath)
    assert store.count() == 0

    store.add(kind="alert", title="Alerta ONU", detail="ONU-01 fuera de linea", severity="critical")
    assert store.count() == 1

    events = store.recent(limit=10)
    assert len(events) == 1
    assert events[0]["title"] == "Alerta ONU"


def test_alert_state(tmp_path):
    fpath = str(tmp_path / "llm_state.json")
    state = AlertState(fpath)

    key = "test_alert_key"
    assert state.should_alert(key, cooldown_seconds=60) is True

    # Immediate second check should fail (cooldown active)
    assert state.should_alert(key, cooldown_seconds=60) is False


def test_cost_tracker(tmp_path):
    fpath = str(tmp_path / "llm_cost.json")
    tracker = CostTracker(fpath, daily_budget_usd=1.0)
    assert tracker.under_budget() is True

    tracker.record(prompt_tokens=1000, completion_tokens=500)
    stats = tracker.stats()
    assert stats["tokens_in"] == 1000
    assert stats["tokens_out"] == 500
    assert stats["estimate_usd"] > 0


# --- 4. Tool Registry Tests ---

def test_tool_registry():
    reader_mock = MagicMock()
    reader_mock.overall_stats.return_value = {"olts": 2, "onus_online": 100, "onus_offline": 5, "low_power_alerts": 1, "hosts_down": 0}

    events = EventStore("")
    registry = ToolRegistry(reader=reader_mock, client_lookup=MagicMock(),
                            event_store=events, alert_sender=None)

    tools, dispatch = registry.build(enabled_instruction_ids=["daily_summary", "send_telegram"])
    tool_names = [t["function"]["name"] for t in tools]
    assert "get_network_summary" in tool_names
    assert "send_alert" in tool_names
    assert "get_network_summary" in dispatch


# --- 5. LLMAgent Execution Tests ---

def test_agent_run_cycle(tmp_path):
    base = LLMRuntimeConfig(enabled=True,
                            runtime_config_file=str(tmp_path / "llm_config.json"))
    store = RuntimeConfigStore(base)

    provider_mock = MagicMock()
    provider_mock.api_key = "sk-dummy"
    provider_mock.chat.return_value = LLMResult(
        ok=True,
        text="Resumen: La red funciona normalmente.",
        usage={"prompt_tokens": 20, "completion_tokens": 10},
    )

    reader_mock = MagicMock()
    reader_mock.overall_stats.return_value = {"olts": 2, "onus_online": 100, "onus_total": 105, "onus_offline": 5, "low_power_alerts": 1, "hosts_down": 0}

    events = EventStore(str(tmp_path / "events.json"))
    registry = ToolRegistry(reader=reader_mock, client_lookup=MagicMock(),
                            event_store=events, alert_sender=None)
    alert_state = AlertState(str(tmp_path / "state.json"))
    cost = CostTracker(str(tmp_path / "cost.json"))

    agent = LLMAgent(store, provider_mock, registry, events, alert_state, cost)

    res = agent.run_cycle(reason="test")
    assert "normalmente" in res
    assert events.count() == 1
