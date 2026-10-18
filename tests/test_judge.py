import pytest
from conftest import message

from mousehole import judge
from mousehole.archive import Floor, Question

QUESTION = {
    "ref": "N1",
    "type": "question",
    "messages": [1],
    "title": "夏令营要准备哪些材料",
    "body": "夏令营要准备哪些材料？",
    "board": "qa",
    "tags": [],
    "same_as": None,
}


def numbered(*messages):
    for i, m in enumerate(messages, 1):
        m.number = i
    return {m.number: m for m in messages}


def check(raw, numbers, questions=None, floors=None):
    return judge.check(raw, numbers, questions or {}, floors or {}, {"qa"}, {"夏令营"}, {1: "U1"})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"board": "nope"}, "unknown board nope"),
        ({"title": "问"}, "no usable title"),
        ({"messages": [1, 2]}, "messages by more than one person"),
        ({"author": "U2"}, "messages not by U2"),
        ({"messages": [9]}, "unknown message 9"),
        ({"same_as": "Q3"}, "unknown same_as Q3"),
        ({"body": " "}, "no body"),
    ],
)
def test_unsound_questions_are_left_out(change, reason):
    numbers = numbered(message(1, 1, "a"), message(2, 2, "b"))
    plan = check({"new": [QUESTION | change]}, numbers)
    assert (plan.questions, plan.dropped) == ([], [f"new N1: {reason}"])


def test_titles_are_cut_and_unknown_tags_dropped():
    numbers = numbered(message(1, 1, "a"))
    item = QUESTION | {"title": "长" * 40, "tags": ["夏令营", "编的"]}
    (question,) = check({"new": [item]}, numbers).questions
    assert (len(question.title), question.tags) == (judge.TITLE_CHARS, ["夏令营"])


def test_a_floors_messages_are_in_the_order_they_were_said():
    numbers = numbered(message(1, 1, "a"), message(2, 1, "b"))
    (question,) = check({"new": [QUESTION | {"messages": [2, 1]}]}, numbers).questions
    assert [m.number for m in question.messages] == [1, 2]


def test_floors_and_boosts_end_without_a_full_stop():
    numbers = numbered(message(1, 1, "a"), message(2, 2, "b"), message(3, 3, "c"))
    answer = {"ref": "N2", "type": "reply", "question": "N1", "messages": [2], "body": "可以。"}
    react = {"message": 3, "post": "N2", "emoji": None, "boost": "学到了。"}
    plan = check({"new": [QUESTION, answer], "react": [react]}, numbers)
    assert (plan.questions[0].body, plan.answers[0].body) == ("夏令营要准备哪些材料？", "可以")
    assert plan.reactions[0].boost == "学到了"


def test_a_picture_left_out_goes_with_what_its_sender_said_around_it():
    picture = [{"type": "image", "data": {"url": "x"}}]
    numbers = numbered(
        message(1, 1, "a"),
        message(2, 2, segments=picture),
        message(3, 2, "b"),
        message(4, 1, segments=picture),
    )
    answer = {"ref": "N2", "type": "reply", "question": "N1", "messages": [3], "body": "b"}
    plan = check({"new": [QUESTION, answer]}, numbers)
    # Not 4, said after someone else spoke.
    assert [m.number for m in plan.questions[0].messages] == [1]
    assert [m.number for m in plan.answers[0].messages] == [2, 3]


def test_a_message_goes_into_one_floor_only():
    numbers = numbered(message(1, 1, "a"))
    answer = {"ref": "N2", "type": "reply", "question": "N1", "messages": [1], "body": "x"}
    plan = check({"new": [QUESTION, answer]}, numbers)
    assert (len(plan.questions), plan.answers) == (1, [])
    assert plan.dropped == ["new N2: #1 is archived already"]


def test_edits_take_only_their_writers_new_messages():
    numbers = numbered(message(1, 1, "a"), message(2, 2, "b"), message(3, 1, "c"))
    q = Question("夏令营要准备什么", opening=True, last=0)
    q.floor = Floor(10, 1, 1, [numbers[1]], "a", None, q)
    numbers[1].floor = q.floor
    edit = {"post": "Q1", "messages": [1, 3], "title": "新标题新标题", "body": "a c"}
    plan = check({"edit": [edit, edit | {"messages": [2]}]}, numbers, {"Q1": q}, {"Q1": q.floor})
    assert [(e.messages, e.title) for e in plan.edits] == [([numbers[3]], "新标题新标题")]
    assert plan.dropped == ["edit Q1: messages by more than one person"]


def test_an_edit_of_a_floor_made_in_the_same_answer_goes_into_it():
    numbers = numbered(message(1, 1, "a"), message(2, 2, "b"), message(3, 1, "c"))
    edit = {"post": "N1", "messages": [1, 3], "title": None, "body": "a 和 c"}
    plan = check({"new": [QUESTION], "edit": [edit, edit | {"messages": [2]}]}, numbers)
    (question,) = plan.questions
    assert ([m.number for m in question.messages], question.body) == ([1, 3], "a 和 c")
    assert question.title == QUESTION["title"]
    assert (plan.edits, plan.dropped) == ([], ["edit N1: messages by more than one person"])


def test_json_is_found_in_what_the_model_wraps_it_in():
    assert judge.extract_json('好的：\n```json\n{"new": []}\n```') == {"new": []}
    with pytest.raises(ValueError):
        judge.extract_json("没有要收的")


def test_new_tags_must_be_short_of_a_known_kind_and_not_had_already():
    new_tags = [
        {"name": "华东师范大学", "kind": "school"},
        {"name": "夏令营", "kind": "topic"},
        {"name": "一个特别特别特别特别特别特别特别长的标签名字", "kind": "topic"},
        {"name": "推免", "kind": "nope"},
    ]
    tags = [
        "华东师范大学",
        "夏令营",
        "一个特别特别特别特别特别特别特别长的标签名字",
        "推免",
        "没说要建的",
    ]
    raw = {"new": [{**QUESTION, "tags": tags, "new_tags": new_tags}], "edit": []}
    [item] = check(raw, numbered(message(1, 1, "问"))).questions
    assert (item.tags, item.new_tags) == (["华东师范大学", "夏令营"], [("华东师范大学", "school")])


def test_a_reply_naming_its_new_thread_replies_to_the_opening_post():
    reply = {"ref": "N2", "type": "reply", "question": "N1", "messages": [2], "body": "可以"}
    raw = {
        "new": [
            QUESTION,
            {**reply, "reply_to": "N1"},
            {**reply, "ref": "N3", "messages": [3], "reply_to": "N9"},
        ],
        "edit": [],
    }
    plan = check(raw, numbered(message(1, 1, "问"), message(2, 2, "可以"), message(3, 3, "嗯")))
    assert [(a.ref, a.reply_to) for a in plan.answers] == [("N2", None)]
    assert plan.dropped == ["new N3: unknown reply_to N9"]
