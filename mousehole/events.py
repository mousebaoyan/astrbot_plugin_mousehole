"""The forum's event stream (design §10), and what it sets off: QQ signatures for members who
just activated, lottery results and gold badges in the groups, and the thread index kept
current."""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .forum import Forum, ForumError
from .qq import QQ, at, fill, joined, text
from .similar import Index
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

INDEX_TYPES = ["thread.created", "post.updated", "post.deleted"]
BATCH = 500
# Seconds before each reconnection in a row; then every minute.
RECONNECT = [1, 2, 5, 10, 30, 60]
# A plugin back after a long stop doesn't announce stale draws or badges.
FRESH = 12 * 3600
# @ in one message, beyond which winners are only named.
MENTIONS = 20


def badge_message(data: dict, template: str, site: str) -> list[dict]:
    return fill(
        template,
        at=[at(data["user"]["qq"])],
        badge=data["badge"]["name"],
        description=data["badge"]["description"],
        url=data["url"],
        forum=site,
    )


def lottery_message(data: dict, templates: dict[str, str], site: str) -> list[dict]:
    """`templates`: the lottery settings, the message's and each prize's."""
    items = []
    mentioned = 0
    for prize in data["prizes"]:
        winners = []
        for winner in prize["winners"]:
            winners.append(
                [at(winner["qq"])] if mentioned < MENTIONS else [text(winner["display_name"])]
            )
            mentioned += 1
        items.append(
            fill(
                templates["item"],
                prize=prize["name"],
                winners=joined(winners, " ") or "无人中奖",
                forum=site,
            )
        )
    return fill(
        templates["template"],
        title=data["thread"]["title"],
        items=joined(items, "\n"),
        entries=data["entry_count"],
        url=data["url"],
        forum=site,
    )


class Events:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        lottery: list[int] | None,
        badges: bool,
        bio: bool,
        index: Index | None,
        site: str,
        lottery_templates: dict[str, str],
        badge_template: str,
    ):
        """`lottery`: the groups draws are announced in, empty for the primary group, None for
        none; `badges`: whether gold badges are congratulated on, in the groups the member is
        in; `bio`: whether newly activated members get their QQ signatures pushed."""
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.lottery = lottery
        self.index = index
        self.site = site
        self.lottery_templates = lottery_templates
        self.badge_template = badge_template
        types = ["member.activated"] if bio else []
        types += ["lottery.drawn"] if lottery is not None else []
        types += ["badge.granted"] if badges else []
        self.types = ",".join(types + (INDEX_TYPES if index else []))

    def load(self) -> int | None:
        return json.loads(self.state.read_text())["cursor"] if self.state.exists() else None

    def save(self, cursor: int) -> None:
        part = self.state.with_suffix(".part")
        part.write_text(json.dumps({"cursor": cursor}))
        part.replace(self.state)

    async def run(self) -> None:
        cursor = self.load()
        failures = 0
        while True:
            try:
                if cursor is None:
                    # The first start: from now on.
                    cursor = (await self.forum.call("GET", "/bot/events/batch"))["next_after"]
                    self.save(cursor)
                # Caught up in batches first, where each event's time shows.
                while True:
                    params = {"after": cursor, "limit": BATCH, "types": self.types}
                    page = await self.forum.call("GET", "/bot/events/batch", params=params)
                    for e in page["items"]:
                        await self.handle(e["type"], e["data"], e["created_at"])
                        cursor = e["id"]
                        self.save(cursor)
                    if len(page["items"]) < BATCH:
                        break
                failures = 0
                params = {"types": self.types}
                async for event_id, kind, data in self.forum.stream("/bot/events", params, cursor):
                    await self.handle(kind, data, time.time())
                    cursor = event_id
                    self.save(cursor)
                    failures = 0
            except ForumError as e:
                if e.code == "cursor_expired":
                    log.warning("Event cursor %s expired; going on from %s", cursor, e.details)
                    cursor = e.details["latest_id"]
                    self.save(cursor)
                    continue
                log.warning("Event stream: %s", e)
            except httpx.HTTPError as e:
                log.warning("Event stream: %r", e)
            await asyncio.sleep(RECONNECT[min(failures, len(RECONNECT) - 1)])
            failures += 1

    async def handle(self, kind: str, data: dict[str, Any], at: float) -> None:
        """One event; what goes wrong with it is logged and the stream goes on."""
        try:
            await self.act(kind, data, at)
        except Exception:
            log.exception("Handling a %s event failed", kind)

    async def act(self, kind: str, data: dict[str, Any], at: float) -> None:
        match kind:
            case "member.activated":
                qq = data["user"]["qq"]
                info = await self.qq.call("get_stranger_info", user_id=qq)
                if info.get("long_nick"):
                    body = {"bio": info["long_nick"]}
                    await self.forum.call("PUT", f"/bot/users/qq:{qq}/bio", body=body)
            case "lottery.drawn":
                if time.time() - at > FRESH:
                    return
                message = lottery_message(data, self.lottery_templates, self.site)
                for group in self.lottery or self.sync.groups[:1]:
                    await self.qq.send_group(group, message)
            case "badge.granted" if data["badge"]["tier"] == "gold":
                if time.time() - at > FRESH:
                    return
                qq = data["user"]["qq"]
                for group in self.sync.groups:
                    if qq in self.sync.roster.get(group, ()):
                        await self.qq.send_group(
                            group, badge_message(data, self.badge_template, self.site)
                        )
            case "thread.created" if self.index:
                await self.index.thread_created(data["thread"]["id"])
            case "post.updated" if self.index and data["post"]["floor"] == 1:
                await self.index.put(
                    data["thread"]["id"], data["thread"]["title"], data["post"]["body"]
                )
            case "post.deleted" if self.index and data["floor"] == 1:
                self.index.remove(data["thread_id"])
