import json

import httpx
from conftest import Script

from mousehole import judge
from mousehole.forum import Forum
from mousehole.summaries import Summaries

SOURCE = {
    "thread": {
        "id": 7,
        "kind": "question",
        "title": "预推免的第一批和第二批能都报吗",
        "board": "保研问答",
        "tags": ["预推免"],
        "positioning": False,
    },
    "replies": 6,
    "posts": [
        {"floor": 1, "op": True, "accepted": False, "likes": 0, "text": "第一批没过还能报第二批吗"},
        {"floor": 2, "op": False, "accepted": True, "likes": 3, "text": "可以，两批互不影响"},
        {"floor": 3, "op": False, "accepted": False, "likes": 0, "text": "谢谢"},
    ],
}


class FakeForum:
    """The summary endpoints of the forum, keeping what was pushed."""

    def __init__(self, due: list[dict], source: dict | None = SOURCE, put_status: int = 204):
        self.due = due
        self.source = source
        self.put_status = put_status
        self.pushed: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/bot/summaries/due":
            return httpx.Response(200, json={"items": self.due})
        if path.endswith("/source"):
            if self.source is None:
                error = {"code": "thread_not_found", "message": "帖子不存在"}
                return httpx.Response(404, json={"error": error})
            return httpx.Response(200, json=self.source)
        self.pushed.append((path, json.loads(request.content)))
        if self.put_status == 409:
            error = {"code": "summary_disabled", "message": "版主关闭了这个帖子的 AI 总结"}
            return httpx.Response(409, json={"error": error})
        return httpx.Response(204)

    def client(self) -> Forum:
        return Forum("https://bbs.test", "t", transport=httpx.MockTransport(self.handle))


async def test_a_due_thread_is_summarized_from_its_floors_and_pushed_with_its_replies():
    forum = FakeForum([{"thread_id": 7, "replies": 6}])
    llm = Script(
        {
            "lead": "问的是预推免第一批没过能不能报第二批",
            "points": [
                {"text": "可以，两批互不影响", "floors": [2, 2, 9]},
                {"text": "没有出处的要点", "floors": [9]},
            ],
        }
    )
    await Summaries(forum.client(), llm).round()
    [(system, prompt)] = llm.prompts
    assert system == judge.SUMMARY
    assert prompt == (
        "# 帖子\n版块：保研问答\n类型：问答帖\n标签：预推免\n标题：预推免的第一批和第二批能都报吗\n\n"
        "# 楼层\n#1（楼主）：第一批没过还能报第二批吗\n#2（已采纳，3 个赞）：可以，两批互不影响\n#3：谢谢"
    )
    assert forum.pushed == [
        (
            "/bot/summaries/7",
            {
                "lead": "问的是预推免第一批没过能不能报第二批",
                "points": [{"text": "可以，两批互不影响", "floors": [2]}],
                "replies": 6,
            },
        )
    ]


def test_past_the_budget_the_opening_post_accepted_and_most_liked_floors_are_kept():
    def post(floor, *, likes=0, accepted=False):
        return {
            "floor": floor,
            "op": floor == 1,
            "accepted": accepted,
            "likes": likes,
            "text": "字" * 900,
        }

    posts = [post(1), *(post(f) for f in range(2, 60)), post(60, likes=9), post(61, accepted=True)]
    kept = judge.summary_floors(posts)
    assert sum(len(p["text"]) for p in kept) <= judge.SUMMARY_CHARS
    assert all(len(p["text"]) == judge.SUMMARY_FLOOR_CHARS for p in kept)
    assert kept[0]["floor"] == 1
    assert {60, 61} <= {p["floor"] for p in kept}
    assert [p["floor"] for p in kept] == sorted(p["floor"] for p in kept)


async def test_what_comes_to_nothing_is_not_paid_for_again_until_the_thread_grows():
    forum = FakeForum([{"thread_id": 7, "replies": 6}])
    llm = Script("不是 JSON", {"lead": "问的是能不能报两批", "points": []})
    s = Summaries(forum.client(), llm)
    await s.round()
    await s.round()
    assert len(llm.prompts) == 1
    forum.due = [{"thread_id": 7, "replies": 7}]
    await s.round()
    assert len(llm.prompts) == 2
    assert forum.pushed == []


async def test_a_thread_gone_or_turned_off_meanwhile_is_skipped():
    gone = FakeForum([{"thread_id": 7, "replies": 6}], source=None)
    llm = Script()
    await Summaries(gone.client(), llm).round()
    assert llm.prompts == [] and gone.pushed == []

    off = FakeForum(
        [{"thread_id": 7, "replies": 6}, {"thread_id": 8, "replies": 5}], put_status=409
    )
    answer = {"lead": "问的是能不能报两批", "points": [{"text": "可以", "floors": [2]}]}
    llm = Script(answer, answer)
    await Summaries(off.client(), llm).round()
    assert [path for path, _ in off.pushed] == ["/bot/summaries/7", "/bot/summaries/8"]
