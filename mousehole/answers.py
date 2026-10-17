"""新回答提醒 (design §9): a question asked in a group and archived is answered on the forum, and
the asker is told in the group, quoting the question."""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .forum import Forum
from .qq import QQ, at, fill
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Rounds every half hour from 9:00 to 23:00 by the machine's clock: what is answered at night
# waits for the morning.
FIRST, LAST = 9, 23
# Seconds an answer is left for the asker to read on the forum before they are told; answers
# coming close together are told in one reminder.
SETTLE = 1800
# Reminders a group gets in an hour at most; the rest wait for later rounds.
PER_HOUR = 3
# Seconds before a question can be told of again.
AGAIN = 24 * 3600
# Seconds after being asked a question is told of: later the asker has moved on.
FRESH = 48 * 3600
# Seconds waited before each reminder, so they don't come one after another.
GAP = (60, 300)


def next_round(now: float) -> float:
    """The first round after `now`."""
    at = datetime.fromtimestamp(now)
    due = at.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=30 * (at.minute // 30 + 1)
    )
    if due.hour < FIRST:
        due = due.replace(hour=FIRST, minute=0)
    elif due > due.replace(hour=LAST, minute=0):
        due = (due + timedelta(days=1)).replace(hour=FIRST, minute=0)
    return due.timestamp()


def reminder(item: dict[str, Any], template: str, site: str) -> list[dict]:
    """Quoting the question; NapCat leaves the quote out when it no longer has the message."""
    return [
        {"type": "reply", "data": {"id": str(item["source"]["message_id"])}},
        *fill(
            template,
            at=[at(item["asker"]["qq"])],
            title=item["thread"]["title"],
            count=item["count"],
            url=item["url"],
            forum=site,
        ),
    ]


class Answers:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        template: str,
        site: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.template = template
        self.site = site
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

    def save(self, since: int, told: dict[str, list]) -> None:
        part = self.state.with_suffix(".part")
        part.write_text(json.dumps({"since": since, "told": told}))
        part.replace(self.state)

    async def round(self) -> None:
        now = self.clock()
        until = int(now - SETTLE)
        if not self.state.exists():
            self.save(until, {})
            return
        state = json.loads(self.state.read_text())
        since = state["since"]
        # Thread -> [group, time] of each reminder in the last day.
        told = {t: v for t, v in state["told"].items() if now - v[1] < AGAIN}
        params = {"since": since, "until": until}
        items = (await self.forum.call("GET", "/bot/answers", params=params))["items"]
        held = False
        for item in items:
            group = item["source"]["chat_id"]
            thread = str(item["thread"]["id"])
            roster = self.sync.roster.get(group, set())
            now = self.clock()
            if (
                thread in told
                or now - item["thread"]["created_at"] > FRESH
                or group not in self.sync.groups
                or item["asker"]["qq"] not in roster
            ):
                continue
            if sum(g == group and now - t < 3600 for g, t in told.values()) >= PER_HOUR:
                log.info("新回答提醒: group %s is full this hour, %s waits", group, item["url"])
                held = True
                continue
            await self.sleep(random.uniform(*GAP))
            await self.qq.send_group(group, reminder(item, self.template, self.site))
            told[thread] = [group, self.clock()]
            # Kept as each goes: a round cut short is asked again and goes on where it was.
            self.save(since, told)
        # Those held come again in the next round, asked from the same time.
        self.save(since if held else until, told)
