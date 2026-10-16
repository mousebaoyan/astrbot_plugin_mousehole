import httpx
import pytest
from conftest import defaults

from mousehole.forum import Forum
from mousehole.login import login, wants_login
from mousehole.qq import text

REPLIES = defaults("login_replies")


@pytest.mark.parametrize(
    ("text", "wanted"),
    [
        ("登录", True),
        (" 登录链接", True),
        ("482913", True),
        ("登录 482913", True),
        ("4829", False),
        ("怎么登录", False),
    ],
)
def test_what_counts_as_logging_in(text, wanted):
    assert wants_login(text) is wanted


def forum(status, body, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(
                (request.url.path, request.headers["X-Act-As"], request.headers["X-Operator"])
            )
        return httpx.Response(status, json=body)

    return Forum("http://forum.test", "t", httpx.MockTransport(handle))


async def test_a_code_logs_in_the_browser_and_anything_else_sends_a_link():
    seen = []
    reply = await login(
        forum(200, {"device": "Chrome · macOS"}, seen), 1, "登录 482913", REPLIES, ""
    )
    assert reply[0]["data"]["text"].startswith("已在 Chrome · macOS 登录鼠洞")
    reply = await login(forum(201, {"url": "https://bbs.test/l/x"}, seen), 1, "登录", REPLIES, "")
    assert reply == [text("鼠洞的登录链接，10 分钟内有效，只能用一次：\nhttps://bbs.test/l/x")]
    assert seen == [
        ("/api/v1/bot/auth/code/confirm", "qq:1", "qq:1"),
        ("/api/v1/bot/auth/links", "qq:1", "qq:1"),
    ]


async def test_errors_are_told_plainly():
    unknown = {"error": {"code": "unknown_member", "message": "成员不存在"}}
    reply = await login(forum(404, unknown), 1, "登录", REPLIES, "")
    assert reply == [text("论坛还没同步到你，进群后稍等几分钟再试")]
    other = {"error": {"code": "rate_limited", "message": "太频繁了"}}
    assert await login(forum(429, other), 1, "登录", REPLIES, "") == [text("太频繁了")]

    def down(request):
        raise httpx.ConnectError("refused")

    unreachable = Forum("http://forum.test", "t", httpx.MockTransport(down))
    reply = await login(unreachable, 1, "登录", REPLIES, "")
    assert reply == [text("鼠洞暂时连不上，稍后再试")]
