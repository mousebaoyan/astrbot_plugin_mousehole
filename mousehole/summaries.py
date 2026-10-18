"""AI 总结 (design §8.12): the forum names busy threads gone quiet; their public floors go to the
LLM, and what it makes of them goes back to the forum (forum design §8.18)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import judge
from .archive import Complete
from .forum import Forum, ForumError
from .llm import LLMError, Unavailable

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Seconds between rounds, and threads a round writes at most, one after another.
ROUND = 1800
PER_ROUND = 5


class Summaries:
    def __init__(
        self,
        forum: Forum,
        complete: Complete,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.forum = forum
        self.complete = complete
        self.sleep = sleep
        # (thread, replies) the LLM made nothing of: not paid for again until the thread grows.
        self.failed: set[tuple[int, int]] = set()

    async def run(self) -> None:
        while True:
            await self.sleep(ROUND)
            try:
                await self.round()
            except Unavailable as e:
                # Down, or today's tokens are spent: the next round asks again.
                log.warning("AI 总结 waits: %s", e)
            except Exception:
                log.exception("AI 总结 failed")

    async def round(self) -> None:
        due = await self.forum.call("GET", "/bot/summaries/due", params={"limit": PER_ROUND})
        for item in due["items"]:
            key = (item["thread_id"], item["replies"])
            if key in self.failed:
                continue
            try:
                await self.write(item["thread_id"])
            except (LLMError, ValueError) as e:
                log.warning("AI 总结 of thread %s failed: %s", item["thread_id"], e)
                self.failed.add(key)

    async def write(self, thread: int) -> None:
        try:
            source = await self.forum.call("GET", f"/bot/summaries/{thread}/source")
        except ForumError as e:
            # Gone, or turned off by a moderator, since it was named.
            if e.status != 404:
                raise
            return
        posts = judge.summary_floors(source["posts"])
        answer, _, _ = await self.complete(
            judge.SUMMARY, judge.summary_prompt(source, posts), judge.SUMMARY_SCHEMA
        )
        summary = judge.summary_of(judge.extract_json(answer), {p["floor"] for p in posts})
        if summary is None:
            raise ValueError("no point with floors it was given")
        body = summary | {"replies": source["replies"]}
        try:
            await self.forum.call("PUT", f"/bot/summaries/{thread}", body=body)
        except ForumError as e:
            if e.status not in (404, 409):
                raise
