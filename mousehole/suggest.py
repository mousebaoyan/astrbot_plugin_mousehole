"""猜你想问 (design §8.11): a question asked in a group that a thread on the forum answers
already gets the thread there at once."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from . import judge
from .archive import ROUND, Complete, Message, Notify, plain
from .forum import Forum, ForumError
from .similar import Index

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# What a message must have to be looked at as a question: no LLM is asked for the rest.
QUESTION_WORDS = (
    "?", "？", "吗", "嘛", "呢", "怎么", "怎样", "如何", "哪", "多少", "几", "什么", "啥",
    "为什么", "为啥", "能不能", "可不可以", "是不是", "有没有", "要不要", "会不会", "求",
)  # fmt: skip
MIN_CHARS = 6
# Asked of someone (你, not 你们): between people, which no thread answers.
OF_SOMEONE = re.compile("你(?!们)")
# Candidates for a question, by meaning alone and only those this alike: on October's messages
# against 144 threads, 0.6 let through 30 questions a day, most of them chatter, and 0.65 seven,
# a few asked again; the literal search would bring in most of the chatter.
CANDIDATES = 3
SIMILARITY = 0.65
# Replies a group gets a round at most, so that the bot doesn't flood it.
REPLIES_A_ROUND = 2
# Seconds before a thread is told to a group again.
AGAIN_AFTER = 6 * 3600


def asks(text: str) -> bool:
    return (
        len(text) >= MIN_CHARS
        and any(w in text for w in QUESTION_WORDS)
        and not OF_SOMEONE.search(text)
    )


class Suggest:
    def __init__(
        self,
        forum: Forum,
        complete: Complete,
        index: Index,
        notify: Notify,
        *,
        site: str,
        prefixes: list[str],
        clock: Callable[[], float] = time.time,
    ):
        self.forum = forum
        self.complete = complete
        self.index = index
        self.notify = notify
        self.site = site.rstrip("/")
        self.prefixes = tuple(p for p in prefixes if p)
        self.clock = clock
        # Questions asked since the last round, by group.
        self.waiting: dict[int, list[Message]] = {}
        # When each (group, thread) was told last.
        self.told: dict[tuple[int, int], float] = {}
        # Forum user ID by QQ number; None for those the forum doesn't know.
        self.users: dict[int, int | None] = {}

    def add(self, group: int, m: Message, bot: int) -> None:
        text = text_of(m)
        if m.user != bot and not (self.prefixes and text.startswith(self.prefixes)) and asks(text):
            self.waiting.setdefault(group, []).append(m)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(ROUND)
            try:
                await self.round()
            except Exception:
                # The questions of this round go untold; the next round is on its own.
                log.exception("猜你想问 failed")

    async def round(self) -> None:
        waiting, self.waiting = self.waiting, {}
        now = self.clock()
        self.told = {k: t for k, t in self.told.items() if now - t < AGAIN_AFTER}
        asked = []
        for group, messages in waiting.items():
            for m in messages:
                if found := await self.threads_for(group, m):
                    asked.append((group, m, found))
        if not asked:
            return
        prompt = judge.suggest_prompt([(text_of(m), found) for _, m, found in asked])
        answer, _, _ = await self.complete(judge.SUGGEST, prompt, judge.SUGGEST_SCHEMA)
        try:
            raw = judge.extract_json(answer)
        except ValueError:
            log.warning("The LLM's answer for 猜你想问 is not JSON")
            return
        picked = judge.suggestions(raw, [len(found) for _, _, found in asked])
        replies: Counter[int] = Counter()
        for i, (group, m, found) in enumerate(asked):
            threads = [
                found[t] for t in picked.get(i, []) if (group, found[t]["id"]) not in self.told
            ]
            if not threads or replies[group] == REPLIES_A_ROUND:
                continue
            await self.notify(group, suggestion(m, threads, self.site))
            replies[group] += 1
            for t in threads:
                self.told[group, t["id"]] = now

    async def threads_for(self, group: int, m: Message) -> list[dict]:
        """The threads that may answer it: with replies, not the asker's own, not told lately."""
        asker = await self.user(m.user)
        found = []
        for thread in await self.index.nearest(text_of(m), CANDIDATES, SIMILARITY):
            if (group, thread) in self.told:
                continue
            try:
                t = await self.forum.call("GET", f"/threads/{thread}")
            except ForumError as e:
                if e.status != 404:
                    raise
                continue
            if t["reply_count"] and t["author"].get("id") != asker:
                t["created"] = datetime.fromtimestamp(t["created_at"]).strftime("%Y-%m-%d")
                found.append(t)
        return found

    async def user(self, qq: int) -> int | None:
        if qq not in self.users:
            try:
                self.users[qq] = (await self.forum.call("GET", f"/users/qq:{qq}"))["id"]
            except ForumError as e:
                if e.status != 404:
                    raise
                self.users[qq] = None
        return self.users[qq]


def text_of(m: Message) -> str:
    return " ".join(plain(m.segments).split())


def suggestion(m: Message, threads: list[dict], site: str) -> list[dict]:
    """The reply to the question: the threads, numbered, with their links."""
    lines = ["猜你想问："]
    lines += [f"{i}. {t['title']}\n{site}/t/{t['id']}" for i, t in enumerate(threads, 1)]
    return [
        {"type": "reply", "data": {"id": str(m.id)}},
        {"type": "text", "data": {"text": "\n".join(lines)}},
    ]
