"""入群欢迎 (design §9): newcomers to a group, a few minutes' worth at a time and hours
apart, welcomed with the words the maintainer set."""

import asyncio
import logging
import time
from collections.abc import Callable

from .qq import QQ, at, fill, joined
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Seconds from the first newcomer to the welcome: those joining meanwhile get the same one.
WAIT = 600
# Seconds from one welcome in a group to the next: newcomers meanwhile wait and share it.
GAP = 7200
MENTIONS = 20


def welcome_message(template: str, users: list[int], site: str) -> list[dict]:
    return fill(template, at=joined([[at(u)] for u in users], " "), forum=site)


class Welcome:
    def __init__(
        self,
        qq: QQ,
        sync: Sync,
        *,
        groups: list[int],
        template: str,
        site: str,
        clock: Callable[[], float] = time.time,
    ):
        """`groups`: where newcomers are welcomed, empty for every synced group."""
        self.qq = qq
        self.sync = sync
        self.groups = groups
        self.template = template
        self.site = site
        self.clock = clock
        # Newcomers waiting for their welcome, by group, with when the first of them came.
        self.waiting: dict[int, tuple[float, list[int]]] = {}
        # When each group may next be welcomed into.
        self.next_at: dict[int, float] = {}

    def joined(self, group: int, user: int, bot: int) -> None:
        """Someone came into a group; those in another synced group already aren't new."""
        if group not in (self.groups or self.sync.groups) or user == bot:
            return
        if any(user in self.sync.roster.get(g, set()) for g in self.sync.groups if g != group):
            return
        since, users = self.waiting.get(group, (self.clock(), []))
        if user not in users and len(users) < MENTIONS:
            self.waiting[group] = (since, [*users, user])

    async def run(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.send_due()
            except Exception:
                log.exception("入群欢迎 failed")

    async def send_due(self) -> None:
        now = self.clock()
        for group, (since, users) in list(self.waiting.items()):
            if now >= max(since + WAIT, self.next_at.get(group, 0.0)):
                del self.waiting[group]
                self.next_at[group] = now + GAP
                await self.qq.send_group(group, welcome_message(self.template, users, self.site))
