"""Provider-agnostic LLM client: one method, decide(), that returns exactly one tool call.

Every turn is a fresh, self-contained prompt (system + one user message) instead of a
growing chat. That keeps token use bounded, and it avoids each provider's different
format for tool results, so swapping providers is a .env change.

Only the discovery agent and bounded recovery use this module. Replay never imports it.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

from src.models.settings import Settings

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
PROVIDERS = ("groq", "openai", "anthropic")
MAX_API_ATTEMPTS = 3  # transient API errors (rate limit, timeout) are retried a few times
# Per request. The SDK default is 600 s with 2 hidden retries: a hung request looked like a frozen
# run for up to 30 minutes. Retries are done by _with_retries instead, where they are visible.
REQUEST_TIMEOUT_S = 60
RATE_LIMIT_WAIT_S = 20        # when the provider does not say how long to wait
MAX_RATE_LIMIT_WAIT_S = 60    # a per-minute limit has reset by then


class LLMError(Exception):
    """The LLM could not give a usable answer (API down, no tool call)."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


class LLMClient(Protocol):
    model: str

    def decide(self, system: str, prompt: str, tools: list[dict]) -> ToolCall: ...


def _with_retries(call):
    """Retry transient failures with a short backoff. Anything else is raised as LLMError."""
    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            return call()
        except Exception as e:  # SDK errors differ per provider; classify by name
            transient = type(e).__name__ in {"RateLimitError", "APITimeoutError", "APIConnectionError",
                                             "InternalServerError", "OverloadedError"}
            # Groq rejects a tool call whose arguments break the schema. The model's output
            # varies between calls, so asking again usually works.
            transient = transient or "tool call validation failed" in str(e).lower()
            if not transient or attempt == MAX_API_ATTEMPTS:
                raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
            time.sleep(_backoff_s(e, attempt))


def _backoff_s(error: Exception, attempt: int) -> float:
    """How long to wait before retrying. A rate limit (e.g. Groq's tokens per minute) says how long
    in its retry-after header; a short fixed backoff would retry while the limit still applies."""
    if type(error).__name__ == "RateLimitError":
        headers = getattr(getattr(error, "response", None), "headers", None) or {}
        try:
            wait = float(headers.get("retry-after", RATE_LIMIT_WAIT_S))
        except ValueError:
            wait = RATE_LIMIT_WAIT_S
        wait = min(max(wait, 1), MAX_RATE_LIMIT_WAIT_S)
        print(f"  [llm] rate limit reached, waiting {wait:.0f} s before retrying")
        return wait
    return 2 * attempt


class OpenAICompatibleClient:
    """OpenAI's chat-completions API. Groq speaks the same API, so it is this client with another URL."""

    def __init__(self, api_key: str, model: str, temperature: float, base_url: str | None = None):
        from openai import OpenAI  # imported here: only needed when this provider is chosen
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_S, max_retries=0)
        self.model = model
        self.temperature = temperature

    def decide(self, system: str, prompt: str, tools: list[dict]) -> ToolCall:
        response = _with_retries(lambda: self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            tools=[{"type": "function", "function": t} for t in tools],
            tool_choice="required",  # an answer in prose is not an action
        ))
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            raise LLMError("the model answered without a tool call")
        # One action per turn: if the model sends several, only the first is used, so it can
        # never batch actions; each one is checked and its result seen before the next.
        fn = calls[0].function
        try:
            return ToolCall(fn.name, json.loads(fn.arguments or "{}"))
        except json.JSONDecodeError as e:
            raise LLMError(f"tool arguments were not valid JSON: {e}") from e


class AnthropicClient:
    def __init__(self, api_key: str, model: str, temperature: float):
        from anthropic import Anthropic
        self._client = Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_S, max_retries=0)
        self.model = model
        self.temperature = temperature

    def decide(self, system: str, prompt: str, tools: list[dict]) -> ToolCall:
        response = _with_retries(lambda: self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                   for t in tools],
            tool_choice={"type": "any"},
        ))
        for block in response.content:
            if block.type == "tool_use":
                return ToolCall(block.name, dict(block.input))
        raise LLMError("the model answered without a tool call")


def make_llm(settings: Settings) -> LLMClient:
    """Build the real client from .env (LLM_PROVIDER, LLM_API_KEY, optional LLM_MODEL)."""
    provider = (os.environ.get("LLM_PROVIDER") or "groq").lower()
    if provider not in PROVIDERS:
        raise LLMError(f"unknown LLM_PROVIDER '{provider}' (use {', '.join(PROVIDERS)})")
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise LLMError("LLM_API_KEY is not set in .env (or run with --mock)")
    model = os.environ.get("LLM_MODEL") or settings.llm_models.get(provider)
    if not model:
        raise LLMError(f"no model configured for provider '{provider}' in config/settings.json")
    t = settings.llm_temperature
    if provider == "groq":
        return OpenAICompatibleClient(api_key, model, t, base_url=GROQ_BASE_URL)
    if provider == "openai":
        return OpenAICompatibleClient(api_key, model, t)
    return AnthropicClient(api_key, model, t)
