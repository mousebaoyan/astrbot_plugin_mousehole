from contextlib import aclosing
from datetime import datetime

import pytest
from conftest import BOT, GROUP, Script, message

from mousehole.archive import (
    BACKLOG,
    LEAST,
    LONGEST,
    QUIET,
    Message,
    Tuning,
    content,
    load,
    segments,
    to_take,
    until,
)
from mousehole.llm import Truncated, Unavailable

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


def first_batch():
    return [
        message(1, ASKER, "大家预推免第一批和第二批"),
        message(2, ASKER, "能都报吗"),
        message(3, ANSWERER, "看学校，有些报了不好撤回", reply=2),
    ]


async def asked(setup, *later_answers: object):
    """A setup whose first batch asked QUESTION and got ANSWER."""
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": []}, *later_answers))
    await s.batch(*first_batch())
    return s


def thread_of(s):
    return next(iter(s.forum.threads.values()))


async def test_messages_wait_on_disk_with_their_pictures_copied_and_chatter_left_out(
    setup, tmp_path
):
    s = setup(Script())
    a = s.archive
    picture = {"type": "image", "data": {"sub_type": 0, "url": "https://gchat.qpic.cn/2"}}
    sticker = {"type": "image", "data": {"sub_type": 1, "url": "https://gchat.qpic.cn/1"}}
    await a.add(GROUP, message(1, ASKER, "不是"), BOT)
    await a.add(GROUP, message(2, BOT, "bot 自己说的"), BOT)
    await a.add(GROUP, message(3, ASKER, "/help"), BOT)
    await a.add(GROUP, message(4, ASKER, segments=[{"type": "face", "data": {"id": "1"}}]), BOT)
    await a.add(GROUP, message(5, ASKER, reply=1, segments=[sticker]), BOT)
    await a.add(GROUP, message(6, ASKER, segments=[picture]), BOT)
    await a.add(GROUP, message(7, ASKER, "发错了"), BOT)
    a.recall(GROUP, 7)
    a.start_round(everything=True)
    [path] = (tmp_path / "batch").iterdir()
    assert [(m.id, len(m.uploads)) for m in load(path)] == [(1, 0), (6, 1)]
    assert s.forum.uploads == 1


def test_a_batch_is_cut_where_the_group_was_quiet_longest_and_small_pieces_joined():
    def at(*times):
        return [Message(i, ASKER, t, []) for i, t in enumerate(times)]

    def cut(messages, most):
        return [[m.time for m in piece] for piece in segments(messages, most)]

    # 0–2 is one talk, 100–101 another; the lone 50 joins the talk before it, which has room.
    assert cut(at(0, 1, 2, 50, 100, 101), 4) == [[0, 1, 2, 50], [100, 101]]
    assert cut(at(0, 1, 10, 11, 30), 2) == [[0, 1], [10, 11], [30]]
    assert cut(at(0, 1), 50) == [[0, 1]]


def test_the_next_of_some_hours_is_found_across_midnight():
    def at(hour, minute=0):
        return datetime(2026, 10, 15, hour, minute).timestamp()

    assert until((4, 18), at(17, 30)) == 1800
    assert until((4, 18), at(18)) == 10 * 3600
    assert until((4, 18), at(3)) == 3600


def test_a_group_is_taken_in_once_its_talk_stops_with_enough_said():
    def said(*times):
        return [Message(i, ASKER, t, []) for i, t in enumerate(times)]

    talk = said(*range(LEAST))
    end = talk[-1].time
    assert to_take(talk, end + QUIET - 1) == 0
    assert to_take(talk, end + QUIET) == LEAST
    # A few wait for more, but not for ever.
    assert to_take(talk[:3], end + QUIET) == 0
    assert to_take(talk[:3], LONGEST) == 3
    assert to_take([], LONGEST) == 0


def test_a_backlog_is_taken_up_to_a_lull_leaving_the_talk_going_on():
    # Quiet longest after the 30th, and longer still after the 2nd, which would take too few,
    # and after the 140th, which would cut into the talk going on.
    times = [*range(BACKLOG)]
    gaps = {2: 500, 30: 100, 140: 900}
    times = [t + sum(g for i, g in gaps.items() if n >= i) for n, t in enumerate(times)]
    talk = [Message(i, ASKER, t, []) for i, t in enumerate(times)]
    assert to_take(talk, talk[-1].time) == 30
    assert to_take(talk[:-1], talk[-1].time) == 0


async def test_a_batch_a_restart_broke_off_goes_on_from_the_next_segment(setup, tmp_path):
    s = setup(Script({"new": [], "edit": []}), tuning=Tuning(segment_messages=2))
    await s.archive.refresh_taxonomy()
    for i in range(1, 5):
        await s.archive.add(GROUP, message(i * 100, ASKER, f"第 {i} 条"), BOT)
    s.archive.start_round(everything=True)
    async with aclosing(s.archive.batch()) as batch:
        await anext(batch)

    again = setup(Script({"new": [], "edit": []}))
    await again.batch()
    [(_, prompt)] = again.llm.prompts
    assert "第 3 条" in prompt and "第 4 条" in prompt and "第 2 条" not in prompt
    assert list((tmp_path / "batch").iterdir()) == []


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


async def test_thanks_and_me_too_become_reactions_and_boosts_as_said_in_the_group(setup):
    react = [
        {"message": 4, "post": "N1", "emoji": "+1", "boost": "同问"},
        {"message": 5, "post": "N2", "emoji": "heart", "boost": None},
        {"message": 6, "post": "N1", "emoji": "+1", "boost": None},
        {"message": 3, "post": "N1", "emoji": "clap", "boost": "看学校"},
    ]
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": [], "react": react}))
    report = await s.batch(
        *first_batch(),
        message(4, OTHER, "同问", reply=2),
        message(5, ASKER, "谢谢佬", reply=3),
        message(6, ASKER, "顶一下"),
    )
    opening, reply = thread_of(s).posts
    # The answer claps the question besides, without its words again.
    assert opening.reactions == {OTHER: "+1", ANSWERER: "clap"}
    assert opening.boosts == {OTHER: "同问"}
    assert (reply.reactions, reply.boosts) == ({ASKER: "heart"}, {})
    assert report["dropped"] == ["react #6: their own floor"]
    # Dated as said in the group, as the floors are.
    assert (opening.created_at, reply.created_at) == (message(1, 0).time, message(3, 0).time)


async def test_a_joke_at_a_floor_is_a_boost_without_a_reaction(setup):
    react = [
        {"message": 4, "post": "N2", "emoji": None, "boost": "这下真得卷起来了"},
        {"message": 5, "post": "N2", "emoji": None, "boost": None},
    ]
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": [], "react": react}))
    report = await s.batch(
        *first_batch(),
        message(4, OTHER, "这下真得卷起来了", reply=3),
        message(5, ASKER, "嗯", reply=3),
    )
    reply = thread_of(s).posts[1]
    assert (reply.reactions, reply.boosts) == ({}, {OTHER: "这下真得卷起来了"})
    assert report["dropped"] == ["react #5: nothing to leave"]


async def test_what_the_asker_adds_later_rewrites_the_opening_and_its_title(setup):
    edit = {"post": "Q1", "messages": [4], "title": "rank 10% 预推免两批能都报吗", "body": "新正文"}
    s = await asked(setup, {"new": [], "edit": [edit]})
    report = await s.batch(message(4, ASKER, "我 rank 前 10%", reply=3))

    assert "Q1 标题：预推免第一批和第二批能都报吗" in s.llm.prompts[1][1]
    t = thread_of(s)
    assert (t.title, t.posts[0].body, t.posts[0].sources) == (edit["title"], "新正文", [1, 2, 4])
    assert report["done"] == [f"edit {t.posts[0].id}"]


async def test_messages_archived_already_are_not_posted_again(setup):
    again = {**QUESTION, "messages": [1, 4], "title": "又一个标题哦"}
    s = await asked(setup, {"new": [again], "edit": []})
    report = await s.batch(message(4, ASKER, "有人知道吗"))
    assert len(s.forum.threads) == 1
    assert "new N1: #1 is archived already" in report["dropped"]


async def test_a_floor_changed_on_the_forum_is_left_to_its_writer(setup):
    edit = {"post": "Q1", "messages": [4], "title": None, "body": "新正文"}
    s = await asked(setup, {"new": [], "edit": [edit]})
    thread_of(s).posts[0].edited_at = 123
    report = await s.batch(message(4, ASKER, "补充一下", reply=1))
    assert thread_of(s).posts[0].body == QUESTION["body"]
    assert report["dropped"] == [f"edit {thread_of(s).posts[0].id}: changed on the forum"]


async def test_the_same_question_asked_again_goes_into_the_thread_as_a_floor(setup):
    again = {**QUESTION, "messages": [4], "same_as": "Q1"}
    answer = {**ANSWER, "question": "N1", "messages": [5], "ref": "N2"}
    s = await asked(setup, {"new": [{**again, "ref": "N1"}, answer], "edit": []})
    await s.batch(message(4, OTHER, "预推免两批可以都报吗"), message(5, ANSWERER, "可以"))
    t = thread_of(s)
    asked_again, answered = t.posts[2:]
    assert (len(s.forum.threads), asked_again.author, asked_again.sources) == (1, OTHER, [4])
    assert answered.reply_to == asked_again.id


async def thread_asked_at(s, time: int) -> int:
    older = await s.forum.client().call(
        "POST",
        "/threads",
        body={
            "board": "qa",
            "kind": "question",
            "title": "预推免第一批第二批",
            "tags": [],
            "content": {"onebot": [{"type": "text", "data": {"text": "能都报吗"}}]},
            "source": {"chat_type": "group", "chat_id": 1, "message_ids": [1]},
            "created_at": time,
        },
        act_as=OTHER,
    )
    return older["id"]


async def test_a_question_some_older_thread_asks_goes_there(setup):
    s = setup(Script({"new": [{**QUESTION, "messages": [1]}], "edit": []}, {"same": "C1"}))
    asked = message(2, ASKER, "预推免第一批和第二批能都报吗")
    older = await thread_asked_at(s, int(asked.time) - 3600)
    await s.batch(asked)
    t = s.forum.threads[older]
    assert (len(s.forum.threads), t.posts[-1].author) == (1, ASKER)
    system, prompt = s.llm.prompts[1]
    assert "C1. 预推免第一批第二批" in prompt


async def test_a_thread_talked_in_after_the_question_was_asked_is_no_candidate(setup):
    s = setup(Script({"new": [{**QUESTION, "messages": [1]}], "edit": []}))
    asked = message(2, ASKER, "预推免第一批和第二批能都报吗")
    await thread_asked_at(s, int(asked.time) + 3600)
    await s.batch(asked)
    assert (len(s.forum.threads), len(s.llm.prompts)) == (2, 1)


async def test_someone_who_turned_archiving_off_is_left_out_with_answers_to_them(setup):
    s = setup(Script({"new": [QUESTION, ANSWER], "edit": []}))
    s.forum.proxy_disabled.add(ASKER)
    report = await s.batch(*first_batch())
    assert s.forum.threads == {}
    assert report["dropped"] == ["new N1: forum proxy_disabled", "new N2: N1 was not posted"]


async def test_questions_are_followed_for_a_while_then_found_again_by_quotes(setup):
    # Four segments of chatter, then two batches more.
    s = await asked(setup, *[{"new": [], "edit": []}] * 9)
    chatter = [message(10 + i, OTHER, "哈哈") for i in range(TUNING.follow_messages)]
    await s.batch(*chatter)
    chat = s.archive.chats[GROUP]
    assert len(chat.following) == 1
    await s.batch(message(500, OTHER, "哈哈"))
    assert chat.following == []

    await s.batch(message(501, OTHER, "那第二批呢", reply=3))
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
    report = await s.batch(message(1, ASKER, "我又想到一点", reply=7))
    prompt = s.llm.prompts[0][1]
    assert "Q1 标题：很早以前的问题" in prompt and "Q1（论坛上）：早先的正文" in prompt
    assert report["dropped"] == ["edit Q1: not a floor that can be edited"]


async def test_messages_missed_while_down_are_read_back_from_the_groups_history(setup):
    def onebot(m):
        return {
            "message_id": m.id,
            "user_id": m.user,
            "time": m.time,
            "message": m.segments,
            "self_id": BOT,
        }

    heard, missed = message(10, ASKER, "听到了的"), message(12, ANSWERER, "漏掉的")
    older = message(2, OTHER, "上一批的")
    # The same message under another ID is heard all the same.
    renamed = Message(99, heard.user, heard.time, heard.segments)
    pages = {None: [renamed, message(11, OTHER, "也漏掉了"), missed], 99: [older, renamed]}
    asked = []

    async def history(group, before):
        asked.append((group, before))
        return [onebot(m) for m in pages[before]]

    s = setup(Script(), history=history)
    s.archive.state["since"][str(GROUP)] = message(5, 0).time
    await s.archive.add(GROUP, heard, BOT)
    await s.archive.catch_up()
    pending = load(s.archive.pending / f"{GROUP}.jsonl")
    assert [m.id for m in pending] == [10, 11, 12]
    # Back page by page till what the last round took in.
    assert asked == [(GROUP, None), (GROUP, 99)]


async def test_a_summary_missed_while_down_goes_right_away(setup):
    s = setup(Script())
    day = datetime(2026, 10, 16).timestamp()
    s.archive.state["summary"] = day - 16 * 3600
    s.now = day + 7 * 3600
    assert not s.archive.due("summary", (8,))
    s.now = day + 9 * 3600
    assert s.archive.due("summary", (8,))


async def test_a_round_takes_in_what_is_due_and_leaves_the_rest_waiting(setup, tmp_path):
    s = setup(Script())
    for i in range(BACKLOG):
        await s.archive.add(GROUP, message(i * 10 + (500 if i >= 40 else 0), ASKER, "聊"), BOT)
    s.now = message(BACKLOG * 10 + 500, 0).time
    s.archive.start_round()
    [taken] = (tmp_path / "batch").iterdir()
    assert len(load(taken)) == 40
    assert len(load(s.archive.pending / f"{GROUP}.jsonl")) == BACKLOG - 40
    assert s.archive.state["since"][str(GROUP)] == message(390, 0).time


async def test_a_segment_a_service_failed_waits_in_its_batch_to_be_tried_again(setup):
    question = {"new": [{**QUESTION, "messages": [1]}], "edit": []}
    nothing = {"new": [], "edit": []}
    s = setup(Script(question, Unavailable("down"), nothing), tuning=Tuning(segment_messages=1))
    await s.archive.refresh_taxonomy()
    await s.archive.add(GROUP, message(1, ASKER, "预推免两批能都报吗"), BOT)
    await s.archive.add(GROUP, message(1000, OTHER, "哈哈"), BOT)
    s.archive.start_round(everything=True)
    with pytest.raises(Unavailable):
        async for _ in s.archive.batch():
            pass
    [report] = [r async for r in s.archive.batch()]
    assert report["numbers"] == [2, 2] and "哈哈" in s.llm.prompts[-1][1]
    assert (len(s.forum.threads), len(s.archive.summary[GROUP])) == (1, 1)


async def test_a_segment_thought_too_long_over_is_split_in_halves(setup):
    nothing = {"new": [], "edit": []}
    s = setup(Script(Truncated(5000, 32768), nothing, nothing))
    await s.archive.refresh_taxonomy()
    for i in (1, 2, 100, 101):
        await s.archive.add(GROUP, message(i, ASKER, f"第 {i} 条"), BOT)
    s.archive.start_round(everything=True)
    reports = [r async for r in s.archive.batch()]
    assert [r["numbers"] for r in reports] == [[1, 4], [1, 2], [3, 4]]
    assert reports[0]["llm"][0]["output_tokens"] == 32768


async def test_a_message_recalled_while_its_batch_is_worked_on_is_left_out(setup):
    s = setup(
        Script({"new": [], "edit": []}, {"new": [], "edit": []}), tuning=Tuning(segment_messages=1)
    )
    await s.archive.refresh_taxonomy()
    await s.archive.add(GROUP, message(1, ASKER, "第一段"), BOT)
    await s.archive.add(GROUP, message(1000, ASKER, "发错了"), BOT)
    s.archive.start_round(everything=True)
    async for _ in s.archive.batch():
        s.archive.recall(GROUP, 1000)
    assert len(s.llm.prompts) == 1


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
    await s.batch(
        message(1, ASKER, "听说今年机试改成项目题了"), message(2, ANSWERER, "我们学校也是")
    )
    t = thread_of(s)
    assert (t.kind, t.author, [p.author for p in t.posts]) == (
        "discussion",
        ASKER,
        [ASKER, ANSWERER],
    )
    await s.archive.send_summary()
    [(_, segments)] = s.sent
    assert texts(segments).endswith(
        f"1. @{ASKER}\n（讨论）今年夏令营机试改了题型\nhttps://forum.test/t/{t.id}"
    )


async def test_the_summary_names_each_new_thread_but_not_questions_asked_before(setup):
    again = {**QUESTION, "ref": "N1", "messages": [4], "same_as": "Q1"}
    other = {**QUESTION, "ref": "N2", "messages": [5], "title": "夏令营要带简历吗", "same_as": None}
    s = await asked(setup, {"new": [again, other], "edit": []}, NOT_SEEN)
    first = thread_of(s)
    await s.batch(
        message(4, OTHER, "预推免两批可以都报吗"), message(5, ANSWERER, "夏令营要带简历吗")
    )
    assert [p.author for p in first.posts] == [ASKER, ANSWERER, OTHER]
    assert s.sent == []

    await s.archive.send_summary()
    second = max(s.forum.threads)
    [(group, segments)] = s.sent
    assert group == GROUP
    # The one asked before was told as it was asked (猜你想问).
    assert texts(segments) == (
        "鼠洞收录了群里的 2 个帖子：\n"
        f"1. @{ASKER}\n{QUESTION['title']}\nhttps://forum.test/t/{first.id}\n"
        f"2. @{ANSWERER}\n夏令营要带简历吗\nhttps://forum.test/t/{second}"
    )
    await s.archive.send_summary()
    assert len(s.sent) == 1


async def test_the_summary_waits_on_disk_and_names_ten_at_most(setup):
    entry = {"user": ASKER, "kind": "question", "thread": 1, "title": "问题"}
    s = setup(Script())
    s.archive.remember(GROUP, [entry] * 12)

    again = setup(Script())
    await again.archive.send_summary()
    [(_, segments)] = again.sent
    assert texts(segments).count("问题") == 10
    assert texts(segments).endswith("还有 2 个，到鼠洞上看：https://forum.test")
    assert again.archive.summary == {}


async def test_a_question_asked_in_a_thread_since_locked_or_deleted_gets_its_own(setup):
    again = {**QUESTION, "messages": [4], "same_as": "Q1"}
    # Q2 is the thread the first asking again got.
    again_later = {**again, "messages": [5], "same_as": "Q2"}
    s = await asked(setup, {"new": [again], "edit": []}, {"new": [again_later], "edit": []})
    first = thread_of(s)
    first.locked = True
    await s.batch(message(4, OTHER, "预推免两批可以都报吗"))
    assert len(s.forum.threads) == 2
    newer = s.forum.threads[max(s.forum.threads)]
    newer.deleted = True
    await s.batch(message(5, OTHER, "预推免两批真的都能报吗"))
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
    await s.batch(message(1, ASKER, "华东师大预推免"))
    assert thread_of(s).tags == ["华东师大", "预推免"]
    # The test forum knows 夏令营 and 预推免 to begin with.
    assert s.forum.tags.count("华东师大") == 1

    await s.batch(message(2, ASKER, "a"), message(3, ASKER, "b"))
    report = await s.batch(message(4, OTHER, "c"))
    assert len(s.forum.tags) == 2 + 10
    assert s.forum.threads[max(s.forum.threads)].tags == []
    assert report["dropped"] == ["tag 学校9: 10 made today already"]


async def test_a_token_that_may_not_make_tags_posts_the_thread_without_the_new_one(setup):
    item = {
        **QUESTION,
        "tags": ["预推免", "华东师大"],
        "new_tags": [{"name": "华东师大", "kind": "school"}],
    }
    s = setup(Script({"new": [{**item, "messages": [1]}], "edit": []}))
    s.forum.moderate = False
    report = await s.batch(message(1, ASKER, "华东师大预推免"))
    assert thread_of(s).tags == ["预推免"]
    assert report["dropped"] == ["tag 华东师大: forum scope_missing"]


def test_pictures_go_where_the_body_kept_them():
    shot = Message(1, ASKER, 0, [], uploads=["/uploads/a.webp", "/uploads/b.webp"])
    body = "看这张：[图片]分数线在这。[图片][图片]"
    assert content([shot], body) == {
        "markdown": "看这张：![](/uploads/a.webp)分数线在这。![](/uploads/b.webp)"
    }
    assert content([shot], "分数线在这。")["markdown"] == (
        "分数线在这。\n\n![](/uploads/a.webp)\n\n![](/uploads/b.webp)"
    )


async def test_replies_are_posted_in_the_order_they_were_said(setup):
    later = {**ANSWER, "ref": "N3", "messages": [4], "body": "我也想知道"}
    s = setup(Script({"new": [QUESTION, later, ANSWER], "edit": []}))
    await s.batch(*first_batch(), message(4, OTHER, "我也想知道"))
    assert [p.created_at for p in thread_of(s).posts] == [message(i, 0).time for i in (1, 3, 4)]


async def test_a_reply_said_before_its_thread_was_opened_is_dated_no_earlier(setup):
    question = {**QUESTION, "messages": [2, 3]}
    early = {**ANSWER, "messages": [1], "body": "先说一句"}
    s = setup(Script({"new": [question, early], "edit": []}))
    await s.batch(
        message(1, ANSWERER, "先说一句"),
        message(2, ASKER, "大家预推免第一批和第二批"),
        message(3, ASKER, "能都报吗"),
    )
    opening, reply = thread_of(s).posts
    assert reply.created_at == opening.created_at == message(2, 0).time
