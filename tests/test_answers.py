from datetime import datetime

import httpx
from conftest import defaults

from mousehole import answers
from mousehole.answers import FRESH, PER_HOUR, SETTLE, Answers, next_round
from mousehole.forum import Forum

GROUP = 100200300
ASKER = 10001
START = 1_000_000.0


def item(
    thread: int, *, group: int = GROUP, asker: int = ASKER, count: int = 1, asked: float = START
) -> dict:
    return {
        "thread": {"id": thread, "title": "预推免能报两批吗", "created_at": int(asked)},
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


def test_rounds_are_every_half_hour_from_nine_to_twenty_three():
    def at(*args):
        return datetime(2026, 10, *args).timestamp()

    assert next_round(at(16, 8, 59)) == at(16, 9)
    assert next_round(at(16, 9)) == at(16, 9, 30)
    assert next_round(at(16, 12, 45)) == at(16, 13)
    assert next_round(at(16, 22, 30)) == at(16, 23)
    assert next_round(at(16, 23)) == at(17, 9)
    assert next_round(at(17, 0, 10)) == at(17, 9)


async def test_reminders_go_to_askers_in_the_group_a_few_an_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(answers.random, "uniform", lambda a, b: 120)
    asked = []
    found = {"items": []}

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json=found)

    clock = {"t": START}

    async def sleep(seconds):
        clock["t"] += max(seconds, 0)

    qq = FakeQQ(clock)
    forum = Forum("http://forum.test", "t", httpx.MockTransport(handle))
    others = [ASKER + n for n in range(1, PER_HOUR + 2)]
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
    first = int(START - SETTLE)

    clock["t"] = START + 3600
    found["items"] = [
        item(1, count=3),
        item(2, asker=ASKER + 99),  # left the group
        item(3, group=GROUP + 1),  # a group no longer linked
        item(4, asked=clock["t"] - FRESH - 1),  # asked too long ago
        *(item(10 + n, asker=q) for n, q in enumerate(others)),
    ]
    await a.round()
    assert asked == [{"since": str(first), "until": str(int(START + 3600 - SETTLE))}]
    assert len(qq.sent) == PER_HOUR
    told_at, group, [quote, at, text] = qq.sent[0]
    assert (group, quote, at) == (
        GROUP,
        {"type": "reply", "data": {"id": "555"}},
        {"type": "at", "data": {"qq": str(ASKER)}},
    )
    assert text["data"]["text"] == (
        " 你问的「预推免能报两批吗」在鼠洞上有 3 条新回答：\nhttps://bbs.test/t/1?floor=3"
    )
    assert [t - START - 3600 for t, _, _ in qq.sent] == [120, 240, 360]

    # Those held are asked for again from the same time, and those told are not told again.
    clock["t"] = START + 2 * 3600 + 360
    await a.round()
    assert asked[-1]["since"] == str(first)
    assert [m[1]["data"]["qq"] for _, _, m in qq.sent[PER_HOUR:]] == [str(q) for q in others[2:]]

    # All told, the next round asks from where this one ended.
    found["items"] = []
    await a.round()
    assert asked[-1]["since"] == str(int(START + 2 * 3600 + 360 - SETTLE))
