"""Logging in to the forum by a private message to the bot (design §7)."""

import re

import httpx

from .forum import Forum, ForumError

CODE = re.compile(r"\d{6}")

# The forum's own messages are for people already; these few get said more plainly.
REPLIES = {
    "unknown_member": "论坛还没同步到你，进群后稍等几分钟再试",
    "member_frozen": "你已经不在鼠群里了",
    "login_code_invalid": "口令不对或者过期了，刷新网页拿个新的",
}


def wants_login(text: str) -> bool:
    text = text.strip()
    return text.startswith("登录") or CODE.fullmatch(text) is not None


async def login(forum: Forum, qq: int, text: str) -> str:
    code = text.strip().removeprefix("登录").strip()
    try:
        if CODE.fullmatch(code):
            result = await forum.call(
                "POST", "/bot/auth/code/confirm", body={"code": code}, act_as=qq, operator=qq
            )
            return (
                f"已在 {result['device']} 登录鼠洞。"
                "不是你本人在登录的话，到鼠洞设置页的登录设备里让它下线。"
            )
        link = await forum.call("POST", "/bot/auth/links", act_as=qq, operator=qq)
        return f"鼠洞的登录链接，10 分钟内有效，只能用一次：\n{link['url']}"
    except ForumError as e:
        return REPLIES.get(e.code, e.message)
    except httpx.HTTPError:
        return "鼠洞暂时连不上，稍后再试"
