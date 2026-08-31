# src/ai/llm_agent/__init__.py
"""
LLM Agent — Active AI agent for the monitoring system.

A separate lightweight process (no torch dependency) that runs in one of three
modes:
  - autonomous: the LLM decides what to inspect and what to alert
  - hybrid:    deterministic rules trigger, LLM enriches analysis
  - analyst:   on-demand + scheduled analysis

Tools = enabled rules from config/instructions.json (read-only + send_alert).
Provider-agnostic (OpenRouter first, Ollama local later).
"""
