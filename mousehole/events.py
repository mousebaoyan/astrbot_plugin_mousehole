"""The forum's event stream (design §10), and what it sets off: QQ signatures for members who
just activated, lottery results in the groups, and the thread index kept current."""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .forum import Forum, ForumError
from .qq import QQ
from .similar import Index
from .sync import Sync

log = logging.getLogger("astrbot.mousehole")

INDEX_TYPES = ["thread.created", "post.updated", "post.deleted"]
BATCH = 500
# Seconds before each reconnection in a row; then every minute.
RECONNECT = [1, 2, 5, 10, 30, 60]
# A plugin back after a long stop doesn't announce stale draws.
LOTTERY_FRESH = 12 * 3600
# @ in one message, beyond which winners are only named.
MENTIONS = 20


def lottery_message(data: dict) -> list[dict]:
    def text(value: str) -> dict:
        return {"type": "text", "data": {"text": value}}

    segments = [text(f"【抽奖开奖】{data['thread']['title']}")]
    mentioned = 0
    for prize in data["prizes"]:
        segments.append(text(f"\n{prize['name']}"))
        if not prize["winners"]:
            segments.append(text(" 无人中奖"))
        for winner in prize["winners"]:
            if mentioned < MENTIONS:
                segments += [text(" "), {"type": "at", "data": {"qq": str(winner["qq"])}}]
                mentioned += 1
            else:
                segments.append(text(f" {winner['display_name']}"))
    segments.append(text(f"\n共 {data['entry_count']} 人参与\n{data['url']}"))
    return segments


class Events:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        lottery: list[int] | None,
        bio: bool,
        index: Index | None,
    ):
        """`lottery`: the groups draws are announced in, empty for the primary group, None for
        none; `bio`: whether newly activated members get their QQ signatures pushed."""
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.lottery = lottery
        self.index = index
        types = ["member.activated"] if bio else []
        types += ["lottery.drawn"] if lottery is not None else []
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
                if time.time() - at > LOTTERY_FRESH:
                    return
                message = lottery_message(data)
                for group in self.lottery or self.sync.groups[:1]:
                    await self.qq.send_group(group, message)
            case "thread.created" if self.index:
                await self.index.thread_created(data["thread"]["id"])
            case "post.updated" if self.index and data["post"]["floor"] == 1:
                await self.index.put(
                    data["thread"]["id"], data["thread"]["title"], data["post"]["body"]
                )
            case "post.deleted" if self.index and data["floor"] == 1:
                self.index.remove(data["thread_id"])
