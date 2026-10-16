"""入群欢迎 (design §9): newcomers to a group, a few minutes' worth at a time, welcomed with
the words the maintainer set."""

import asyncio
import logging
import time
from collections.abc import Callable

from .qq import QQ
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Seconds from the first newcomer to the welcome: those joining meanwhile get the same one.
WAIT = 600
MENTIONS = 20


def welcome_message(template: str, users: list[int], site: str) -> list[dict]:
    """The template with `{at}` as @ for each newcomer and `{forum}` as the forum's address."""
    at = []
    for user in users:
        at += [{"type": "at", "data": {"qq": str(user)}}, {"type": "text", "data": {"text": " "}}]
    segments = []
    for i, part in enumerate(template.replace("{forum}", site).split("{at}")):
        if i:
            segments += at[:-1]
        if part:
            segments.append({"type": "text", "data": {"text": part}})
    return segments


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
            if now - since >= WAIT:
                del self.waiting[group]
                await self.qq.send_group(group, welcome_message(self.template, users, self.site))
