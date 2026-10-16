import httpx

from mousehole import cards
from mousehole.cards import Cards
from mousehole.forum import Forum

GROUP = 100200300
SENDER, BOT = 10001, 20000


def thread(id: int, **change) -> dict:
    return {
        "id": id,
        "board": {"slug": "qa", "name": "保研问答"},
        "kind": "question",
        "title": "预推免能报两批吗",
        "author": {"id": 7, "display_name": "鼠鼠"},
        "status": "visible",
        "solved": True,
        "reply_count": 5,
        "view_count": 120,
    } | change


def said(text: str, user: int = SENDER, id: int = 777) -> dict:
    return {
        "group_id": GROUP,
        "user_id": user,
        "self_id": BOT,
        "message_id": id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


async def test_links_get_cards_the_sender_can_read_once_in_a_while(monkeypatch):
    monkeypatch.setattr(cards, "REPLY_DELAY", (0, 0))
    threads = {
        1: thread(1),
        2: thread(2, kind="discussion", reply_count=0, author={"display_name": "匿名鼠 1"}),
        4: thread(4, status="pending"),
    }
    read_as = []

    def handle(request: httpx.Request) -> httpx.Response:
        read_as.append(request.headers["X-Act-As"])
        t = threads.get(int(request.url.path.rsplit("/", 1)[1]))
        if t is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "没有"}})
        return httpx.Response(200, json=t)

    sent = []

    class FakeQQ:
        async def send_group(self, group, message):
            sent.append((group, message))

    clock = {"t": 0.0}
    c = Cards(
        Forum("http://forum.test", "t", httpx.MockTransport(handle)),
        FakeQQ(),
        site="https://bbs.test/",
        clock=lambda: clock["t"],
    )
    await c.saw(
        said("看 https://bbs.test/t/1?floor=3 和 https://bbs.test/t/1 还有 https://bbs.test/t/2")
    )
    assert read_as == [f"qq:{SENDER}"] * 2
    [(group, [quote, text])] = sent
    assert (group, quote) == (GROUP, {"type": "reply", "data": {"id": "777"}})
    assert text["data"]["text"] == (
        "「保研问答」预推免能报两批吗\n鼠鼠 · 5 条回复 · ✅ 已解决 · 120 次浏览\n\n"
        "「保研问答」预推免能报两批吗\n匿名鼠 1 · 还没有回复 · 120 次浏览"
    )

    # Shown lately, hidden from the sender, gone, or the bot's own: no card.
    await c.saw(said("https://bbs.test/t/1 https://bbs.test/t/4 https://bbs.test/t/9"))
    await c.saw(said("https://bbs.test/t/3", user=BOT))
    assert len(sent) == 1
    clock["t"] = cards.AGAIN_AFTER
    await c.saw(said("https://bbs.test/t/1"))
    assert len(sent) == 2
