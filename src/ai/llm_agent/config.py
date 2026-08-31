# src/ai/llm_agent/config.py
"""
Runtime configuration for the LLM agent.

Precedence:
  1. config/config_ia.yaml -> `llm:` section (defaults, env-resolved)
  2. config/llm_config.json -> runtime overrides written by the dashboard API

The dashboard writes overrides to the JSON file; the agent watches the file's
mtime and picks up changes on the next cycle without restarting.
"""
import os
import json
import time
import logging
import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

VALID_MODES = ("autonomous", "hybrid", "analyst")
VALID_PROVIDERS = ("openrouter", "ollama", "anthropic")

OVERRIDE_FIELDS = (
    "enabled",
    "mode",
    "provider",
    "model",
    "base_url",
    "check_interval_seconds",
    "max_tokens",
    "temperature",
    "timeout_seconds",
    "daily_budget_usd",
    "analyst_report_hour",
    "system_prompt",
    "enabled_tools",
    "keep_alive",
    "num_ctx",
    "num_thread",
)


@dataclass
class LLMRuntimeConfig:
    enabled: bool = True
    mode: str = "autonomous"
    provider: str = "openrouter"
    model: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    check_interval_seconds: int = 900
    max_tokens: int = 2048
    temperature: float = 0.3
    timeout_seconds: int = 120
    daily_budget_usd: float = 2.0
    analyst_report_hour: str = "08:00"
    system_prompt: str = ""
    enabled_tools: List[str] = field(default_factory=list)  # empty = all
    keep_alive: str = "5m"       # Ollama model keep_alive duration
    num_ctx: int = 4096          # Ollama context window size
    num_thread: int = 4          # Ollama CPU threads

    # Paths (from yaml, not overridable from dashboard)
    log_file: str = "logs/llm_agent.log"
    events_file: str = "logs/llm_events.json"
    state_file: str = "logs/llm_state.json"
    runtime_config_file: str = "config/llm_config.json"
    instructions_path: str = "config/instructions.json"
    csv_path: str = "grid_servicios.csv"

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "has_api_key": bool(self.api_key()),
            "check_interval_seconds": self.check_interval_seconds,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "daily_budget_usd": self.daily_budget_usd,
            "analyst_report_hour": self.analyst_report_hour,
            "system_prompt": self.system_prompt,
            "enabled_tools": list(self.enabled_tools),
            "keep_alive": self.keep_alive,
            "num_ctx": self.num_ctx,
            "num_thread": self.num_thread,
            "log_file": self.log_file,
            "events_file": self.events_file,
            "state_file": self.state_file,
        }


def build_default_from_llm_config(cfg, project_root: str) -> LLMRuntimeConfig:
    """Build runtime config from the config_ia.yaml `llm` section."""
    runtime = LLMRuntimeConfig()
    runtime.enabled = cfg.enabled
    runtime.mode = cfg.mode
    runtime.provider = cfg.provider
    runtime.model = cfg.model
    runtime.base_url = cfg.base_url
    runtime.api_key_env = cfg.api_key_env
    runtime.check_interval_seconds = cfg.check_interval_seconds
    runtime.max_tokens = cfg.max_tokens
    runtime.temperature = cfg.temperature
    runtime.timeout_seconds = cfg.timeout_seconds
    runtime.daily_budget_usd = cfg.daily_budget_usd
    runtime.analyst_report_hour = cfg.analyst_report_hour
    runtime.system_prompt = cfg.system_prompt
    runtime.keep_alive = getattr(cfg, "keep_alive", "5m")
    runtime.num_ctx = getattr(cfg, "num_ctx", 4096)
    runtime.num_thread = getattr(cfg, "num_thread", 4)

    for name in ("log_file", "events_file", "state_file", "runtime_config_file"):
        value = getattr(cfg, name, "")
        if value and not os.path.isabs(value):
            value = os.path.join(project_root, value)
        setattr(runtime, name, value)

    runtime.instructions_path = os.path.join(
        project_root, "config", "instructions.json"
    )
    runtime.csv_path = os.path.join(project_root, "grid_servicios.csv")
    return runtime


class RuntimeConfigStore:
    """Loads yaml defaults, applies JSON overrides, persists overrides."""

    def __init__(self, base: LLMRuntimeConfig):
        self.base = base
        self._overrides: Dict[str, Any] = {}
        self._mtime: float = 0
        self._load()

    @property
    def path(self) -> str:
        return self.base.runtime_config_file

    def _load(self):
        path = self.path
        if not path or not os.path.exists(path):
            self._overrides = {}
            self._mtime = 0
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._overrides = json.load(f) or {}
            self._mtime = os.path.getmtime(path)
        except Exception as e:
            logger.warning(f"Failed to load runtime config {path}: {e}")
            self._overrides = {}

    def effective(self) -> LLMRuntimeConfig:
        runtime = copy.deepcopy(self.base)
        for key in OVERRIDE_FIELDS:
            if key in self._overrides:
                setattr(runtime, key, self._overrides[key])
        return runtime

    def changed(self) -> bool:
        path = self.path
        if not path or not os.path.exists(path):
            return self._mtime != 0
        return os.path.getmtime(path) != self._mtime

    def reload(self):
        self._load()

    def update(self, updates: Dict[str, Any]) -> LLMRuntimeConfig:
        """Apply validated updates and persist to JSON."""
        current = self.effective()
        for key, value in updates.items():
            if key not in OVERRIDE_FIELDS:
                continue
            if key == "mode":
                if value not in VALID_MODES:
                    raise ValueError(f"Invalid mode: {value}")
            elif key == "provider":
                if value not in VALID_PROVIDERS:
                    raise ValueError(f"Invalid provider: {value}")
            elif key in ("check_interval_seconds", "max_tokens", "timeout_seconds"):
                if not isinstance(value, int) or value <= 0:
                    raise ValueError(f"Invalid {key}: {value}")
            elif key in ("temperature",):
                if not isinstance(value, (int, float)) or not (0 <= value <= 2):
                    raise ValueError(f"Invalid temperature: {value}")
            elif key in ("daily_budget_usd",):
                if not isinstance(value, (int, float)) or value < 0:
                    raise ValueError(f"Invalid budget: {value}")
            elif key == "model" and not isinstance(value, str):
                raise ValueError("Invalid model")
            elif key == "enabled_tools" and not isinstance(value, list):
                raise ValueError("Invalid enabled_tools: must be a list")
            elif key == "keep_alive":
                if not isinstance(value, str):
                    raise ValueError("Invalid keep_alive: must be a string like '5m', '10m', '0'")
            elif key == "num_ctx":
                if not isinstance(value, int) or value <= 0:
                    raise ValueError("Invalid num_ctx: must be positive int")
            elif key == "num_thread":
                if not isinstance(value, int) or value <= 0:
                    raise ValueError("Invalid num_thread: must be positive int")
            self._overrides[key] = value

        self._save()
        return self.effective()

    def _save(self):
        path = self.path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._overrides, f, indent=2, ensure_ascii=False)
        self._mtime = os.path.getmtime(path) if os.path.exists(path) else 0
