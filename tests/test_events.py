import json
import time

import httpx
import pytest
from conftest import defaults

from mousehole import events
from mousehole.events import MENTIONS, Events, badge_message, lottery_message
from mousehole.forum import Forum

GROUP = 100200300
SITE = "https://bbs.test"
WORDING = {
    "site": SITE,
    "lottery_templates": defaults("lottery"),
    "badge_template": defaults("badges")["template"],
}


def drawn(winners_per_prize):
    return {
        "thread": {"id": 42, "title": "周年抽奖"},
        "prizes": [
            {
                "name": f"奖{i}",
                "winners": [{"qq": 1000 + n, "display_name": f"鼠{n}"} for n in range(w)],
            }
            for i, w in enumerate(winners_per_prize)
        ],
        "entry_count": 25,
        "url": "https://bbs.test/t/42",
    }


def test_lottery_results_mention_winners_up_to_a_limit_and_name_the_rest():
    segments = lottery_message(drawn([MENTIONS + 2, 0]), defaults("lottery"), SITE)
    assert sum(s["type"] == "at" for s in segments) == MENTIONS
    text = "".join(s["data"].get("text", "") for s in segments)
    assert text.startswith("【抽奖开奖】周年抽奖\n奖0")
    assert (
        f" 鼠{MENTIONS} 鼠{MENTIONS + 1}\n奖1 无人中奖\n共 25 人参与\nhttps://bbs.test/t/42" in text
    )


class FakeQQ:
    def __init__(self):
        self.sent = []

    async def call(self, action, **params):
        assert action == "get_stranger_info"
        return {"long_nick": f"{params['user_id']} 的签名"}

    async def send_group(self, group, message):
        self.sent.append(group)


class Stop(Exception):
    pass


async def test_events_are_caught_up_then_streamed_and_the_cursor_kept(tmp_path, monkeypatch):
    now = time.time()
    batch = [
        {"id": 6, "type": "lottery.drawn", "data": drawn([1]), "created_at": now - 60},
        {"id": 7, "type": "lottery.drawn", "data": drawn([1]), "created_at": now - 13 * 3600},
        {"id": 8, "type": "member.activated", "data": {"user": {"qq": 123}}, "created_at": now},
    ]
    streamed = f"id: 9\nevent: lottery.drawn\ndata: {json.dumps(drawn([1]))}\n\n: heartbeat\n\n"
    bios = []

    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path.removeprefix("/api/v1"):
            case "/bot/events/batch":
                after = int(request.url.params["after"])
                return httpx.Response(
                    200, json={"items": [e for e in batch if e["id"] > after], "next_after": 8}
                )
            case "/bot/events":
                assert request.headers["Last-Event-ID"] == "8"
                return httpx.Response(200, text=streamed)
            case "/bot/users/qq:123/bio":
                bios.append(json.loads(request.content))
                return httpx.Response(204)
        raise AssertionError(request.url)

    async def stop(seconds):
        raise Stop

    monkeypatch.setattr(events.asyncio, "sleep", stop)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cursor": 5}))
    qq = FakeQQ()
    sync = type("Sync", (), {"groups": [GROUP]})()
    forum = Forum("http://forum.test", "t", httpx.MockTransport(handle))
    with pytest.raises(Stop):
        await Events(
            forum, qq, sync, state, lottery=[], badges=True, bio=True, archive=None, **WORDING
        ).run()
    # The stale draw isn't announced; the fresh and the streamed ones are.
    assert qq.sent == [GROUP, GROUP]
    assert bios == [{"bio": "123 的签名"}]
    assert json.loads(state.read_text()) == {"cursor": 9}


async def test_an_expired_cursor_goes_on_from_the_latest_event(tmp_path, monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "5":
            error = {"error": {"code": "cursor_expired", "message": "过期"}, "latest_id": 77}
            return httpx.Response(410, json=error)
        raise httpx.ConnectError("gone")

    async def stop(seconds):
        raise Stop

    monkeypatch.setattr(events.asyncio, "sleep", stop)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cursor": 5}))
    forum = Forum("http://forum.test", "t", httpx.MockTransport(handle))
    with pytest.raises(Stop):
        await Events(
            forum, FakeQQ(), None, state, lottery=[], badges=True, bio=True, archive=None, **WORDING
        ).run()
    assert json.loads(state.read_text()) == {"cursor": 77}


async def test_gold_badges_are_congratulated_in_the_groups_the_member_is_in():
    other = GROUP + 1
    qq = FakeQQ()
    sync = type("Sync", (), {"groups": [GROUP, other], "roster": {GROUP: {123}, other: {456}}})()
    e = Events(None, qq, sync, None, lottery=None, badges=True, bio=False, archive=None, **WORDING)

    def granted(tier: str) -> dict:
        badge = {"id": 3, "name": "无所不知", "description": "回复被采纳 30 次", "tier": tier}
        return {"user": {"id": 7, "qq": 123}, "badge": badge, "post": None, "url": "u"}

    await e.act("badge.granted", granted("gold"), time.time())
    await e.act("badge.granted", granted("silver"), time.time())
    await e.act("badge.granted", granted("gold"), time.time() - 13 * 3600)
    assert qq.sent == [GROUP]
    assert badge_message(granted("gold"), defaults("badges")["template"], SITE) == [
        {"type": "text", "data": {"text": "【鼠洞金徽章】恭喜 "}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " 得到「无所不知」：回复被采纳 30 次\nu"}},
    ]
