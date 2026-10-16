"""新回答提醒 (design §9): a question asked in a group and archived is answered on the forum, and
the asker is told in the group, quoting the question."""

import asyncio
import json
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .forum import Forum
from .qq import QQ
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Rounds by the machine's clock, none at night: what is answered then waits for the morning.
HOURS = (10, 16, 22)
# Reminders a group gets a round at most, those with most answers first.
PER_ROUND = 8
# Seconds a round's reminders are spread over, so they don't come one after another.
SPREAD = 2 * 3600


def next_round(now: float) -> float:
    """The first round after `now`."""
    at = datetime.fromtimestamp(now)
    days = (at, at + timedelta(days=1))
    dues = (d.replace(hour=h, minute=0, second=0, microsecond=0) for d in days for h in HOURS)
    return min(due for due in dues if due > at).timestamp()


def reminder(item: dict[str, Any]) -> list[dict]:
    """Quoting the question; NapCat leaves the quote out when it no longer has the message."""
    text = (
        f" 你问的「{item['thread']['title']}」在鼠洞上有 {item['count']} 条新回答：\n{item['url']}"
    )
    return [
        {"type": "reply", "data": {"id": str(item["source"]["message_id"])}},
        {"type": "at", "data": {"qq": str(item["asker"]["qq"])}},
        {"type": "text", "data": {"text": text}},
    ]


class Answers:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        while True:
            await self.sleep(next_round(self.clock()) - self.clock())
            try:
                await self.round()
            except Exception:
                # What it missed comes in the next round: that one asks from this one's start.
                log.exception("新回答提醒 failed")

    def save(self, since: float) -> None:
        part = self.state.with_suffix(".part")
        part.write_text(json.dumps({"since": since}))
        part.replace(self.state)

    async def round(self) -> None:
        now = self.clock()
        if not self.state.exists():
            self.save(now)
            return
        since = json.loads(self.state.read_text())["since"]
        params = {"since": int(since), "until": int(now)}
        items = (await self.forum.call("GET", "/bot/answers", params=params))["items"]
        # Saved before sending: if the plugin stops midway, the rest go untold, not twice.
        self.save(now)
        told: Counter[int] = Counter()
        picked = []
        for item in items:
            group = item["source"]["chat_id"]
            roster = self.sync.roster.get(group, set())
            if group not in self.sync.groups or item["asker"]["qq"] not in roster:
                continue
            if told[group] == PER_ROUND:
                log.info("新回答提醒: group %s is full this round, %s untold", group, item["url"])
                continue
            told[group] += 1
            picked.append(item)
        # Each in a slot of its own, at a random moment in it.
        for i, item in enumerate(picked):
            at = now + SPREAD * (i + random.random()) / len(picked)
            await self.sleep(at - self.clock())
            await self.qq.send_group(item["source"]["chat_id"], reminder(item))
