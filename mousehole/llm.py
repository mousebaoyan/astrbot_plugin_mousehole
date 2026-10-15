"""The LLM and the embedding model, called over the OpenAI-compatible API directly rather than
through AstrBot's providers, which drop per-request options such as the response format and
thinking (design §3). Replay uses these same clients with the same settings."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Too many requests at once: ECNU allows 3 per model and person, shared with other plugins, and
# turns the rest away without charging. Tried for 10 minutes: a call that holds a place while
# thinking may take 5.
BUSY_WAIT = 15
BUSY_TRIES = 40
# A dropped connection, an answer broken off or a server error: asked again after these many
# seconds. A call cut off midway is barely charged.
RETRY_WAITS = (30, 120, 300)


class LLMError(Exception):
    """An answer asking again won't put right: the segment is lost."""


class Truncated(LLMError):
    """The answer reached max_tokens before its JSON was whole, which was paid for all the
    same."""

    def __init__(self, input_tokens: int, output_tokens: int):
        super().__init__(f"the answer reached max_tokens ({output_tokens})")
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Unavailable(Exception):
    """No answer to be had for now: the batch waits and tries again (design §8.2)."""


class BudgetExhausted(Unavailable):
    """Today's tokens are used up."""


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
    max_tokens: int = 32768
    # How long nothing may come: answers stream, however long they take.
    timeout_seconds: int = 120
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
            # Streamed, so a long answer is never cut off by the timeout: at ECNU's 20 or so tokens
            # a second one may take minutes, and a call cut off is charged all the same.
            "stream": True,
            "stream_options": {"include_usage": True},
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
        busy = failures = 0
        while True:
            try:
                async with self.http.stream("POST", "/chat/completions", json=body) as res:
                    text = ""
                    if res.status_code == 200:
                        answer, usage, finish = await read(res)
                    else:
                        text = (await res.aread()).decode(errors="replace")
            except httpx.TransportError as e:
                problem = repr(e)
            else:
                if res.status_code == 200 and finish is not None:
                    break
                if res.status_code == 429:
                    busy += 1
                    if busy == BUSY_TRIES:
                        raise Unavailable(f"HTTP 429 for {BUSY_WAIT * BUSY_TRIES} seconds")
                    await self.sleep(BUSY_WAIT)
                    continue
                if res.status_code in (401, 403):
                    # The key or the IP turned away: nothing to do with the segment.
                    raise Unavailable(f"HTTP {res.status_code}: {text[:200]}")
                if 400 <= res.status_code < 500:
                    raise LLMError(f"HTTP {res.status_code}: {text[:200]}")
                problem = f"HTTP {res.status_code} {text[:200]}" if text else "answer broken off"
            if failures == len(RETRY_WAITS):
                raise Unavailable(problem)
            wait = RETRY_WAITS[failures]
            log.warning("The LLM failed (%s); asking again in %d seconds", problem, wait)
            await self.sleep(wait)
            failures += 1
        tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        self.spent += sum(tokens)
        if finish == "length":
            # Thought all it may: another try at the same would too.
            raise Truncated(*tokens)
        return answer, *tokens


async def read(res: httpx.Response) -> tuple[str, dict, str | None]:
    """A streamed answer, its usage, which comes with the last chunk, and why it finished: None
    when the stream stopped before the answer did."""
    parts, usage, finish = [], {}, None
    async for line in res.aiter_lines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line.removeprefix("data: "))
        usage = chunk.get("usage") or usage
        for c in chunk["choices"]:
            parts.append(c["delta"].get("content") or "")
            finish = c.get("finish_reason") or finish
    return "".join(parts), usage, finish


class Embedder:
    def __init__(self, base_url: str, api_key: str, model: str):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=120)
        self.model = model

    async def close(self) -> None:
        await self.http.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        res = await self.http.post("/embeddings", json={"model": self.model, "input": texts})
        if res.status_code in (401, 403) or res.status_code >= 500:
            raise Unavailable(f"HTTP {res.status_code}: {res.text[:200]}")
        if res.status_code != 200:
            raise LLMError(f"HTTP {res.status_code}: {res.text[:200]}")
        return [d["embedding"] for d in sorted(res.json()["data"], key=lambda d: d["index"])]
