"""链接卡片 (design §9): a link to a thread in a group gets its title and replies under it, which
QQ shows no preview of."""

import asyncio
import random
import re
import time
from collections.abc import Callable
from typing import Any

from .archive import plain
from .forum import Forum, ForumError
from .qq import QQ, REPLY_DELAY, fill, joined

# Threads a message gets cards for at most.
PER_MESSAGE = 2
# Seconds before a thread gets a card in the same group again.
AGAIN_AFTER = 1800


def card(t: dict[str, Any], templates: dict[str, str], site: str) -> list[dict]:
    """`templates`: the cards settings, one template for questions and one for the rest."""
    question = t["kind"] == "question"
    return fill(
        templates["question" if question else "other"],
        board=t["board"]["name"],
        title=t["title"],
        author=t["author"]["display_name"],
        replies=f"{t['reply_count']} 条回复" if t["reply_count"] else "还没有回复",
        status=("✅ 已解决" if t["solved"] else "待解答") if question else "",
        views=t["view_count"],
        forum=site,
    )


class Cards:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        *,
        site: str,
        templates: dict[str, str],
        clock: Callable[[], float] = time.time,
    ):
        self.forum = forum
        self.qq = qq
        self.site = site.rstrip("/")
        self.templates = templates
        self.link = re.compile(re.escape(self.site) + r"/t/(\d+)")
        self.clock = clock
        # When each (group, thread) got its card last.
        self.shown: dict[tuple[int, int], float] = {}

    async def saw(self, raw: dict[str, Any]) -> None:
        """A group message: cards for the threads it links to that its sender can read."""
        if raw["user_id"] == raw["self_id"]:
            return
        group = raw["group_id"]
        now = self.clock()
        self.shown = {k: t for k, t in self.shown.items() if now - t < AGAIN_AFTER}
        threads = dict.fromkeys(int(n) for n in self.link.findall(plain(raw["message"])))
        cards = []
        for thread in [t for t in threads if (group, t) not in self.shown][:PER_MESSAGE]:
            try:
                t = await self.forum.call("GET", f"/threads/{thread}", act_as=raw["user_id"])
            except ForumError as e:
                # Gone, or the sender can't read it: its title isn't shown either.
                if e.status not in (403, 404):
                    raise
                continue
            if t["status"] == "visible":
                cards.append(card(t, self.templates, self.site))
                self.shown[group, thread] = now
        if not cards:
            return
        await asyncio.sleep(random.uniform(*REPLY_DELAY))
        await self.qq.send_group(
            group,
            [{"type": "reply", "data": {"id": str(raw["message_id"])}}, *joined(cards, "\n\n")],
        )
