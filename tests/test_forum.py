import httpx
import pytest

from mousehole import forum as forum_module
from mousehole.forum import Forum


@pytest.fixture
def waits(monkeypatch):
    monkeypatch.setattr(forum_module, "RETRY_WAITS", (0, 0))


def forum(*troubles):
    """A fake forum answering with these in turn, then 200; keeps the methods it was sent."""
    sent, left = [], list(troubles)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        trouble = left.pop(0) if left else 200
        if isinstance(trouble, int):
            return httpx.Response(trouble, json={"ok": True})
        raise trouble

    return Forum("http://forum.test", "t", httpx.MockTransport(handle)), sent


async def test_a_forum_restarting_is_asked_again(waits):
    f, sent = forum(httpx.ConnectError("refused"), 502)
    assert await f.call("POST", "/tags", body={}) == {"ok": True}
    assert sent == ["POST"] * 3


async def test_what_may_have_been_done_is_sent_again_only_with_a_key_or_for_reading(waits):
    f, sent = forum(httpx.ReadError("reset"))
    with pytest.raises(httpx.ReadError):
        await f.call("POST", "/tags", body={})
    for call in ({"key": "k"}, {}):
        f, sent = forum(httpx.ReadError("reset"))
        method = "POST" if call else "GET"
        assert await f.call(method, "/threads", **call) == {"ok": True}
        assert len(sent) == 2


async def test_the_forum_down_for_long_fails_in_the_end(waits):
    f, sent = forum(503, 503, 503)
    with pytest.raises(forum_module.ForumError):
        await f.call("GET", "/boards")
    assert len(sent) == 3
