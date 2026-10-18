"""悬赏 (design §9): the forum puts up questions asked there that nobody answered for a day for a
bounty (forum design §8.19); each evening the groups are told of the new ones."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .files import write_file
from .forum import Forum
from .qq import QQ, fill, joined
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Each day at 20:00 by the machine's clock.
HOUR = 20
# Questions a message lists at most, the first put up first.
MOST = 5
# Seconds looked back the first time, with nothing told yet.
FIRST_SINCE = 86_400


def next_round(now: float) -> float:
    at = datetime.fromtimestamp(now)
    due = at.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    return (due if due > at else due + timedelta(days=1)).timestamp()


def message(found: dict[str, Any], templates: dict[str, str], site: str) -> list[dict]:
    """The questions, numbered, with their links, and the coins for answering. `templates`: the
    bounty settings, with the message's and each question's."""
    items = [
        fill(templates["item"], n=i, title=b["thread"]["title"], url=b["url"], forum=site)
        for i, b in enumerate(found["items"][:MOST], 1)
    ]
    return fill(
        templates["template"],
        items=joined(items, "\n"),
        reply=found["reward"]["reply"],
        answer=found["reward"]["answer"],
        forum=site,
    )


class Bounties:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        groups: list[int],
        templates: dict[str, str],
        site: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        """`groups`: where the message goes, empty for every synced group."""
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.groups = groups
        self.templates = templates
        self.site = site
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        while True:
            await self.sleep(next_round(self.clock()) - self.clock())
            try:
                await self.round()
            except Exception:
                # Asked from the same time tomorrow: what was missed comes then, if still unanswered.
                log.exception("悬赏 failed")

    async def round(self) -> None:
        now = int(self.clock())
        since = (
            json.loads(self.state.read_text())["since"]
            if self.state.exists()
            else now - FIRST_SINCE
        )
        params = {"since": since, "until": now}
        found = await self.forum.call("GET", "/bot/bounties", params=params)
        if found["items"]:
            for group in self.groups or sorted(self.sync.groups):
                await self.qq.send_group(group, message(found, self.templates, self.site))
        write_file(self.state, json.dumps({"since": now}))
