"""Group members to the forum (design §6). Only NapCat's cache is read, never QQ's servers."""

import asyncio
import logging
import time
from typing import Any

from .forum import Forum
from .qq import QQ

log = logging.getLogger("astrbot.mousehole")

FULL_EVERY = 3600


def delta(notice: dict[str, Any]) -> dict[str, Any] | None:
    """The member record a QQ notice comes to (forum API §4.2), or None for one it doesn't."""
    user = notice.get("user_id")
    match notice["notice_type"], notice.get("sub_type"):
        case "group_increase", _:
            return {"user_id": user, "in_group": True}
        case "group_decrease", "leave" | "kick" as reason:
            return {"user_id": user, "in_group": False, "left_reason": reason}
        case "group_decrease", _:
            # The bot itself was kicked, or the group disbanded: an admin stops the group.
            log.warning("Group %s: bot left (%s)", notice["group_id"], notice.get("sub_type"))
            return None
        case "group_admin", "set":
            return {"user_id": user, "role": "admin"}
        case "group_admin", "unset":
            return {"user_id": user, "role": "member"}
        case "group_card", _:
            return {"user_id": user, "card": notice["card_new"]}
        case "notify", "title":
            return {"user_id": user, "title": notice["title"]}
    return None


class Sync:
    def __init__(self, forum: Forum, qq: QQ, *, members: bool = True):
        self.forum = forum
        self.qq = qq
        # Off, only the list of synced groups is kept current, which the rest goes by.
        self.members = members
        # Synced groups, the primary one first.
        self.groups: list[int] = []
        # When each member last spoke, as seen here: NapCat's cache only learns it on a refetch.
        self.spoke: dict[tuple[int, int], int] = {}

    async def refresh_groups(self) -> None:
        items = (await self.forum.call("GET", "/bot/groups"))["items"]
        self.groups = [g["group_id"] for g in items]

    def saw(self, group: int, user: int, at: int) -> None:
        self.spoke[group, user] = at

    async def full(self, group: int) -> None:
        observed = int(time.time())
        members = await self.qq.call("get_group_member_list", group_id=group)
        for m in members:
            spoke = self.spoke.get((group, m["user_id"]), 0)
            m["last_sent_time"] = max(m.get("last_sent_time", 0), spoke)
        result = await self.forum.call(
            "PUT",
            f"/bot/groups/{group}/members",
            body={"observed_at": observed, "members": members},
            compress=True,
        )
        if result["held"]:
            log.warning("Group %s: the forum held a full sync that lost many members", group)
        info = await self.qq.call("get_group_info", group_id=group)
        await self.forum.call(
            "PUT", f"/bot/groups/{group}/info", body={**info, "observed_at": observed}
        )

    async def run(self) -> None:
        while True:
            try:
                await self.refresh_groups()
                for group in self.groups if self.members else []:
                    await self.full(group)
            except Exception:
                log.exception("Member sync failed")
            await asyncio.sleep(FULL_EVERY)

    async def notice(self, notice: dict[str, Any]) -> None:
        group = notice.get("group_id")
        record = delta(notice) if self.members and group in self.groups else None
        if record is None:
            return
        if notice["notice_type"] == "group_increase":
            # NapCat refetches the list before it tells of a newcomer, so they come with a name.
            members = await self.qq.call("get_group_member_list", group_id=group)
            record |= next((m for m in members if m["user_id"] == record["user_id"]), {})
        await self.forum.call(
            "PATCH",
            f"/bot/groups/{group}/members",
            body={"observed_at": notice["time"], "members": [record]},
        )
