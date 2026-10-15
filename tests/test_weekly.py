from datetime import datetime

import httpx
import pytest

from mousehole.forum import Forum
from mousehole.weekly import MENTIONS, Weekly, last_due, mention_message, poster_html

GROUP = 100200300


def person(n: int) -> dict:
    return {"id": n, "display_name": f"鼠{n}", "qq": 1000 + n, "avatar_url": None}


def stats(threads: int = 1) -> dict:
    return {
        "since": 1_790_000_000,
        "until": 1_790_604_800,
        "overview": {
            "threads": threads,
            "posts": 0,
            "archived": 0,
            "active": 1,
            "activated": 0,
            "badges": 0,
        },
        "threads": {
            "most_liked": [
                {
                    "thread": {"id": 1, "title": "<b>夏令营</b>", "url": "u"},
                    "board": {"slug": "qa", "name": "保研问答"},
                    "author": person(1),
                    "count": 5,
                }
            ],
            "most_replied": [],
            "most_read": [],
        },
        "people": {
            "most_liked": [{"user": person(n), "count": 9 - n} for n in range(1, 4)],
            "most_accepted": [],
            "most_replies": [],
        },
        "gold_badges": [{"user": person(2), "badge": {"id": 9, "name": "无所不知"}}],
    }


def test_the_week_is_due_on_sunday_at_20():
    sunday = datetime(2026, 10, 18, 20).timestamp()
    assert last_due(sunday) == sunday
    assert last_due(sunday - 1) == datetime(2026, 10, 11, 20).timestamp()
    assert last_due(datetime(2026, 10, 21, 9).timestamp()) == sunday


def test_only_members_of_the_group_are_mentioned_and_titles_are_escaped():
    segments = mention_message(stats(), {1001, 1002}, "https://bbs.test")
    assert [s["data"]["qq"] for s in segments if s["type"] == "at"] == ["1001", "1002", "1002"]
    text = "".join(s["data"].get("text", "") for s in segments)
    assert "鼠3" in text and "被采纳最多" not in text and "「无所不知」" in text
    many = stats()
    many["people"]["most_liked"] = [{"user": person(n), "count": 1} for n in range(30)]
    crowd = mention_message(many, {1000 + n for n in range(30)}, "s")
    assert sum(s["type"] == "at" for s in crowd) == MENTIONS
    page = poster_html(stats(), "https://bbs.test", "", {})
    assert "&lt;b&gt;夏令营" in page and "<b>夏令营" not in page


class FakeQQ:
    def __init__(self, fail_in: int | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_in = fail_in

    async def send_group(self, group, message):
        if group == self.fail_in:
            raise RuntimeError("offline")
        self.sent.append((group, message[0]["type"]))


async def test_the_poster_goes_once_to_each_group_and_not_for_an_empty_or_stale_week(tmp_path):
    asked = []
    week = {"threads": 1}

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(int(request.url.params["until"]))
        return httpx.Response(200, json=stats(week["threads"]))

    async def render(page: str) -> bytes:
        return b"jpeg"

    sunday = datetime(2026, 10, 18, 20).timestamp()
    now = {"t": sunday + 60}
    forum = Forum("http://forum.test", "t", httpx.MockTransport(handle))
    sync = type("Sync", (), {"groups": [GROUP, GROUP + 1], "roster": {}})()
    qq = FakeQQ(fail_in=GROUP + 1)

    def weekly(qq):
        return Weekly(
            forum,
            qq,
            sync,
            tmp_path / "weekly.json",
            groups=[],
            site="https://bbs.test",
            render=render,
            clock=lambda: now["t"],
        )

    with pytest.raises(RuntimeError):
        await weekly(qq).post()
    assert qq.sent == [(GROUP, "image"), (GROUP, "text")]
    assert asked == [int(sunday)]

    # After a restart only the group that missed it gets it.
    qq = FakeQQ()
    await weekly(qq).post()
    assert qq.sent == [(GROUP + 1, "image"), (GROUP + 1, "text")]
    await weekly(qq).post()
    assert len(qq.sent) == 2

    now["t"] = sunday + 7 * 86400 + 13 * 3600
    await weekly(qq).post()
    week["threads"] = 0
    now["t"] = sunday + 14 * 86400
    await weekly(qq).post()
    assert len(qq.sent) == 2
