from datetime import datetime

import httpx
from conftest import defaults

from mousehole import answers
from mousehole.answers import PER_ROUND, SPREAD, Answers, next_round
from mousehole.forum import Forum

GROUP = 100200300
ASKER = 10001


def item(thread: int, *, group: int = GROUP, asker: int = ASKER, count: int = 1) -> dict:
    return {
        "thread": {"id": thread, "title": "预推免能报两批吗"},
        "asker": {"id": 7, "display_name": "鼠鼠", "qq": asker},
        "source": {"chat_id": group, "message_id": 555},
        "count": count,
        "url": f"https://bbs.test/t/{thread}?floor=3",
    }


class FakeQQ:
    def __init__(self, clock):
        self.clock = clock
        self.sent: list[tuple[float, int, list[dict]]] = []

    async def send_group(self, group, message):
        self.sent.append((self.clock["t"], group, message))


def test_rounds_are_at_ten_sixteen_and_twenty_two_and_none_at_night():
    def at(*args):
        return datetime(2026, 10, *args).timestamp()

    assert next_round(at(16, 9, 59)) == at(16, 10)
    assert next_round(at(16, 10)) == at(16, 16)
    assert next_round(at(16, 22, 30)) == at(17, 10)


async def test_a_round_tells_askers_still_in_the_group_spread_over_two_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(answers.random, "random", lambda: 0.5)
    asked = []
    found = {"items": []}

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json=found)

    clock = {"t": 1_000_000.0}

    async def sleep(seconds):
        clock["t"] += max(seconds, 0)

    qq = FakeQQ(clock)
    forum = Forum("http://forum.test", "t", httpx.MockTransport(handle))
    others = [ASKER + n for n in range(1, PER_ROUND + 1)]
    sync = type(
        "Sync", (), {"groups": [GROUP], "roster": {GROUP: {ASKER, *others}, GROUP + 1: {ASKER}}}
    )()
    a = Answers(
        forum,
        qq,
        sync,
        tmp_path / "answers.json",
        template=defaults("answers")["template"],
        site="https://bbs.test",
        clock=lambda: clock["t"],
        sleep=sleep,
    )

    # The first round only marks where the next begins.
    await a.round()
    assert (asked, qq.sent) == ([], [])

    start = clock["t"] = 1_020_000.0
    found["items"] = [
        item(1, count=3),
        item(2, asker=ASKER + 99),  # left the group
        item(3, group=GROUP + 1),  # a group no longer linked
        *(item(10 + n, asker=q) for n, q in enumerate(others)),
    ]
    await a.round()
    assert asked == [{"since": "1000000", "until": "1020000"}]
    assert len(qq.sent) == PER_ROUND
    told_at, group, [quote, at, text] = qq.sent[0]
    assert (group, quote, at) == (
        GROUP,
        {"type": "reply", "data": {"id": "555"}},
        {"type": "at", "data": {"qq": str(ASKER)}},
    )
    assert text["data"]["text"] == (
        " 你问的「预推免能报两批吗」在鼠洞上有 3 条新回答：\nhttps://bbs.test/t/1?floor=3"
    )
    slot = SPREAD / PER_ROUND
    assert [t - start for t, _, _ in qq.sent] == [slot * (i + 0.5) for i in range(PER_ROUND)]

    # The next round asks from where this one began.
    found["items"] = []
    clock["t"] += 3600
    await a.round()
    assert asked[-1]["since"] == "1020000"
