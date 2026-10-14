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
    return judge.check(raw, numbers, questions or {}, floors or {}, {"qa"}, {"夏令营"})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"board": "nope"}, "unknown board nope"),
        ({"title": "问"}, "no usable title"),
        ({"messages": [1, 2]}, "messages by more than one person"),
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


def test_a_message_goes_into_one_floor_only():
    numbers = numbered(message(1, 1, "a"))
    answer = {"ref": "N2", "type": "answer", "question": "N1", "messages": [1], "body": "x"}
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


def test_json_is_found_in_what_the_model_wraps_it_in():
    assert judge.extract_json('好的：\n```json\n{"new": []}\n```') == {"new": []}
    with pytest.raises(ValueError):
        judge.extract_json("没有要收的")
