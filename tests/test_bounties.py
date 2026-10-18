from datetime import datetime
from types import SimpleNamespace

import httpx
from conftest import defaults

from mousehole.bounties import FIRST_SINCE, Bounties, next_round
from mousehole.forum import Forum

NOW = datetime(2026, 10, 18, 20).timestamp()


def test_rounds_are_each_day_at_eight_in_the_evening():
    def at(*args):
        return datetime(2026, 10, *args).timestamp()

    assert next_round(at(18, 19, 59)) == at(18, 20)
    assert next_round(at(18, 20)) == at(19, 20)
    assert next_round(at(18, 23, 30)) == at(19, 20)


async def test_new_bounties_go_to_each_group_once_a_day(tmp_path):
    asked = []
    found = {
        "items": [
            {
                "thread": {"id": i, "title": f"第 {i} 个问题", "kind": "question"},
                "board": {"slug": "qa", "name": "保研问答"},
                "url": f"https://bbs.test/t/{i}",
                "until": int(NOW) + 3 * 86400,
            }
            for i in range(1, 8)
        ],
        "reward": {"reply": 1, "answer": 5},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append({k: int(v) for k, v in request.url.params.items()})
        return httpx.Response(200, json=found)

    sent = []

    async def send_group(group, message):
        sent.append((group, message))

    clock = {"t": NOW}
    bounties = Bounties(
        Forum("https://bbs.test", "t", transport=httpx.MockTransport(handle)),
        SimpleNamespace(send_group=send_group),
        SimpleNamespace(groups={300, 100}),
        tmp_path / "bounties.json",
        groups=[],
        templates=defaults("bounty"),
        site="https://bbs.test",
        clock=lambda: clock["t"],
    )
    await bounties.round()
    assert asked == [{"since": int(NOW) - FIRST_SINCE, "until": int(NOW)}]
    assert [group for group, _ in sent] == [100, 300]
    [text] = sent[0][1]
    assert text["data"]["text"] == (
        "【鼠洞悬赏】这几个问题在鼠洞上一天没人回答，回复得 1 鼠币，被采纳再得 5 鼠币：\n"
        + "\n".join(f"{i}. 第 {i} 个问题\nhttps://bbs.test/t/{i}" for i in range(1, 6))
    )

    # The next day asks on from there; with nothing new, nothing is sent.
    found["items"] = []
    clock["t"] += 86400
    await bounties.round()
    assert asked[1] == {"since": int(NOW), "until": int(NOW) + 86400}
    assert len(sent) == 2
