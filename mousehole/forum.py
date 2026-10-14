"""The forum's REST API (docs/api.md in the mousehole repo)."""

import gzip
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ForumError(Exception):
    """An error the forum answered with; `code` is what to act on, `message` is for people."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any]):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Forum:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=30,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        act_as: int | None = None,
        operator: int | None = None,
        key: str | None = None,
        compress: bool = False,
    ) -> Any:
        headers = {}
        if act_as is not None:
            headers["X-Act-As"] = f"qq:{act_as}"
        if operator is not None:
            headers["X-Operator"] = f"qq:{operator}"
        if key is not None:
            headers["Idempotency-Key"] = key
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, ensure_ascii=False).encode()
            if compress:
                headers["Content-Encoding"] = "gzip"
                content = gzip.compress(content)
        res = await self.http.request(method, path, content=content, params=params, headers=headers)
        if res.status_code == 204:
            return None
        if res.is_error:
            raise error_of(res)
        return res.json()

    async def stream(
        self, path: str, params: dict[str, Any], last_id: int
    ) -> AsyncIterator[tuple[int, str, Any]]:
        """Server-sent events as (id, type, data). The forum sends a comment every 20 seconds,
        so a minute without a line means the connection is gone."""
        headers = {"Last-Event-ID": str(last_id)}
        timeout = httpx.Timeout(30, read=60)
        async with self.http.stream(
            "GET", path, params=params, headers=headers, timeout=timeout
        ) as res:
            if res.is_error:
                await res.aread()
                raise error_of(res)
            event: dict[str, str] = {}
            async for line in res.aiter_lines():
                if line == "":
                    if "data" in event:
                        yield int(event["id"]), event["event"], json.loads(event["data"])
                    event = {}
                elif not line.startswith(":"):
                    name, _, value = line.partition(":")
                    event[name] = value.removeprefix(" ")


def error_of(res: httpx.Response) -> ForumError:
    try:
        data = res.json()
        error = data.pop("error")
        return ForumError(res.status_code, error["code"], error["message"], data)
    except (ValueError, KeyError, TypeError, AttributeError):
        return ForumError(res.status_code, "bad_response", res.text[:200], {})
