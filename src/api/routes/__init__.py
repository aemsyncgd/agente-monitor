# src/api/routes/__init__.py
from . import metrics, anomalies, health, agent_llm, syslog

__all__ = ["metrics", "anomalies", "health", "agent_llm", "syslog"]
