import asyncio

import httpx
import pytest
from conftest import BOT, GROUP, Script, defaults, embed, message

from mousehole import judge, suggest
from mousehole.forum import Forum, ForumError
from mousehole.similar import Index
from mousehole.suggest import Suggest
from tools.fake_forum import FakeForum, error

ASKER, ANSWERER, OTHER = 10001, 10002, 10003


ASKS = {"asking": True, "asks": "预推免的第一批和第二批能不能都报"}
MATCH = {"threads": [{"thread": "C1", "same": True, "floor": 2}]}


async def suggest_with(llm, *threads_by, scores=None):
    """Suggest over a forum with a thread with a reply by each of `threads_by`, the reranker
    scoring the threads found `scores` in their order, 0.9 each by default; and the fake
    forum."""
    forum = FakeForum()
    client = forum.client()
    for author in threads_by:
        thread = {
            "board": "qa",
            "kind": "question",
            "title": "预推免的第一批和第二批能都报吗",
            "tags": [],
            "content": {"markdown": "预推免能报两批吗"},
            "source": {"chat_id": GROUP, "message_ids": [author]},
        }
        t = forum.create_thread(thread, author).json()
        reply = {
            "content": {"markdown": "可以"},
            "source": {"chat_id": GROUP, "message_ids": [-author]},
        }
        forum.create_post(forum.threads[t["id"]], reply, OTHER)
    index = Index(client, embed, "test", None)
    await index.rebuild()
    sent = []

    async def notify(group, segments):
        sent.append((group, segments))

    async def rerank(query, documents):
        return scores or [0.9] * len(documents)

    s = Suggest(
        client,
        llm,
        rerank,
        index,
        notify,
        site="https://forum.test",
        templates=defaults("archive"),
        prefixes=["/"],
    )
    return s, sent, forum


async def test_a_question_a_thread_answers_gets_it_at_once_and_not_again_soon():
    llm = Script(ASKS, MATCH)
    s, sent, _ = await suggest_with(llm, ANSWERER)
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    s.add(GROUP, message(2, OTHER, "哈哈哈哈哈哈"), BOT)
    s.add(GROUP, message(3, BOT, "预推免能报两批吗？"), BOT)
    await s.round()
    [(group, [quote, text])] = sent
    assert (group, quote) == (GROUP, {"type": "reply", "data": {"id": "1"}})
    assert text["data"]["text"] == (
        "猜你想问：\n1. 预推免的第一批和第二批能都报吗\nhttps://forum.test/t/1"
    )
    # Asked only about the question; told already, the thread is no candidate for a while.
    [(asks, _), (match, prompt)] = llm.prompts
    assert (asks, match) == (judge.SUGGEST_ASKS, judge.SUGGEST_MATCH) and "哈哈" not in prompt
    s.add(GROUP, message(4, OTHER, "预推免两批能一起报吗？"), BOT)
    await s.round()
    assert len(sent) == 1


async def test_a_question_brings_a_round_a_minute_later():
    llm = Script(ASKS, MATCH)
    s, sent, _ = await suggest_with(llm, ANSWERER)
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    s.sleep = sleep
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0)
    s.add(GROUP, message(1, OTHER, "哈哈哈哈哈哈"), BOT)
    await asyncio.sleep(0)
    assert waited == []
    s.add(GROUP, message(2, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    async with asyncio.timeout(1):
        while not sent:
            await asyncio.sleep(0)
    task.cancel()
    assert waited == [suggest.DELAY]


async def test_a_group_gets_two_replies_in_any_ten_minutes_at_most():
    s, sent, _ = await suggest_with(Script(*[ASKS, MATCH] * 4), ANSWERER, OTHER, 10004, 10005)
    now = 1_791_600_000.0
    s.clock = lambda: now
    # Rounds 4 minutes apart: the third has two replies in the 10 minutes before it.
    for n, replies in enumerate([1, 2, 2, 3], 1):
        s.add(GROUP, message(n, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
        await s.round()
        assert len(sent) == replies
        now += 240


async def test_the_same_question_said_by_others_within_a_minute_is_a_joke_not_asked():
    # Script() has nothing to answer with: the LLM is not asked.
    s, sent, _ = await suggest_with(Script(), ANSWERER)
    s.add(GROUP, message(1, ASKER, "科大这辈子还有机会吗"), BOT)
    s.add(GROUP, message(30, OTHER, "科大这辈子还有机会吗"), BOT)
    await s.round()
    assert sent == []


async def test_without_candidates_the_llm_is_not_asked():
    # The asker's own thread is none for them; Script() has nothing to answer with.
    s, sent, _ = await suggest_with(Script(), ASKER)
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    await s.round()
    assert sent == []


async def test_a_forum_without_the_source_endpoint_fails_the_round_not_skips_the_thread():
    s, _, forum = await suggest_with(Script(), ANSWERER)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/source"):
            return error(404, "not_found")
        return forum.handle(request)

    s.forum = Forum("http://forum.test", "token", httpx.MockTransport(handle))
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    with pytest.raises(ForumError):
        await s.round()


async def test_the_llm_sees_the_question_after_what_was_said_just_before_and_then_the_floors():
    llm = Script(ASKS, {"threads": []})
    s, _, forum = await suggest_with(llm, ANSWERER)
    forum.threads[1].posts[1].accepted = True
    # A message's ID is its second: 1 is quoted but long before, 50 only long before.
    for m in [
        message(1, OTHER, "预推免报名开始了"),
        message(50, ANSWERER, "旧的话"),
        message(400, ANSWERER, "第一批明天截止"),
        message(401, BOT, "收到"),
        message(402, ASKER, "我还没报"),
        message(403, ASKER, "第一批和第二批可以都报吗？", reply=1),
        message(404, OTHER, "后来的话"),
    ]:
        s.add(GROUP, m, BOT)
    await s.round()
    [(_, question), (_, prompt)] = llm.prompts
    # What is asked is judged by the messages alone.
    assert question == (
        "群友1：预推免报名开始了\n群友2：第一批明天截止\nbot：收到\n提问人：我还没报\n"
        "提问人 ↩群友1：第一批和第二批可以都报吗？"
    )
    asks, thread = prompt.split("\n\n")
    assert asks == "asks：预推免的第一批和第二批能不能都报\n原话：第一批和第二批可以都报吗？"
    assert thread.startswith("C1. 预推免的第一批和第二批能都报吗（保研问答，问答帖，")
    assert thread.endswith("1 条回复）\n#1（楼主）：预推免能报两批吗\n#2（已采纳）：可以")


async def test_the_reranker_picks_which_threads_go_to_the_llm_and_in_what_order():
    llm = Script(ASKS, MATCH)
    s, sent, forum = await suggest_with(llm, ANSWERER, OTHER, 10004, scores=[0.2, 0.8, 0.1])
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    await s.round()
    [_, (_, prompt)] = llm.prompts
    assert "C1." in prompt and "C2." not in prompt
    [(_, [_, text])] = sent
    _, second, _ = forum.threads
    assert text["data"]["text"].endswith(f"https://forum.test/t/{second}")


async def test_threads_the_reranker_scores_low_or_a_question_asking_nothing_get_nothing():
    # Script() has nothing to answer with: the LLM is not asked.
    s, sent, _ = await suggest_with(Script(), ANSWERER, scores=[0.29])
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    await s.round()
    # Not asking: no threads are looked at.
    llm = Script({"asking": False, "asks": ""})
    s, _, _ = await suggest_with(llm, ANSWERER)
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    await s.round()
    assert sent == [] and len(llm.prompts) == 1


def test_only_threads_asking_the_same_with_an_answering_floor_shown_are_picked():
    threads = [{"posts": [{"floor": 1}, {"floor": 2}, {"floor": 5}]} for _ in range(2)]
    raw = {
        "threads": [
            {"thread": "C2", "same": True, "floor": 1},
            {"thread": "C2", "same": True, "floor": 3},
            {"thread": "C1", "same": False, "floor": 2},
            {"thread": "C9", "same": True, "floor": 2},
            {"thread": "C2", "same": True, "floor": 5},
            {"thread": "C2", "same": True, "floor": 2},
            {"thread": "C1", "same": True, "floor": 2},
        ]
    }
    assert judge.matches(raw, threads) == [1, 0]
    assert judge.matches({"asking": True}, threads) == []
