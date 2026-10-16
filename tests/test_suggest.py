from conftest import BOT, GROUP, Script, defaults, embed, message

from mousehole import judge
from mousehole.similar import Index
from mousehole.suggest import Suggest
from tools.fake_forum import FakeForum

ASKER, ANSWERER, OTHER = 10001, 10002, 10003


async def suggest_with(llm, *threads_by):
    """Suggest over a forum with a thread with a reply by each of `threads_by`."""
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

    s = Suggest(
        client,
        llm,
        index,
        notify,
        site="https://forum.test",
        templates=defaults("archive"),
        prefixes=["/"],
    )
    return s, sent


async def test_a_question_a_thread_answers_gets_it_at_once_and_not_again_soon():
    llm = Script({"answers": [{"question": "Q1", "threads": ["C1"]}]})
    s, sent = await suggest_with(llm, ANSWERER)
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
    [(system, prompt)] = llm.prompts
    assert system == judge.SUGGEST and "哈哈" not in prompt
    s.add(GROUP, message(4, OTHER, "预推免两批能一起报吗？"), BOT)
    await s.round()
    assert len(sent) == 1


async def test_without_candidates_the_llm_is_not_asked():
    # The asker's own thread is none for them; Script() has nothing to answer with.
    s, sent = await suggest_with(Script(), ASKER)
    s.add(GROUP, message(1, ASKER, "预推免第一批和第二批可以都报吗？"), BOT)
    await s.round()
    assert sent == []


def test_what_the_llm_picks_is_kept_only_for_questions_and_threads_there_are():
    raw = {
        "answers": [
            {"question": "Q1", "threads": ["C2", "C2", "C9", "C1"]},
            {"question": "Q7", "threads": ["C1"]},
            {"question": "X", "threads": ["C1"]},
        ]
    }
    assert judge.suggestions(raw, [2]) == {0: [1, 0]}
