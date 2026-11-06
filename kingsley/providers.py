"""Model router: task-aware provider selection with automatic failover.

Three wire formats are supported because that is what the upstreams speak:

* ``openai``    — OpenAI chat completions (also Grok/xAI, FreeLLMAPI, Ollama)
* ``anthropic`` — Claude Messages API
* ``gemini``    — Google generateContent

Every call returns which provider actually served it, and every failure is
recorded so the UI can show a truthful routing log instead of pretending.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any

import httpx

from kingsley import config, store

_ANTHROPIC_VERSION = "2023-06-01"
_DATA_URL = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.*)$", re.S)


class ProviderError(RuntimeError):
    """Upstream call failed for one provider; the router moves on."""


def _split_data_url(image: str) -> tuple[str, str]:
    match = _DATA_URL.match(image.strip())
    if match:
        return match.group("mime"), match.group("data")
    return "image/jpeg", image


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(method, url, headers=headers, json=json_body, params=params)
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport: {exc}") from exc
    if response.status_code >= 400:
        body = response.text[:400].replace("\n", " ")
        raise ProviderError(f"http {response.status_code}: {body}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"non-JSON response: {response.text[:200]}") from exc


async def _call_openai(
    provider: config.Provider,
    messages: list[dict[str, Any]],
    system: str | None,
    images: list[str],
    max_tokens: int,
    temperature: float,
) -> str:
    chat: list[dict[str, Any]] = []
    if system:
        chat.append({"role": "system", "content": system})
    for index, message in enumerate(messages):
        content: Any = message["content"]
        if images and index == len(messages) - 1:
            parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
            for image in images:
                parts.append({"type": "image_url", "image_url": {"url": image}})
            content = parts
        chat.append({"role": message["role"], "content": content})

    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": provider.model,
        "messages": chat,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        data = await _request(client, "POST", f"{provider.base_url}/chat/completions", headers=headers, json_body=payload)
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError(f"empty choices: {str(data)[:200]}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if isinstance(text, list):  # some providers return content blocks
        text = "".join(block.get("text", "") for block in text if isinstance(block, dict))
    if not text:
        raise ProviderError("empty completion")
    return str(text).strip()


async def _call_anthropic(
    provider: config.Provider,
    messages: list[dict[str, Any]],
    system: str | None,
    images: list[str],
    max_tokens: int,
    temperature: float,
) -> str:
    blocks: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content: list[dict[str, Any]] = [{"type": "text", "text": message["content"]}]
        if images and index == len(messages) - 1:
            for image in images:
                mime, data = _split_data_url(image)
                content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
        blocks.append({"role": message["role"], "content": content})

    headers = {
        "x-api-key": provider.api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": blocks,
    }
    if system:
        payload["system"] = system
    async with httpx.AsyncClient(timeout=120) as client:
        data = await _request(client, "POST", f"{provider.base_url}/v1/messages", headers=headers, json_body=payload)
    parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise ProviderError("empty completion")
    return text


async def _call_gemini(
    provider: config.Provider,
    messages: list[dict[str, Any]],
    system: str | None,
    images: list[str],
    max_tokens: int,
    temperature: float,
) -> str:
    contents: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        parts: list[dict[str, Any]] = [{"text": message["content"]}]
        if images and index == len(messages) - 1:
            for image in images:
                mime, data = _split_data_url(image)
                parts.append({"inline_data": {"mime_type": mime, "data": data}})
        role = "model" if message["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": parts})

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = f"{provider.base_url}/v1beta/models/{provider.model}:generateContent"
    async with httpx.AsyncClient(timeout=120) as client:
        data = await _request(client, "POST", url, json_body=payload, params={"key": provider.api_key})
    candidates = data.get("candidates") or []
    if not candidates:
        raise ProviderError(f"no candidates: {str(data)[:200]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise ProviderError("empty completion")
    return text


_CALLERS = {"openai": _call_openai, "anthropic": _call_anthropic, "gemini": _call_gemini}


class Router:
    """Task-aware, failover-capable model router."""

    def __init__(self) -> None:
        self.providers = config.load_providers()
        self._probe_cache: tuple[float, dict[str, str]] = (0.0, {})
        self._probe_lock = asyncio.Lock()

    # -- routing ---------------------------------------------------------
    def chain(self, task: str, override: str | None = None) -> list[config.Provider]:
        if override and override in self.providers:
            preferred = [override]
        else:
            preferred = list(config.TASK_ROUTES.get(task, config.TASK_ROUTES["default"]))
        ordered: list[config.Provider] = []
        for slug in preferred + list(self.providers):
            provider = self.providers.get(slug)
            if provider and provider not in ordered:
                ordered.append(provider)
        return ordered

    async def chat(
        self,
        task: str,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        images: list[str] | None = None,
        override: str | None = None,
        max_tokens: int = 1400,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        images = images or []
        attempts: list[dict[str, str]] = []
        for provider in self.chain(task, override):
            if not provider.configured:
                attempts.append({"provider": provider.slug, "error": "not configured"})
                continue
            if images and not provider.vision:
                attempts.append({"provider": provider.slug, "error": "no vision support"})
                continue
            caller = _CALLERS[provider.kind]
            try:
                text = await caller(provider, messages, system, images, max_tokens, temperature)
            except (ProviderError, KeyError) as exc:
                attempts.append({"provider": provider.slug, "error": str(exc)[:240]})
                continue
            return {
                "text": text,
                "provider": provider.slug,
                "provider_label": provider.label,
                "model": provider.model,
                "task": task,
                "attempts": attempts,
            }
        return {
            "text": "",
            "error": "no provider could serve this request",
            "task": task,
            "attempts": attempts,
        }

    # -- health ----------------------------------------------------------
    async def probe(self, ttl: float = 60.0) -> dict[str, str]:
        async with self._probe_lock:
            stamp, cached = self._probe_cache
            if cached and time.time() - stamp < ttl:
                return cached
            results: dict[str, str] = {}
            for slug, provider in self.providers.items():
                if not provider.configured:
                    results[slug] = "unconfigured"
                    continue
                results[slug] = await self._probe_one(provider)
            self._probe_cache = (time.time(), results)
            return results

    async def _probe_one(self, provider: config.Provider) -> str:
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                if provider.kind == "gemini":
                    response = await client.get(
                        f"{provider.base_url}/v1beta/models",
                        params={"key": provider.api_key, "pageSize": 1},
                    )
                elif provider.kind == "anthropic":
                    response = await client.get(
                        f"{provider.base_url}/v1/models",
                        headers={"x-api-key": provider.api_key, "anthropic-version": _ANTHROPIC_VERSION},
                    )
                else:
                    response = await client.get(
                        f"{provider.base_url}/models",
                        headers={"Authorization": f"Bearer {provider.api_key}"},
                    )
            if response.status_code < 400:
                return "up"
            if response.status_code in (401, 403):
                return "unauthorized"
            return f"http {response.status_code}"
        except httpx.HTTPError:
            return "down"

    def describe(self) -> list[dict[str, Any]]:
        return [provider.public() for provider in self.providers.values()]


ROUTER = Router()
