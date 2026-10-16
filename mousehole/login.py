"""Logging in to the forum by a private message to the bot (design §7)."""

import re

import httpx

from .forum import Forum, ForumError
from .qq import fill, text

CODE = re.compile(r"\d{6}")

# The forum's own messages are for people already; these few get said more plainly, in the
# words of the login_replies settings.
PLAIN = ("unknown_member", "member_frozen", "login_code_invalid")


def wants_login(text: str) -> bool:
    text = text.strip()
    return text.startswith("登录") or CODE.fullmatch(text) is not None


async def login(forum: Forum, qq: int, said: str, replies: dict[str, str], site: str) -> list[dict]:
    """`replies`: the login_replies settings."""
    code = said.strip().removeprefix("登录").strip()
    try:
        if CODE.fullmatch(code):
            result = await forum.call(
                "POST", "/bot/auth/code/confirm", body={"code": code}, act_as=qq, operator=qq
            )
            return fill(replies["confirmed"], device=result["device"], forum=site)
        link = await forum.call("POST", "/bot/auth/links", act_as=qq, operator=qq)
        return fill(replies["link"], url=link["url"], forum=site)
    except ForumError as e:
        return fill(replies[e.code], forum=site) if e.code in PLAIN else [text(e.message)]
    except httpx.HTTPError:
        return fill(replies["unreachable"], forum=site)
