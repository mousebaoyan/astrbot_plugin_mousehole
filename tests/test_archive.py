from conftest import BOT, GROUP, Script, message

from mousehole.archive import Tuning

TUNING = Tuning()

ASKER, ANSWERER, OTHER = 10001, 10002, 10003

QUESTION = {
    "ref": "N1",
    "type": "question",
    "messages": [1, 2],
    "title": "预推免第一批和第二批能都报吗",
    "body": "预推免的第一批和第二批，能都报吗？",
    "board": "qa",
    "tags": ["预推免", "没有的标签"],
    "same_as": None,
}
ANSWER = {
    "ref": "N2",
    "type": "reply",
    "question": "N1",
    "messages": [3],
    "body": "看学校，有些报了不好撤回。",
    "reply_to": None,
}


def first_round():
    return [
        message(1, ASKER, "大家预推免第一批和第二批"),
        message(2, ASKER, "能都报吗"),
        message(3, ANSWERER, "看学校，有些报了不好撤回", reply=2),
    ]


async def asked(setup, *later_answers: object):
    """A setup whose first round asked QUESTION and got ANSWER."""
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": []}, *later_answers))
    await s.round(*first_round())
    return s


def thread_of(s):
    return next(iter(s.forum.threads.values()))


async def test_rounds_close_at_size_or_after_a_while_and_leave_out_what_says_nothing(setup):
    s = setup(Script())
    a = s.archive
    a.add(GROUP, message(1, ASKER, "不是"), BOT)
    a.add(GROUP, message(2, BOT, "bot 自己说的"), BOT)
    a.add(GROUP, message(3, ASKER, "/help"), BOT)
    a.add(GROUP, message(4, ASKER, segments=[{"type": "face", "data": {"id": "1"}}]), BOT)
    sticker = {"type": "image", "data": {"sub_type": 1, "url": "https://gchat.qpic.cn/1"}}
    a.add(GROUP, message(5, ASKER, reply=1, segments=[sticker]), BOT)
    chat = a.chats[GROUP]
    assert [m.id for m in chat.pending] == [1]

    a.tick(chat.started + TUNING.round_minutes * 60 - 1)
    assert a.queue.empty()
    a.tick(chat.started + TUNING.round_minutes * 60)
    assert a.queue.qsize() == 1

    for i in range(TUNING.round_messages):
        a.add(GROUP, message(10 + i, ASKER, "嗯"), BOT)
    assert a.queue.qsize() == 2
    assert chat.pending == []


async def test_a_question_and_its_answer_become_a_thread_and_a_reply(setup):
    s = await asked(setup)
    t = thread_of(s)
    opening, reply = t.posts
    assert (t.title, t.board, t.tags, t.author) == (QUESTION["title"], "qa", ["预推免"], ASKER)
    assert (opening.body, opening.sources) == (QUESTION["body"], [1, 2])
    assert (reply.author, reply.sources, reply.reply_to) == (ANSWERER, [3], None)
    assert {f"qq-{GROUP}-1-thread", f"qq-{GROUP}-3-post"} <= set(s.forum.keys)
    # The models never see QQ numbers.
    system, prompt = s.llm.prompts[0]
    assert "#3 U2 ↩#2 看学校" in prompt and str(ASKER) not in prompt


async def test_what_the_asker_adds_later_rewrites_the_opening_and_its_title(setup):
    edit = {"post": "Q1", "messages": [4], "title": "rank 10% 预推免两批能都报吗", "body": "新正文"}
    s = await asked(setup, {"new": [], "edit": [edit]})
    report = await s.round(message(4, ASKER, "我 rank 前 10%", reply=3))

    assert "Q1 标题：预推免第一批和第二批能都报吗" in s.llm.prompts[1][1]
    t = thread_of(s)
    assert (t.title, t.posts[0].body, t.posts[0].sources) == (edit["title"], "新正文", [1, 2, 4])
    assert report["done"] == [f"edit {t.posts[0].id}"]


async def test_messages_archived_already_are_not_posted_again(setup):
    again = {**QUESTION, "messages": [1, 4], "title": "又一个标题哦"}
    s = await asked(setup, {"new": [again], "edit": []})
    report = await s.round(message(4, ASKER, "有人知道吗"))
    assert len(s.forum.threads) == 1
    assert "new N1: #1 is archived already" in report["dropped"]


async def test_a_floor_changed_on_the_forum_is_left_to_its_writer(setup):
    edit = {"post": "Q1", "messages": [4], "title": None, "body": "新正文"}
    s = await asked(setup, {"new": [], "edit": [edit]})
    thread_of(s).posts[0].edited_at = 123
    report = await s.round(message(4, ASKER, "补充一下", reply=1))
    assert thread_of(s).posts[0].body == QUESTION["body"]
    assert report["dropped"] == [f"edit {thread_of(s).posts[0].id}: changed on the forum"]


async def test_the_same_question_asked_again_goes_into_the_thread_as_a_floor(setup):
    again = {**QUESTION, "messages": [4], "same_as": "Q1"}
    answer = {**ANSWER, "question": "N1", "messages": [5], "ref": "N2"}
    s = await asked(setup, {"new": [{**again, "ref": "N1"}, answer], "edit": []})
    await s.round(message(4, OTHER, "预推免两批可以都报吗"), message(5, ANSWERER, "可以"))
    t = thread_of(s)
    asked_again, answered = t.posts[2:]
    assert (len(s.forum.threads), asked_again.author, asked_again.sources) == (1, OTHER, [4])
    assert answered.reply_to == asked_again.id


async def test_a_question_some_older_thread_asks_goes_there(setup):
    s = setup(Script({"new": [{**QUESTION, "messages": [1]}], "edit": []}, {"same": "C1"}))
    forum = s.forum.client()
    older = await forum.call(
        "POST",
        "/threads",
        body={
            "board": "qa",
            "kind": "question",
            "title": "预推免第一批第二批",
            "tags": [],
            "content": {"onebot": [{"type": "text", "data": {"text": "能都报吗"}}]},
            "source": {"chat_type": "group", "chat_id": 1, "message_ids": [1]},
        },
        act_as=OTHER,
    )
    await s.round(message(1, ASKER, "预推免第一批和第二批能都报吗"))
    t = s.forum.threads[older["id"]]
    assert (len(s.forum.threads), t.posts[-1].author) == (1, ASKER)
    system, prompt = s.llm.prompts[1]
    assert "C1. 预推免第一批第二批" in prompt


async def test_someone_who_turned_archiving_off_is_left_out_with_answers_to_them(setup):
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": []}))
    s.forum.proxy_disabled.add(ASKER)
    report = await s.round(*first_round())
    assert s.forum.threads == {}
    assert report["dropped"] == ["new N1: forum proxy_disabled", "new N2: N1 was not posted"]


async def test_questions_are_followed_for_a_while_then_found_again_by_quotes(setup):
    # Seven rounds of chatter, then two more.
    s = await asked(setup, *[{"new": [], "edit": []}] * 9)
    chatter = [message(10 + i, OTHER, "哈哈") for i in range(TUNING.follow_messages)]
    await s.round(*chatter)
    chat = s.archive.chats[GROUP]
    assert len(chat.following) == 1
    await s.round(message(500, OTHER, "哈哈"))
    assert chat.following == []

    await s.round(message(501, OTHER, "那第二批呢", reply=3))
    assert len(chat.following) == 1
    assert "Q1 标题" in s.llm.prompts[-1][1]


async def test_a_quote_of_a_message_no_longer_kept_fetches_its_thread_to_go_by(setup):
    edit = {"post": "Q1", "messages": [1], "title": None, "body": "改了"}
    s = setup(Script({"new": [], "edit": [edit]}))
    forum = s.forum.client()
    await forum.call(
        "POST",
        "/threads",
        body={
            "board": "qa",
            "kind": "question",
            "title": "很早以前的问题",
            "tags": [],
            "content": {"onebot": [{"type": "text", "data": {"text": "早先的正文"}}]},
            "source": {"chat_type": "group", "chat_id": GROUP, "message_ids": [7]},
        },
        act_as=ASKER,
    )
    report = await s.round(message(1, ASKER, "我又想到一点", reply=7))
    prompt = s.llm.prompts[0][1]
    assert "Q1 标题：很早以前的问题" in prompt and "Q1（论坛上）：早先的正文" in prompt
    assert report["dropped"] == ["edit Q1: not a floor that can be edited"]


async def test_a_recalled_message_still_waiting_leaves_its_round(setup):
    s = setup(Script())
    s.archive.add(GROUP, message(1, ASKER, "发错了"), BOT)
    s.archive.recall(GROUP, 1, ASKER)
    assert s.archive.chats[GROUP].pending == []


async def test_a_recalled_message_leaves_its_floor_organized_anew(setup):
    s = await asked(setup, {"title": "预推免两批能都报吗", "body": "能都报吗？"})
    s.archive.recall(GROUP, 1, ASKER)
    await s.archive.handle(s.archive.queue.get_nowait())
    opening = thread_of(s).posts[0]
    assert (opening.body, opening.sources) == ("能都报吗？", [2])
    assert "能都报吗" in s.llm.prompts[1][1] and "大家预推免" not in s.llm.prompts[1][1]


async def test_a_floor_of_only_the_recalled_message_is_deleted(setup):
    s = await asked(setup)
    s.archive.recall(GROUP, 3, ANSWERER)
    await s.archive.handle(s.archive.queue.get_nowait())
    assert thread_of(s).posts[1].deleted


async def test_a_recalled_message_no_longer_kept_deletes_the_floor_it_became(setup):
    s = setup(Script())
    forum = s.forum.client()
    thread = await forum.call(
        "POST",
        "/threads",
        body={
            "board": "qa",
            "kind": "question",
            "title": "很早以前的问题",
            "tags": [],
            "content": {"onebot": [{"type": "text", "data": {"text": "正文"}}]},
            "source": {"chat_type": "group", "chat_id": GROUP, "message_ids": [7]},
        },
        act_as=ASKER,
    )
    s.archive.recall(GROUP, 7, ASKER)
    await s.archive.handle(s.archive.queue.get_nowait())
    assert s.forum.threads[thread["id"]].deleted


# The test embedder finds every thread alike, so each new one is checked against those there.
NOT_SEEN = {"same": None}


def texts(segments):
    return "".join(
        s["data"]["text"] if s["type"] == "text" else f"@{s['data']['qq']}" for s in segments
    )


async def test_a_discussion_becomes_a_thread_of_whoever_started_it(setup):
    started = {**QUESTION, "type": "discussion", "messages": [1], "title": "今年夏令营机试改了题型"}
    reply = {**ANSWER, "messages": [2]}
    s = setup(Script({"new": [started, reply], "edit": []}))
    await s.round(
        message(1, ASKER, "听说今年机试改成项目题了"), message(2, ANSWERER, "我们学校也是")
    )
    t = thread_of(s)
    assert (t.kind, t.author, [p.author for p in t.posts]) == (
        "discussion",
        ASKER,
        [ASKER, ANSWERER],
    )
    [(group, segments)] = s.sent
    assert group == GROUP
    assert texts(segments) == (
        f"@{ASKER} 这个讨论收录到鼠洞了：今年夏令营机试改了题型\nhttps://forum.test/t/{t.id}"
    )


async def test_a_round_sends_one_receipt_naming_each_new_thread_and_question_asked_before(setup):
    again = {**QUESTION, "ref": "N1", "messages": [4], "same_as": "Q1"}
    other = {**QUESTION, "ref": "N2", "messages": [5], "title": "夏令营要带简历吗", "same_as": None}
    s = await asked(setup, {"new": [again, other], "edit": []}, NOT_SEEN)
    first = thread_of(s)
    await s.round(
        message(4, OTHER, "预推免两批可以都报吗"), message(5, ANSWERER, "夏令营要带简历吗")
    )
    assert [p.author for p in first.posts] == [ASKER, ANSWERER, OTHER]
    second = max(s.forum.threads)
    _, segments = s.sent[-1]
    assert texts(segments) == (
        f"@{OTHER} 这个问题鼠洞上有人问过，已经有 1 条回复：{QUESTION['title']}\n"
        f"https://forum.test/t/{first.id}\n"
        f"@{ANSWERER} 你的问题收录到鼠洞了：夏令营要带简历吗\nhttps://forum.test/t/{second}"
    )
    assert len(s.sent) == 2


async def test_receipts_stop_for_the_hour_after_four_and_threads_are_archived_still(setup):
    def round_of(i):
        return {"new": [{**QUESTION, "messages": [i], "title": f"第 {i} 个问题来了"}], "edit": []}

    later = [answer for i in range(2, 7) for answer in (round_of(i), NOT_SEEN)]
    s = setup(Script(round_of(1), *later))
    for i in range(1, 6):
        s.now = i * 60
        report = await s.round(message(i, ASKER, f"问题 {i}"))
    assert (len(s.forum.threads), len(s.sent)) == (5, 4)
    assert report["dropped"] == ["receipt: 4 sent this hour"]
    s.now = 3600 + 61
    await s.round(message(6, ASKER, "问题 6"))
    assert len(s.sent) == 5


async def test_a_question_asked_in_a_thread_since_locked_or_deleted_gets_its_own(setup):
    again = {**QUESTION, "messages": [4], "same_as": "Q1"}
    # Q2 is the thread the first asking again got.
    again_later = {**again, "messages": [5], "same_as": "Q2"}
    s = await asked(setup, {"new": [again], "edit": []}, {"new": [again_later], "edit": []})
    first = thread_of(s)
    first.locked = True
    await s.round(message(4, OTHER, "预推免两批可以都报吗"))
    assert len(s.forum.threads) == 2
    newer = s.forum.threads[max(s.forum.threads)]
    newer.deleted = True
    await s.round(message(5, OTHER, "预推免两批真的都能报吗"))
    assert len(s.forum.threads) == 3


async def test_new_tags_are_made_once_and_only_so_many_a_day(setup):
    def item(i, *names):
        new = [{"name": n, "kind": "school"} for n in names]
        return {
            **QUESTION,
            "ref": f"N{i}",
            "messages": [i],
            "title": f"第 {i} 个问题来了",
            "tags": list(names),
            "new_tags": new,
        }

    names = [f"学校{i}" for i in range(10)]
    s = setup(
        Script(
            {"new": [item(1, "华东师大", "预推免")], "edit": []},
            {"new": [item(2, *names[:5]), item(3, *names[5:9])], "edit": []},
            NOT_SEEN,
            NOT_SEEN,
            {"new": [item(4, names[9])], "edit": []},
            NOT_SEEN,
        )
    )
    await s.round(message(1, ASKER, "华东师大预推免"))
    assert thread_of(s).tags == ["华东师大", "预推免"]
    # The test forum knows 夏令营 and 预推免 to begin with.
    assert s.forum.tags.count("华东师大") == 1

    await s.round(message(2, ASKER, "a"), message(3, ASKER, "b"))
    report = await s.round(message(4, OTHER, "c"))
    assert len(s.forum.tags) == 2 + 10
    assert s.forum.threads[max(s.forum.threads)].tags == []
    assert report["dropped"] == ["tag 学校9: 10 made today already"]


async def test_a_round_with_enough_messages_closes_once_the_group_falls_quiet(setup):
    s = setup(Script())
    a = s.archive
    a.tuning = Tuning(round_pause_minutes=2, round_least=3)
    for i in range(1, 3):
        a.add(GROUP, message(i, ASKER, "嗯"), BOT)
    chat = a.chats[GROUP]
    last = chat.pending[-1].time
    a.tick(last + 120)
    assert a.queue.empty()
    a.add(GROUP, message(3, ASKER, "嗯"), BOT)
    a.tick(chat.pending[-1].time + 119)
    assert a.queue.empty()
    a.tick(chat.pending[-1].time + 120)
    assert a.queue.qsize() == 1
