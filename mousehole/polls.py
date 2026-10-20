"""新投票 (design §9): each evening the groups are told of the polls put up on the forum that day
that can still be voted on, so that members come and vote (forum design §8.6)."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .bounties import FIRST_SINCE, next_round
from .files import write_file
from .forum import Forum
from .qq import QQ, fill, joined
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Polls a message lists at most, the first put up first.
MOST = 5


def message(found: list[dict[str, Any]], templates: dict[str, str], site: str) -> list[dict]:
    """The polls, numbered, with how many voted and their links. `templates`: the polls settings,
    with the message's and each poll's."""
    items = [
        fill(
            templates["item"],
            n=i,
            title=p["thread"]["title"],
            voters=p["voters"],
            url=p["url"],
            forum=site,
        )
        for i, p in enumerate(found[:MOST], 1)
    ]
    return fill(templates["template"], items=joined(items, "\n"), count=len(found), forum=site)


class Polls:
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
        # With the bounties, so the groups hear from the forum once in the evening.
        while True:
            await self.sleep(next_round(self.clock()) - self.clock())
            try:
                await self.round()
            except Exception:
                # Asked from the same time tomorrow: what was missed comes then, if still open.
                log.exception("新投票 failed")

    async def round(self) -> None:
        now = int(self.clock())
        since = (
            json.loads(self.state.read_text())["since"]
            if self.state.exists()
            else now - FIRST_SINCE
        )
        params = {"since": since, "until": now}
        found = (await self.forum.call("GET", "/bot/polls", params=params))["items"]
        if found:
            for group in self.groups or sorted(self.sync.groups):
                await self.qq.send_group(group, message(found, self.templates, self.site))
        write_file(self.state, json.dumps({"since": now}))
