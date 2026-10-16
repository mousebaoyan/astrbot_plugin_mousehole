import json
from datetime import datetime

from conftest import GROUP, Script, embed

from mousehole.archive import Tuning
from mousehole.similar import Index
from tools.fake_forum import FakeForum
from tools.history import archive_exports

ASKER, ANSWERER = 10001, 10002
MORNING = datetime(2026, 10, 10, 10).timestamp()
EVENING = datetime(2026, 10, 10, 20).timestamp()


def exported(seq, uin, at, elements):
    return {
        "id": str(7690000000000000000 + seq),
        "seq": str(seq),
        "timestamp": int(at * 1000),
        "sender": {"uin": str(uin), "name": f"鼠{uin}"},
        "content": {"elements": elements, "text": "原文"},
        "system": False,
        "recalled": False,
    }


def text(t):
    return {"type": "text", "data": {"text": t}}


def export(tmp_path):
    images = tmp_path / "export" / "resources" / "images"
    images.mkdir(parents=True)
    (images / "abc_ABC.png").write_bytes(b"png")
    path = tmp_path / "export" / "group.json"
    picture = {"type": "image", "data": {"subType": "photo", "md5": "ABC"}}
    messages = [
        exported(2435001, ASKER, MORNING, [text("夏令营要准备什么"), picture]),
        exported(2435002, ANSWERER, MORNING + 60, [text("成绩单和推荐信")]),
        exported(2435003, ANSWERER, EVENING, [text("晚上好")]),
    ]
    info = {"peerUid": str(GROUP), "selfUin": "20000"}
    path.write_text(json.dumps({"chatInfo": info, "messages": messages}), encoding="utf-8")
    return path


QUESTION = {
    "ref": "N1",
    "type": "question",
    "messages": [1],
    "title": "夏令营要准备什么材料",
    "body": "夏令营要准备什么？",
    "board": "qa",
    "tags": [],
    "same_as": None,
}
ANSWER = {"ref": "N2", "type": "reply", "question": "N1", "messages": [2], "body": "成绩单和推荐信"}
NOTHING = {"new": [], "edit": [], "react": []}


async def test_an_export_is_archived_as_it_came_and_a_second_run_does_it_no_more(tmp_path):
    forum = FakeForum()
    client = forum.client()
    path, data = export(tmp_path), tmp_path / "data"

    async def run(llm):
        index = Index(client, embed, "test", None)
        await archive_exports(client, llm, index, [path], data, Tuning(), workers=1)

    await run(Script({"new": [QUESTION, ANSWER], "edit": []}, NOTHING))
    [thread] = forum.threads.values()
    opening, reply = thread.posts
    assert (thread.created_at, reply.created_at) == (MORNING, MORNING + 60)
    assert opening.body.startswith("夏令营要准备什么？\n\n![](/uploads/")
    assert len((data / "done.txt").read_text().split()) == 2
    # All done: the LLM, lined up with nothing, is not asked again.
    await run(Script())
    assert len(forum.threads) == 1
