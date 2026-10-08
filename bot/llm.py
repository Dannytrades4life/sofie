"""Swappable LLM client (text + vision).

Groq, Gemini, OpenRouter and Ollama all expose an OpenAI-compatible chat endpoint, so
one small client covers every free option. If no provider is configured, or a call fails
or is rate-limited, callers get None and fall back to templates; the bot never goes silent.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

PROVIDERS = {
    # name: (base_url, default text model, default vision model)
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "qwen/qwen3.8-27b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "gemini-2.5-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"),
    "ollama": ("http://localhost:11434/v1", "llama3.2", "llama3.2-vision"),
}


# If the configured model has been retired (providers rename models often), try these in order.
FALLBACKS = {
    "groq": (["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"], ["qwen/qwen3.8-27b"]),
    "gemini": (["gemini-2.5-flash", "gemini-flash-latest"], ["gemini-2.5-flash", "gemini-flash-latest"]),
}


class LLM:
    def __init__(self, provider: str, api_key: str = "", model: str = "", base_url: str = "", *, vision: bool = False):
        self.provider = provider
        default_url, text_model, vision_model = PROVIDERS.get(provider, ("", "", ""))
        self.base_url = (base_url or default_url).rstrip("/")
        self.model = model or (vision_model if vision else text_model)
        self.vision = vision
        self._swapped = False
        self.api_key = api_key
        self.enabled = bool(self.base_url) and provider != "none"
        if self.enabled and provider != "ollama" and not api_key:
            log.warning("%s provider %s has no API key; disabled.", "Vision" if vision else "LLM", provider)
            self.enabled = False
        self._client = httpx.AsyncClient(timeout=45)
        self._lock = asyncio.Semaphore(2)
        self._min_interval = 2.5  # seconds between calls, keeps us inside free-tier per-minute limits
        self._last_call = 0.0
        self._cooldown_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _complete(self, messages: list[dict], max_tokens: int, temperature: float) -> str | None:
        if not self.enabled or time.monotonic() < self._cooldown_until:
            return None
        async with self._lock:
            wait = self._last_call + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
            try:
                r = await self._client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                if r.status_code in (400, 404) and "model" in r.text.lower() and await self._swap_model(headers):
                    body["model"] = self.model
                    r = await self._client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
                if r.status_code == 429:
                    retry = float(r.headers.get("retry-after", "60") or 60)
                    self._cooldown_until = time.monotonic() + min(retry, 600)
                    log.warning("LLM rate-limited; using templates for %.0fs", retry)
                    return None
                if r.status_code >= 400:
                    log.warning("LLM call failed (%s, model %s): %s", r.status_code, self.model, r.text[:300])
                    return None
                return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as exc:
                log.warning("LLM call failed: %s", exc)
                return None

    async def _swap_model(self, headers: dict) -> bool:
        """The model was rejected: pick the first fallback the provider still lists. Only tried once per run."""
        if self._swapped:
            return False
        self._swapped = True
        try:
            r = await self._client.get(f"{self.base_url}/models", headers=headers)
            available = {m.get("id", "").removeprefix("models/") for m in r.json().get("data", [])}
        except Exception as exc:
            log.warning("Couldn't list %s models: %s", self.provider, exc)
            return False
        text, vision = FALLBACKS.get(self.provider, ([], []))
        for name in vision if self.vision else text:
            if name in available and name != self.model:
                log.warning("Model %s was rejected by %s; switching to %s. Set %s_MODEL in .env to choose another.",
                            self.model, self.provider, name, "VISION" if self.vision else "LLM")
                self.model = name
                return True
        log.warning("Model %s was rejected and no fallback is available. %s models: %s", self.model, self.provider, ", ".join(sorted(available))[:600])
        return False

    async def chat(self, system: str, user: str, *, max_tokens: int = 220, temperature: float = 0.9) -> str | None:
        text = await self._complete([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens, temperature)
        return clean(text) if text else None

    async def json(self, system: str, user: str, *, max_tokens: int = 500, temperature: float = 0.6) -> dict | list | None:
        text = await self._complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user + "\nRespond with JSON only, no prose."}],
            max_tokens, temperature,
        )
        return parse_json(text)

    async def vision_json(self, prompt: str, image: bytes, mime: str = "image/png", *, max_tokens: int = 600) -> dict | None:
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        text = await self._complete(
            [{"role": "user", "content": [
                {"type": "text", "text": prompt + "\nRespond with JSON only, no prose."},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            max_tokens, 0.1,
        )
        data = parse_json(text)
        return data if isinstance(data, dict) else None


def parse_json(text: str | None):
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    # never let the model ping everyone
    return text.replace("@everyone", "everyone").replace("@here", "here").strip()[:1800]
