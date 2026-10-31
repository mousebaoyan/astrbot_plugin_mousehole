import json
from datetime import datetime

from conftest import GROUP, Script, embed

from mousehole.archive import Tuning
from mousehole.similar import Index
from tools.fake_forum import FakeForum
from tools.handoff import import_journal, quoted, take_snapshot
from tools.history import archive_exports, read_exports
from tools.replay import HISTORY_IDS

ASKER, ANSWERER, OLD_ASKER = 1000001, 1000002, 1000003
YESTERDAY = datetime(2026, 7, 9, 10).timestamp()
MORNING = datetime(2026, 7, 10, 10).timestamp()


def exported(seq, uin, at, elements):
    return {
        "id": f"m{seq}",
        "seq": str(seq),
        "timestamp": int(at * 1000),
        "sender": {"uin": str(uin), "name": f"鼠{uin}", "nickname": "鼠", "groupCard": f"鼠{uin}"},
        "content": {"elements": elements, "text": f"@鼠{ANSWERER} 原文"},
        "system": False,
        "recalled": False,
    }


def text(t):
    return {"type": "text", "data": {"text": t}}


EXPORT = {
    "chatInfo": {"peerUid": str(GROUP), "selfUin": "20000"},
    "messages": [
        # Archived on the forum the day before.
        exported(1, OLD_ASKER, YESTERDAY, [text("九推还有吗")]),
        exported(2, ASKER, MORNING, [text("夏令营要准备什么")]),
        exported(3, ANSWERER, MORNING + 60, [text("成绩单和推荐信")]),
        exported(
            4,
            ANSWERER,
            MORNING + 120,
            [
                {"type": "reply", "data": {"referencedMessageId": "m1", "senderUin": "1000003"}},
                {"type": "at", "data": {"uin": str(OLD_ASKER), "name": f"鼠{OLD_ASKER}"}},
                text("九推一般九月底"),
            ],
        ),
    ],
}
ORGANIZED = {
    "new": [
        {
            "ref": "N1",
            "type": "question",
            "messages": [1],
            "title": "夏令营要准备什么材料",
            "body": "夏令营要准备什么？",
            "board": "qa",
            "tags": [],
            "same_as": None,
        },
        {"ref": "N2", "type": "reply", "question": "N1", "messages": [2], "body": "成绩单和推荐信"},
        {"ref": "N3", "type": "reply", "question": "Q1", "messages": [3], "body": "九推一般九月底"},
    ],
    "edit": [],
}


async def handed_off(tmp_path):
    """The forum with yesterday's question on it, and the morning after archived offline onto a
    snapshot of it: the forum and the offline data."""
    forum = FakeForum()
    client = forum.client()
    yesterday = {
        "board": "qa",
        "kind": "question",
        "title": "今年还有九推吗",
        "tags": [],
        "content": {"markdown": "九推还有吗"},
        "source": {"chat_type": "group", "chat_id": GROUP, "message_ids": [HISTORY_IDS + 1]},
        "created_at": int(YESTERDAY),
    }
    await client.call("POST", "/threads", body=yesterday, act_as=OLD_ASKER)
    export = tmp_path / "group.json"
    export.write_text(json.dumps(EXPORT), encoding="utf-8")
    data = tmp_path / "handoff"
    (data / "forum").mkdir(parents=True)
    yesterdays = read_exports([export]).work[0].name
    (data / "done.txt").write_text(yesterdays + "\n")

    snapshot = await take_snapshot(client, quoted([export], data))
    (data / "forum" / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    offline = FakeForum.of_snapshot(snapshot).journaled(data / "forum" / "journal.jsonl")
    index = Index(offline, embed, "test", None)
    llm = Script(ORGANIZED, {"same": None})
    await archive_exports(offline, llm, index, [export], data, Tuning(), 1, offline=True)
    return forum, data


async def test_what_was_archived_offline_is_made_on_the_forum_once(tmp_path):
    forum, data = await handed_off(tmp_path)
    # The run follows yesterday's thread, which the morning quotes, as on the forum.
    journal = (data / "forum" / "journal.jsonl").read_text().splitlines()
    assert len(journal) == 3
    assert await import_journal(forum.client(), data) == {}
    old, new = sorted(forum.threads.values(), key=lambda t: t.created_at)
    assert (new.title, new.author, new.created_at) == ("夏令营要准备什么材料", ASKER, MORNING)
    assert [(p.author, p.created_at) for p in new.posts[1:]] == [(ANSWERER, MORNING + 60)]
    assert [(p.author, p.body) for p in old.posts[1:]] == [(ANSWERER, "九推一般九月底")]
    # Imported again, nothing is made twice.
    await import_journal(forum.client(), data)
    (data / "forum" / "imported.jsonl").unlink()
    await import_journal(forum.client(), data)
    assert [len(t.posts) for t in (old, new)] == [2, 2] and len(forum.threads) == 2


async def test_writes_the_forum_turns_away_are_skipped_with_those_that_go_with_them(tmp_path):
    forum, data = await handed_off(tmp_path)
    forum.proxy_disabled.add(ASKER)
    skipped = await import_journal(forum.client(), data)
    assert skipped == {"新帖: proxy_disabled": 1, "回复: goes with one skipped": 1}
    [old] = forum.threads.values()
    assert [p.body for p in old.posts[1:]] == ["九推一般九月底"]


async def test_an_offline_run_goes_on_from_its_journal_with_the_same_ids(tmp_path):
    forum, data = await handed_off(tmp_path)
    snapshot = json.loads((data / "forum" / "snapshot.json").read_text())
    again = FakeForum.of_snapshot(snapshot)
    again.journaled(data / "forum" / "journal.jsonl")
    titles = {t.id: (t.title, [p.id for p in t.posts]) for t in again.threads.values()}
    first = FakeForum.of_snapshot(snapshot)
    first.journaled(data / "forum" / "journal.jsonl")
    assert titles == {t.id: (t.title, [p.id for p in t.posts]) for t in first.threads.values()}
    assert len(titles) == 2
