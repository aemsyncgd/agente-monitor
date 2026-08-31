# src/ai/llm_agent/providers.py
"""
LLM provider abstraction.

Implements the OpenAI-compatible chat-completions shape used by OpenRouter and
Ollama so the agent loop stays provider-agnostic. Only needs `requests`.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import requests

logger = logging.getLogger(__name__)


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    ok: bool = True

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens", 0) or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens", 0) or 0)


class LLMProvider:
    """Base provider contract."""

    name = "base"

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: int = 120, keep_alive: str = "5m",
                 num_ctx: int = 4096, num_thread: int = 4):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_thread = num_thread

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
             max_tokens: int = 2048, temperature: float = 0.3) -> LLMResult:
        raise NotImplementedError

    def list_models(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def test(self) -> bool:
        try:
            result = self.chat(
                [{"role": "user", "content": "Responde solo: OK"}],
                max_tokens=8,
                temperature=0,
            )
            return result.ok
        except Exception as e:
            logger.error(f"Provider test failed: {e}")
            return False


class OpenRouterProvider(LLMProvider):
    """OpenRouter — OpenAI-compatible /chat/completions."""

    name = "openrouter"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        return headers

    def chat(self, messages, tools=None, max_tokens=2048,
             temperature=0.3) -> LLMResult:
        payload: Dict[str, Any] = {
            "model": self.model or "openrouter/auto",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            if resp.status_code == 401:
                return LLMResult(error="API key inválida (401)", ok=False)
            if resp.status_code == 402:
                return LLMResult(error="Saldo insuficiente en OpenRouter (402)", ok=False)
            resp.raise_for_status()
            return self._parse(resp.json())
        except requests.exceptions.Timeout:
            return LLMResult(error=f"Timeout tras {self.timeout}s", ok=False)
        except Exception as e:
            return LLMResult(error=f"OpenRouter error: {e}", ok=False)

    def _parse(self, data: Dict) -> LLMResult:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        content = message.get("content")
        result = LLMResult(
            text=(content or "").strip(),
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
        )

        tool_calls = message.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result.tool_calls.append(LLMToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))
        return result

    def list_models(self) -> List[Dict[str, Any]]:
        try:
            resp = requests.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.error(f"OpenRouter list models failed: {e}")
            return []


class OllamaProvider(LLMProvider):
    """Ollama — local models via /api/chat (tools supported)."""

    name = "ollama"

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    @staticmethod
    def _normalize_messages(messages: List[Dict]) -> List[Dict]:
        """Convert OpenAI-style messages to Ollama's /api/chat format."""
        out: List[Dict] = []
        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                calls = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except json.JSONDecodeError:
                            args = {}
                    calls.append({"function": {"name": fn.get("name", ""),
                                               "arguments": args}})
                out.append({"role": "assistant", "content": "",
                            "tool_calls": calls})
                continue
            out.append(msg)
        return out

    def chat(self, messages, tools=None, max_tokens=2048,
             temperature=0.3) -> LLMResult:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._normalize_messages(messages),
            "options": {
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
                "num_thread": self.num_thread,
                "temperature": temperature,
            },
            "keep_alive": self.keep_alive,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        try:
            logger.info(f"Ollama request: model={self.model}, base_url={self.base_url}")
            resp = requests.post(
                f"{self.base_url}/api/chat",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            logger.info(f"Ollama response: {resp.status_code}")
            if resp.status_code != 200:
                logger.error(f"Ollama error body: {resp.text[:500]}")
            resp.raise_for_status()
            return self._parse(resp.json())
        except requests.exceptions.Timeout:
            return LLMResult(error=f"Timeout tras {self.timeout}s", ok=False)
        except Exception as e:
            logger.error(f"Ollama request failed: {e}")
            return LLMResult(error=f"Ollama error: {e}", ok=False)

    def _parse(self, data: Dict) -> LLMResult:
        message = data.get("message", {}) or {}
        content = message.get("content")
        result = LLMResult(
            text=(content or "").strip(),
            model=data.get("model", self.model),
            usage={
                "prompt_tokens": int(data.get("prompt_eval_count", 0) or 0),
                "completion_tokens": int(data.get("eval_count", 0) or 0),
            },
        )
        tool_calls = message.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", {}) or {}
            result.tool_calls.append(LLMToolCall(
                id=tc.get("id") or f"ollama_{time.time()}",
                name=fn.get("name", ""),
                arguments=args if isinstance(args, dict) else {},
            ))
        return result

    def list_models(self) -> List[Dict[str, Any]]:
        try:
            resp = requests.get(
                f"{self.base_url}/api/tags",
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {"id": m.get("name", ""), "name": m.get("name", "")}
                for m in data.get("models", [])
            ]
        except Exception as e:
            logger.error(f"Ollama list models failed: {e}")
            return []


class AnthropicProvider(LLMProvider):
    """Anthropic Claude via Messages API. Designed to work behind
    pxpipe-proxy (http://127.0.0.1:47821) for ~60% token savings."""

    name = "anthropic"

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI-style tools to Anthropic tool format."""
        out = []
        for t in tools:
            fn = t.get("function", {})
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    def _convert_messages(self, messages: List[Dict]) -> List[Dict]:
        """Convert OpenAI messages to Anthropic format (separate system)."""
        system_text = ""
        out = []
        for msg in messages:
            if msg.get("role") == "system":
                system_text += (msg.get("content") or "") + "\n"
                continue
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "assistant" and msg.get("tool_calls"):
                content_parts = []
                if content:
                    content_parts.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except json.JSONDecodeError:
                            args = {}
                    content_parts.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                out.append({"role": "assistant", "content": content_parts})
                continue
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content,
                    }],
                })
                continue
            out.append({"role": role, "content": content})
        return system_text.strip(), out

    def chat(self, messages, tools=None, max_tokens=2048,
             temperature=0.3) -> LLMResult:
        system_text, converted = self._convert_messages(messages)
        payload: Dict[str, Any] = {
            "model": self.model or "claude-3-haiku-20240307",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": converted,
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = self._convert_tools(tools)

        try:
            logger.info(f"Anthropic request: model={self.model}, base_url={self.base_url}")
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            logger.info(f"Anthropic response: {resp.status_code}")
            if resp.status_code == 401:
                return LLMResult(error="API key inválida (401)", ok=False)
            if resp.status_code == 402:
                return LLMResult(error="Saldo insuficiente (402)", ok=False)
            resp.raise_for_status()
            return self._parse(resp.json())
        except requests.exceptions.Timeout:
            return LLMResult(error=f"Timeout tras {self.timeout}s", ok=False)
        except Exception as e:
            logger.error(f"Anthropic request failed: {e}")
            return LLMResult(error=f"Anthropic error: {e}", ok=False)

    def _parse(self, data: Dict) -> LLMResult:
        text_parts = []
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(LLMToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))
        usage = data.get("usage", {})
        return LLMResult(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            model=data.get("model", self.model),
            usage={
                "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            },
        )

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": "claude-3-haiku-20240307", "name": "Claude 3 Haiku"},
            {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
            {"id": "claude-3-opus-20240229", "name": "Claude 3 Opus"},
        ]


def create_provider(config=None, provider_name: str = "", model: str = "",
                    api_key: str = "", ollama_url: str = "",
                    keep_alive: str = "5m", num_ctx: int = 4096,
                    num_thread: int = 4) -> LLMProvider:
    """Factory from LLMRuntimeConfig or direct parameters."""
    if config is not None:
        p_name = getattr(config, "provider", "openrouter")
        base_url = getattr(config, "base_url", "")
        m_name = getattr(config, "model", "")
        key = config.api_key() if callable(getattr(config, "api_key", None)) else getattr(config, "api_key", "")
        timeout = getattr(config, "timeout_seconds", 120)
        keep_alive = getattr(config, "keep_alive", "5m")
        num_ctx = getattr(config, "num_ctx", 4096)
        num_thread = getattr(config, "num_thread", 4)
    else:
        p_name = provider_name or "openrouter"
        base_url = ollama_url if p_name == "ollama" else ""
        m_name = model
        key = api_key
        timeout = 120

    if p_name == "ollama":
        if not base_url or "openrouter" in base_url:
            base_url = "http://localhost:11434"
        return OllamaProvider(
            base_url=base_url,
            api_key=key,
            model=m_name,
            timeout=timeout,
            keep_alive=keep_alive,
            num_ctx=num_ctx,
            num_thread=num_thread,
        )
    if p_name == "anthropic":
        if not base_url:
            base_url = "http://127.0.0.1:47821"
        return AnthropicProvider(
            base_url=base_url,
            api_key=key,
            model=m_name,
            timeout=timeout,
        )
    if not base_url or "localhost:11434" in base_url:
        base_url = "https://openrouter.ai/api/v1"
    return OpenRouterProvider(
        base_url=base_url,
        api_key=key,
        model=m_name,
        timeout=timeout,
    )
