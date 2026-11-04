"""
CRAVE Council v2 — Provider Registry
======================================
Abstracts Groq, OpenRouter (Gemini / free models) and OpenAI into a single
unified chat interface.  Every agent picks a provider by name; this module
handles authentication, rate-limit back-off and transparent fallback.

Provider priority (read from .env):
  1. groq          → COUNCIL_GROQ_API_KEY
  2. openrouter    → COUNCIL_GEMINI_API_KEY + COUNCIL_GEMINI_BASE_URL
  3. openai        → COUNCIL_OPENAI_API_KEY
"""

import os
import time
import logging
import requests
from typing import Optional

log = logging.getLogger("crave.council_v2.providers")

# ── Provider definitions ───────────────────────────────────────────────────────

class ProviderError(Exception):
    """Raised when a provider call fails and no fallback is left."""


class _BaseProvider:
    name: str = "base"

    def chat(self, messages: list[dict], max_tokens: int = 800) -> str:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class GroqProvider(_BaseProvider):
    name = "groq"

    def __init__(self):
        self._key   = os.environ.get("COUNCIL_GROQ_API_KEY", "").strip()
        self._model = os.environ.get("COUNCIL_GROQ_MODEL", "llama-3.1-70b-versatile").strip()
        self._url   = "https://api.groq.com/openai/v1/chat/completions"

    def available(self) -> bool:
        return bool(self._key)

    def chat(self, messages: list[dict], max_tokens: int = 800) -> str:
        if not self._key:
            raise ProviderError("COUNCIL_GROQ_API_KEY not set")
        resp = requests.post(
            self._url,
            json={"model": self._model, "messages": messages, "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        if resp.status_code == 429:
            raise ProviderError(f"Groq rate-limited: {resp.text[:120]}")
        raise ProviderError(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")


class OpenRouterProvider(_BaseProvider):
    """
    OpenRouter — used as the Gemini / secondary provider slot.
    Env vars: COUNCIL_GEMINI_API_KEY (the OR key) + COUNCIL_GEMINI_BASE_URL
    + COUNCIL_GEMINI_MODEL (e.g. google/gemini-2.0-flash-exp:free)
    """
    name = "openrouter"

    # Ordered list of free models to try if the primary is rate-limited
    FREE_FALLBACKS = [
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.1-8b-instruct:free",
        "deepseek/deepseek-r1:free",
        "mistralai/mistral-7b-instruct:free",
    ]

    def __init__(self):
        self._key   = os.environ.get("COUNCIL_GEMINI_API_KEY", "").strip()
        self._base  = os.environ.get("COUNCIL_GEMINI_BASE_URL", "").strip().rstrip("/")
        self._model = os.environ.get("COUNCIL_GEMINI_MODEL",
                                     "google/gemini-2.0-flash-exp:free").strip()

    def available(self) -> bool:
        return bool(self._key and self._base)

    def chat(self, messages: list[dict], max_tokens: int = 800) -> str:
        if not self.available():
            raise ProviderError("OpenRouter not configured (COUNCIL_GEMINI_API_KEY / BASE_URL missing)")

        candidates = [self._model] + [m for m in self.FREE_FALLBACKS if m != self._model]
        last_error = ""

        for model in candidates:
            try:
                resp = requests.post(
                    f"{self._base}/chat/completions",
                    json={"model": model, "messages": messages, "max_tokens": max_tokens},
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://crave-quant.local",
                        "X-Title": "CRAVE Council v2",
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                if resp.status_code == 429:
                    last_error = f"rate-limited on {model}"
                    time.sleep(1)
                    continue
                last_error = f"HTTP {resp.status_code} on {model}: {resp.text[:120]}"
                break
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                break

        raise ProviderError(f"OpenRouter exhausted all models: {last_error}")


class OpenAIProvider(_BaseProvider):
    name = "openai"

    def __init__(self):
        self._key   = os.environ.get("COUNCIL_OPENAI_API_KEY", "").strip()
        self._base  = os.environ.get("COUNCIL_OPENAI_BASE_URL",
                                     "https://api.openai.com/v1").strip().rstrip("/")
        self._model = os.environ.get("COUNCIL_OPENAI_MODEL", "gpt-4o-mini").strip()

    def available(self) -> bool:
        return bool(self._key)

    def chat(self, messages: list[dict], max_tokens: int = 800) -> str:
        if not self._key:
            raise ProviderError("COUNCIL_OPENAI_API_KEY not set")
        resp = requests.post(
            f"{self._base}/chat/completions",
            json={"model": self._model, "messages": messages, "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        if resp.status_code == 429:
            raise ProviderError("OpenAI no credits / rate-limited")
        raise ProviderError(f"OpenAI HTTP {resp.status_code}: {resp.text[:200]}")


# ── Registry singleton ─────────────────────────────────────────────────────────

class ProviderRegistry:
    """
    Manages all configured providers and routes calls by priority.
    Thread-safe via stateless provider instances (all state in .env).
    """

    def __init__(self):
        self._providers: list[_BaseProvider] = [
            GroqProvider(),
            OpenRouterProvider(),
            OpenAIProvider(),
        ]

    def available_providers(self) -> list[str]:
        return [p.name for p in self._providers if p.available()]

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 800,
        prefer: Optional[str] = None,
        exclude: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        """
        Call LLMs in priority order. Returns (response_text, provider_name).
        prefer: try this provider first (e.g. "groq" or "openrouter")
        exclude: skip these provider names (e.g. if already used by another agent)
        """
        exclude = exclude or []
        ordered = list(self._providers)

        # Move preferred provider to front
        if prefer:
            ordered = sorted(ordered, key=lambda p: 0 if p.name == prefer else 1)

        for provider in ordered:
            if provider.name in exclude:
                continue
            if not provider.available():
                continue
            try:
                text = provider.chat(messages, max_tokens=max_tokens)
                if text and text.strip():
                    log.debug(f"[Registry] {provider.name} responded ({len(text)} chars)")
                    return text.strip(), provider.name
            except ProviderError as e:
                log.warning(f"[Registry] {provider.name} failed: {e}")
                continue
            except Exception as e:
                log.warning(f"[Registry] {provider.name} unexpected error: {e}")
                continue

        raise ProviderError(
            f"All providers exhausted. Available: {self.available_providers()}"
        )


# Singleton
_registry: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry

