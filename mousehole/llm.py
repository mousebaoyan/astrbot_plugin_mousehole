"""The LLM and the embedding model, called over the OpenAI-compatible API directly rather than
through AstrBot's providers, which drop per-request options such as the response format and
thinking (design §3). Replay uses these same clients with the same settings."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("astrbot.mousehole")

# Too many requests at once: ECNU allows 3 per model and person, shared with other plugins.
BUSY_WAIT = 15
BUSY_TRIES = 8


class LLMError(Exception):
    pass


class BudgetExhausted(Exception):
    """Today's tokens are used up: rounds are let go until tomorrow."""


@dataclass
class Settings:
    base_url: str
    api_key: str
    model: str
    # "json_schema", "json_object" or "off"
    structured_output: str = "json_schema"
    # "enabled", "disabled", or "omit" for providers that know no such field
    thinking: str = "enabled"
    reasoning_effort: str = "low"
    temperature: float = 0.2
    max_tokens: int = 8192
    timeout_seconds: int = 300
    # 0 for no limit.
    tokens_per_day: int = 0

    @classmethod
    def of(cls, config: dict) -> "Settings":
        return cls(**{k: config[k] for k in cls.__dataclass_fields__ if k in config})


class LLM:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        self.http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=settings.timeout_seconds,
        )
        self.sleep = sleep
        self.day = time.strftime("%Y-%m-%d")
        self.spent = 0

    async def close(self) -> None:
        await self.http.aclose()

    def body(self, system: str, prompt: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        s = self.settings
        body: dict[str, Any] = {
            "model": s.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": s.max_tokens,
        }
        if s.structured_output == "json_schema" and schema:
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        elif s.structured_output != "off":
            body["response_format"] = {"type": "json_object"}
        if s.thinking != "omit":
            body["thinking"] = {"type": s.thinking}
        if s.thinking == "enabled":
            body["reasoning_effort"] = s.reasoning_effort
        else:
            # Sampling settings may not apply while thinking (ECNU's docs).
            body["temperature"] = s.temperature
        return body

    async def complete(
        self, system: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> tuple[str, int, int]:
        """The answer and the input and output tokens it took."""
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            log.info("LLM tokens on %s: %d", self.day, self.spent)
            self.day, self.spent = today, 0
        limit = self.settings.tokens_per_day
        if limit and self.spent >= limit:
            raise BudgetExhausted(f"{self.spent} tokens today")
        body = self.body(system, prompt, schema)
        for _ in range(BUSY_TRIES):
            res = await self.http.post("/chat/completions", json=body)
            if res.status_code != 429:
                break
            await self.sleep(BUSY_WAIT)
        if res.status_code != 200:
            raise LLMError(f"HTTP {res.status_code}: {res.text[:200]}")
        data = res.json()
        usage = data.get("usage") or {}
        tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        self.spent += sum(tokens)
        return data["choices"][0]["message"]["content"], *tokens


class Embedder:
    def __init__(self, base_url: str, api_key: str, model: str):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=120)
        self.model = model

    async def close(self) -> None:
        await self.http.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        res = await self.http.post("/embeddings", json={"model": self.model, "input": texts})
        if res.status_code != 200:
            raise LLMError(f"HTTP {res.status_code}: {res.text[:200]}")
        return [d["embedding"] for d in sorted(res.json()["data"], key=lambda d: d["index"])]
