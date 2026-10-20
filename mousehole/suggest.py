"""猜你想问 (design §8.11): a question asked in a group that a thread on the forum answers
already gets the thread there at once."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import judge
from .archive import Complete, Message, Notify, plain, shown_text, shows
from .forum import Forum, ForumError
from .qq import fill, joined
from .similar import Index

# (query, documents): each document's score
Rerank = Callable[[str, list[str]], Awaitable[list[float]]]

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# What a message must have to be looked at as a question: no LLM is asked for the rest.
QUESTION_WORDS = (
    "?", "？", "吗", "嘛", "呢", "怎么", "怎样", "如何", "哪", "多少", "几", "什么", "啥",
    "为什么", "为啥", "能不能", "可不可以", "是不是", "有没有", "要不要", "会不会", "求",
)  # fmt: skip
MIN_CHARS = 6
# Asked of someone (你, not 你们): between people, which no thread answers.
OF_SOMEONE = re.compile("你(?!们)")
# Candidates for a question: by meaning alone, as the literal search would bring in most of the
# chatter, the NEAREST threads at least SIMILARITY alike; then the reranker scores the question
# against each one's title and the start of its floors, which tells a thread on the same question
# from one on the same topic far better, and the first CANDIDATES scoring at least RERANKED go to
# the LLM. Of 48 questions in 11 days a thread answers, the vector alone at 0.7 leaves 18 to the
# LLM, with 27 to 32 questions a day going to it; 0.6 and then 0.3 leaves 43, with 56 to 63.
SIMILARITY = 0.6
NEAREST = 10
RERANKED = 0.3
CANDIDATES = 3
RERANK_FLOOR_CHARS = 200
RERANK_CHARS = 1000
# A question goes to the LLM after the BEFORE messages that say anything, said at most
# BEFORE_SECONDS before it, and the one it quotes among the KEPT last of the group: whether it
# asks anything, and what, shows there.
BEFORE = 5
BEFORE_SECONDS = 300
KEPT = 100
# Seconds from a question to the round that looks at it, and at those asked meanwhile: the asker
# is still there, and the bot doesn't answer the instant they ask.
DELAY = 60
# Replies a group gets in any WINDOW seconds at most, so that the bot doesn't flood it.
REPLIES = 2
WINDOW = 600
# Seconds before a thread is told to a group again.
AGAIN_AFTER = 6 * 3600
# Seconds within which others saying the very same is a chorus, a joke going round, not a
# question.
CHORUS = 60


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
        rerank: Rerank,
        index: Index,
        notify: Notify,
        *,
        site: str,
        templates: dict[str, str],
        prefixes: list[str],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.forum = forum
        self.complete = complete
        self.rerank = rerank
        self.index = index
        self.notify = notify
        self.site = site.rstrip("/")
        self.templates = templates
        self.prefixes = tuple(p for p in prefixes if p)
        self.clock = clock
        self.sleep = sleep
        # Set by a question, for a round DELAY after it.
        self.asked = asyncio.Event()
        # Questions asked since the last round, by group, each as the LLM is shown it.
        self.waiting: dict[int, list[tuple[Message, list[str]]]] = {}
        # The latest messages of each group.
        self.recent: dict[int, deque[Message]] = {}
        # When each (group, thread) was told last.
        self.told: dict[tuple[int, int], float] = {}
        # When each group was replied to within the last WINDOW.
        self.replied: dict[int, list[float]] = {}
        # Forum user ID by QQ number; None for those the forum doesn't know.
        self.users: dict[int, int | None] = {}

    def add(self, group: int, m: Message, bot: int) -> None:
        recent = self.recent.setdefault(group, deque(maxlen=KEPT))
        text = text_of(m)
        if m.user != bot and not (self.prefixes and text.startswith(self.prefixes)) and asks(text):
            self.waiting.setdefault(group, []).append((m, lines_of(m, recent, bot)))
            self.asked.set()
        recent.append(m)

    async def run(self) -> None:
        while True:
            await self.asked.wait()
            await self.sleep(DELAY)
            self.asked.clear()
            try:
                await self.round()
            except Exception:
                # The questions of this round go untold; the next round is on its own.
                log.exception("猜你想问 failed")

    async def round(self) -> None:
        waiting, self.waiting = self.waiting, {}
        now = self.clock()
        self.told = {k: t for k, t in self.told.items() if now - t < AGAIN_AFTER}
        self.replied = {g: [t for t in ts if now - t < WINDOW] for g, ts in self.replied.items()}
        asked = []
        for group, questions in waiting.items():
            for m, lines in questions:
                if chorus(m, self.recent[group]):
                    continue
                if found := await self.threads_for(group, m):
                    asked.append((group, m, lines, found))
        told = tokens = 0
        for group, m, lines, found in asked:
            threads, input_tokens, output_tokens = await pick(
                self.complete, self.rerank, lines, text_of(m), found
            )
            tokens += input_tokens + output_tokens
            told += await self.tell(group, m, threads, now)
        if waiting:
            # In numbers only: the questions are the members' words.
            log.info(
                "猜你想问: %d questions, %d with threads, %d told, %d tokens",
                sum(len(questions) for questions in waiting.values()),
                len(asked),
                told,
                tokens,
            )

    async def tell(self, group: int, m: Message, threads: list[dict], now: float) -> bool:
        """The threads not told lately get to the asker, unless the group was replied to enough
        lately; whether they did."""
        threads = [t for t in threads if (group, t["id"]) not in self.told]
        replied = self.replied.setdefault(group, [])
        if not threads or len(replied) == REPLIES:
            return False
        await self.notify(group, suggestion(m, threads, self.site, self.templates))
        replied.append(now)
        for t in threads:
            self.told[group, t["id"]] = now
        return True

    async def threads_for(self, group: int, m: Message) -> list[dict]:
        """The threads that may answer it, the most alike first: with replies, not the asker's
        own, not told lately; with their floors, as the forum gives them to summarize."""
        asker = await self.user(m.user)
        found: list[dict] = []
        for thread in await self.index.nearest(text_of(m), NEAREST, SIMILARITY):
            if (group, thread) in self.told:
                continue
            try:
                t = await self.forum.call("GET", f"/threads/{thread}")
                if not t["reply_count"] or t["author"].get("id") == asker:
                    continue
                source = await self.forum.call("GET", f"/bot/summaries/{thread}/source")
            except ForumError as e:
                # Gone or merged away; or a moderator turned off its AI summary, which takes the
                # source too. By status alone, a forum without the endpoint would look the same.
                if e.code not in ("thread_not_found", "thread_merged"):
                    raise
                continue
            posts = judge.floors_within(
                source["posts"], judge.SUGGEST_FLOOR_CHARS, judge.SUGGEST_CHARS
            )
            found.append(
                {
                    "id": thread,
                    "title": t["title"],
                    "created": datetime.fromtimestamp(t["created_at"]).strftime("%Y-%m-%d"),
                    "source": source,
                    "posts": posts,
                }
            )
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


async def pick(
    complete: Complete, rerank: Rerank, lines: list[str], text: str, found: list[dict]
) -> tuple[list[dict], int, int]:
    """The threads of those found that answer the question, the best first, and the input and
    output tokens the LLM took. Those the reranker scores best go to the LLM, which says first,
    from the messages alone, what is asked, and then which of them answer that."""
    scores = await rerank(text, [rerank_text(t) for t in found])
    ranked = sorted(range(len(found)), key=lambda i: -scores[i])
    candidates = [found[i] for i in ranked if scores[i] >= RERANKED][:CANDIDATES]
    if not candidates:
        return [], 0, 0
    answer, input_tokens, output_tokens = await complete(
        judge.SUGGEST_ASKS, "\n".join(lines), judge.SUGGEST_ASKS_SCHEMA
    )
    asks = judge.asks_of(parsed(answer))
    if not asks:
        return [], input_tokens, output_tokens
    answer, more_input, more_output = await complete(
        judge.SUGGEST_MATCH,
        judge.match_prompt(asks, lines, candidates),
        judge.SUGGEST_MATCH_SCHEMA,
    )
    picked = judge.matches(parsed(answer), candidates)
    return [candidates[t] for t in picked], input_tokens + more_input, output_tokens + more_output


def parsed(answer: str) -> Any:
    try:
        return judge.extract_json(answer)
    except ValueError:
        log.warning("The LLM's answer for 猜你想问 is not JSON")
        return None


def rerank_text(thread: dict) -> str:
    floors = (p["text"][:RERANK_FLOOR_CHARS] for p in thread["source"]["posts"])
    return "\n".join([thread["title"], *floors])[:RERANK_CHARS]


def text_of(m: Message) -> str:
    return " ".join(plain(m.segments).split())


def chorus(m: Message, recent: deque[Message]) -> bool:
    text = text_of(m)
    return any(
        r.user != m.user and abs(r.time - m.time) < CHORUS and text_of(r) == text for r in recent
    )


def lines_of(m: Message, recent: deque[Message], bot: int) -> list[str]:
    """The question as the LLM is shown it: last, after the messages said shortly before it and
    the one it quotes; the asker, the bot and the others by aliases, as the models never see QQ
    numbers or names."""
    said = [r for r in recent if any(shows(s) for s in r.segments)]
    before = [r for r in said if m.time - r.time <= BEFORE_SECONDS][-BEFORE:]
    by_id = {r.id: r for r in recent}
    quoted = by_id.get(m.quoted) if m.quoted is not None else None
    shown = ([quoted] if quoted is not None and quoted not in before else []) + before + [m]
    names = {m.user: "提问人", bot: "bot"}

    def name(user: int) -> str:
        return names.setdefault(user, f"群友{len(names) - 1}")

    lines = []
    for r in shown:
        to = ""
        if r.quoted is not None:
            target = by_id.get(r.quoted)
            to = f" ↩{name(target.user)}" if target else " ↩更早的消息"
        lines.append(f"{name(r.user)}{to}：{shown_text(r, name)}")
    return lines


def suggestion(m: Message, threads: list[dict], site: str, templates: dict[str, str]) -> list[dict]:
    """The reply to the question: the threads, numbered, with their links. `templates`: the
    archive settings, with the reply's and each thread's."""
    items = [
        fill(
            templates["suggest_item"],
            n=i,
            title=t["title"],
            url=f"{site}/t/{t['id']}",
            forum=site,
        )
        for i, t in enumerate(threads, 1)
    ]
    return [
        {"type": "reply", "data": {"id": str(m.id)}},
        *fill(templates["suggest_template"], items=joined(items, "\n"), forum=site),
    ]
