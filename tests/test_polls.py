from datetime import datetime
from types import SimpleNamespace

import httpx
from conftest import defaults

from mousehole.bounties import FIRST_SINCE
from mousehole.forum import Forum
from mousehole.polls import Polls

NOW = datetime(2026, 10, 18, 20).timestamp()


async def test_new_polls_go_to_each_group_once_a_day(tmp_path):
    asked = []
    found = {
        "items": [
            {
                "thread": {"id": i, "title": f"第 {i} 个投票"},
                "voters": i * 2,
                "url": f"https://bbs.test/t/{i}",
            }
            for i in range(1, 8)
        ]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bot/polls"
        asked.append({k: int(v) for k, v in request.url.params.items()})
        return httpx.Response(200, json=found)

    sent = []

    async def send_group(group, message):
        sent.append((group, message))

    clock = {"t": NOW}
    polls = Polls(
        Forum("https://bbs.test", "t", transport=httpx.MockTransport(handle)),
        SimpleNamespace(send_group=send_group),
        SimpleNamespace(groups={300, 100}),
        tmp_path / "polls.json",
        groups=[],
        templates=defaults("polls"),
        site="https://bbs.test",
        clock=lambda: clock["t"],
    )
    await polls.round()
    assert asked == [{"since": int(NOW) - FIRST_SINCE, "until": int(NOW)}]
    assert [group for group, _ in sent] == [100, 300]
    [text] = sent[0][1]
    assert text["data"]["text"] == (
        "【鼠洞投票】今天鼠洞上新开了 7 个投票，来投一票吧：\n"
        + "\n".join(
            f"{i}. 第 {i} 个投票（{i * 2} 人已投）\nhttps://bbs.test/t/{i}" for i in range(1, 6)
        )
    )

    # The next day asks on from there, so no poll comes up twice; with nothing new, nothing is sent.
    found["items"] = []
    clock["t"] += 86400
    await polls.round()
    assert asked[1] == {"since": int(NOW), "until": int(NOW) + 86400}
    assert len(sent) == 2
