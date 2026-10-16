"""NapCat through AstrBot's aiocqhttp adapter (design §2)."""

import asyncio
import base64
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

# Seconds between the messages the bot sends of its own accord.
GAP = (3, 6)
# Seconds before a reply: a person answers after a moment, not the instant a message arrives.
REPLY_DELAY = (1, 2)
# For the coordinator the maintainer's other QQ plugins share, when it is there.
GUARD_CONFIG = {
    "recovery_min_seconds": 60,
    "recovery_max_seconds": 180,
    "failure_threshold": 3,
    "failure_cooldown_seconds": 600,
}
# NapCat has no action that makes a group album; the QQ client's own request does it, as in the
# maintainer's digest plugin.
ADD_ALBUM = "QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.AddAlbum"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def at(qq: int) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def merged(parts: list[object]) -> list[dict]:
    """Texts and lists of segments as one message, the texts next to each other as one."""
    segments: list[dict] = []
    for part in parts:
        for s in part if isinstance(part, list) else [text(str(part))]:
            if s["type"] == "text" and segments and segments[-1]["type"] == "text":
                segments[-1] = text(segments[-1]["data"]["text"] + s["data"]["text"])
            elif s != text(""):
                segments.append(s)
    return segments


def fill(template: str, **values: object) -> list[dict]:
    """A message template from the settings (design §9): each `{name}` given becomes its text, or
    its segments when a list; names not given are left as written, so a misspelt one shows."""
    parts = PLACEHOLDER.split(template)
    return merged([values.get(p, f"{{{p}}}") if i % 2 else p for i, p in enumerate(parts)])


def joined(items: list[list[dict]], sep: str) -> list[dict]:
    return merged([part for i, item in enumerate(items) for part in ([sep] if i else []) + [item]])


def protobuf(*fields: tuple[int, int | str | bytes]) -> bytes:
    def varint(n: int) -> bytes:
        out = bytearray()
        while n > 127:
            out.append(n & 127 | 128)
            n >>= 7
        return bytes(out + bytes([n]))

    out = b""
    for number, value in fields:
        if isinstance(value, int):
            out += varint(number << 3) + varint(value)
        else:
            value = value.encode() if isinstance(value, str) else value
            out += varint(number << 3 | 2) + varint(len(value)) + value
    return out


def add_album(group: int, name: str) -> bytes:
    now = datetime.now()
    session = (
        now.strftime("_%m%d%H%M%S")
        + f"{now.microsecond // 1000:03}_{secrets.randbelow(90000) + 10000}"
    )
    info = protobuf((2, str(group)), (3, name), (4, ""), (5, 0))
    return protobuf(
        (1, secrets.randbelow(0x7FFFFFFE) + 1),
        (2, b""),
        (3, b""),
        (4, protobuf((1, info))),
        (5, session),
        (10, protobuf((1, "fc-appid"), (2, "100"))),
    )


class QQ:
    def __init__(self, platform_manager: Any, platform_id: str):
        self.manager = platform_manager
        self.platform_id = platform_id
        self.sending = asyncio.Lock()
        self.next_at = 0.0

    def found(self) -> list[Any]:
        found = [p for p in self.manager.get_insts() if p.meta().name == "aiocqhttp"]
        if self.platform_id:
            found = [p for p in found if p.meta().id == self.platform_id]
        return found

    def platform(self) -> Any:
        found = self.found()
        if len(found) != 1:
            raise RuntimeError(f"expected one aiocqhttp platform, found {len(found)}")
        return found[0]

    async def call(self, action: str, **params: Any) -> Any:
        return await self.platform().bot.call_action(action, **params)

    async def reply(self, event: dict[str, Any], message: list[dict[str, Any]]) -> None:
        """To the chat a message came from, the group or the private one."""
        await asyncio.sleep(random.uniform(*REPLY_DELAY))
        await self.platform().bot.send(event, message)

    async def online(self) -> bool:
        return (await self.call("get_status"))["online"]

    async def send_group(self, group: int, message: list[dict[str, Any]]) -> None:
        await self.queued(lambda: self.call("send_group_msg", group_id=group, message=message))

    async def album(self, group: int, name: str) -> str:
        """The group album of this name, made if the group has none."""
        if album := await self.find_album(group, name):
            return album
        await self.queued(
            lambda: self.call(
                "send_packet", cmd=ADD_ALBUM, data=add_album(group, name).hex(), rsp=True
            )
        )
        # Looking again is shorter than decoding the protobuf reply, and as sure.
        if album := await self.find_album(group, name):
            return album
        raise RuntimeError(f"QQ didn't make album {name} in group {group}")

    async def find_album(self, group: int, name: str) -> str | None:
        cursor = ""
        while True:
            page = await self.call("get_qun_album_list", group_id=str(group), attach_info=cursor)
            for album in page["album_list"]:
                if album["name"] == name:
                    return album["album_id"]
            if not page["has_more"]:
                return None
            cursor = page["attach_info"]

    async def upload_album(self, group: int, album: str, name: str, image: bytes) -> None:
        file = "base64://" + base64.b64encode(image).decode()
        await self.queued(
            lambda: self.call(
                "upload_image_to_qun_album",
                group_id=str(group),
                album_id=album,
                album_name=name,
                file=file,
            )
        )

    async def queued(self, action: Callable[[], Awaitable[Any]]) -> None:
        """What the bot does in the groups of its own accord, spaced out like a person."""
        guard = getattr(self.manager, "_qq_automation_guard_v1", None)
        if guard is not None:
            await guard.run(
                account=self.platform().meta().id,
                online=self.online,
                action=action,
                config=GUARD_CONFIG,
                gap=GAP,
            )
            return
        async with self.sending:
            await asyncio.sleep(self.next_at - time.monotonic())
            try:
                await action()
            finally:
                self.next_at = time.monotonic() + random.uniform(*GAP)
