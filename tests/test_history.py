import asyncio
import json
from datetime import datetime
from itertools import pairwise

import pytest
from conftest import GROUP, Script, embed

from mousehole.archive import Message, Tuning
from mousehole.similar import Index
from tools import history
from tools.fake_forum import FakeForum
from tools.history import Batch, Run, archive_exports, runs_of, take_over

ASKER, ANSWERER = 10001, 10002
MORNING = datetime(2026, 10, 10, 10).timestamp()
EVENING = datetime(2026, 10, 10, 20).timestamp()


def exported(seq, uin, at, elements):
    return {
        "id": str(7690000000000000000 + seq),
        "seq": str(seq),
        "timestamp": int(at * 1000),
        "sender": {"uin": str(uin), "name": f"鼠{uin}", "nickname": "鼠", "groupCard": f"鼠{uin}"},
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
    system = {"id": "1", "seq": "1", "timestamp": int(MORNING * 1000) - 1, "sender": {}}
    messages = [
        system | {"content": {"elements": [], "text": ""}, "system": True, "recalled": False},
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


async def test_what_came_after_until_is_left_to_the_plugin(tmp_path):
    forum = FakeForum()
    client = forum.client()
    path, data = export(tmp_path), tmp_path / "data"
    index = Index(client, embed, "test", None)
    llm = Script({"new": [QUESTION, ANSWER], "edit": []})
    await archive_exports(client, llm, index, [path], data, Tuning(), workers=1, until=EVENING)
    assert len(forum.threads) == 1
    assert len((data / "done.txt").read_text().split()) == 1


async def test_what_began_before_since_is_left_for_a_run_without_it(tmp_path):
    forum = FakeForum()
    client = forum.client()
    path, data = export(tmp_path), tmp_path / "data"

    async def run(llm, **since):
        index = Index(client, embed, "test", None)
        await archive_exports(client, llm, index, [path], data, Tuning(), 1, **since)

    await run(Script(NOTHING), since=EVENING)
    assert forum.threads == {}
    await run(Script({"new": [QUESTION, ANSWER], "edit": []}))
    assert len(forum.threads) == 1
    assert len((data / "done.txt").read_text().split()) == 2


async def test_who_left_before_the_forum_knew_them_is_archived_under_their_name(tmp_path):
    forum = FakeForum()
    forum.strangers.add(ASKER)
    client = forum.client()
    index = Index(client, embed, "test", None)
    llm = Script({"new": [QUESTION, ANSWER], "edit": []}, NOTHING)
    await archive_exports(client, llm, index, [export(tmp_path)], tmp_path / "data", Tuning(), 1)
    assert forum.synced[GROUP] == [
        {"user_id": ASKER, "in_group": False, "nickname": "鼠", "card": f"鼠{ASKER}"}
    ]
    [thread] = forum.threads.values()
    # Their picture too, which the bot uploads for them.
    assert (thread.author, thread.posts[0].body.count("![](/uploads/")) == (ASKER, 1)


def batch(day, hour, weight):
    at = datetime(2026, 4, day, hour).timestamp()
    return Batch(GROUP, [Message(int(at), ASKER, at, [])], weight)


def test_runs_are_cut_at_four_by_what_there_is_to_organize():
    # Half is before the busy evening of the 2nd, which goes on into the night: no cut there.
    work = [batch(1, 9, 10), batch(1, 19, 10), batch(2, 9, 40), batch(2, 19, 40), batch(3, 9, 10)]
    first, second = runs_of(work, 2)
    assert (first.start, first.end) == (work[0].start, datetime(2026, 4, 2, 4).timestamp())
    assert (second.start, second.end) == (first.end, work[-1].start + 1)


def test_a_worker_done_takes_over_the_later_half_of_what_is_left_of_the_longest_run():
    work = [batch(day, 9, 600) for day in range(1, 9)]
    runs = [
        Run(work[0].start, work[2].start, at=work[1].start),
        Run(work[2].start, work[-1].start + 1),
    ]
    runs[1].at = work[2].start
    done = {work[0].name}
    later = take_over(runs, work, done)
    # Of the 5 batches left after the one at hand, the last 3 make about half.
    assert (later.start, later.end) == (datetime(2026, 4, 6, 4).timestamp(), work[-1].start + 1)
    assert runs[1].end == later.start
    # What is left of the first is too little to cut.
    runs[1].end = later.start
    assert take_over(runs[:1], work, done) is None


class Crash(BaseException):
    """The tool stopped, as by Ctrl-C."""


async def test_runs_taken_over_take_each_batch_once_and_go_on_where_they_were_stopped(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history, "TAKE_OVER_LEAST", 1)
    days = [datetime(2026, 4, day, 10).timestamp() for day in range(1, 9)]
    # The later days take longer to organize.
    said = ["今天组会讲什么"] * 4 + ["慢"] * 4
    messages = [
        exported(i, ASKER, at, [text(t)]) for i, (at, t) in enumerate(zip(days, said, strict=True))
    ]
    path = tmp_path / "group.json"
    info = {"peerUid": str(GROUP), "selfUin": "20000"}
    path.write_text(json.dumps({"chatInfo": info, "messages": messages}), encoding="utf-8")
    forum = FakeForum()
    client = forum.client()
    data = tmp_path / "data"

    def runs():
        return json.loads((data / "runs.json").read_text())

    async def stopping(system, prompt, schema):
        await asyncio.sleep(0.05 if "慢" in prompt else 0)
        # Stopped once a run was taken over, in the middle of a batch.
        if len(runs()) > 2 and "慢" in prompt:
            raise Crash
        return json.dumps(NOTHING), 1000, 100

    async def nothing(system, prompt, schema):
        return json.dumps(NOTHING), 1000, 100

    with pytest.raises(Crash):
        await archive_exports(
            client, stopping, Index(client, embed, "test", None), [path], data, Tuning(), 2
        )
    await archive_exports(
        client, nothing, Index(client, embed, "test", None), [path], data, Tuning(), 2
    )
    done = (data / "done.txt").read_text().split()
    assert (len(done), len(set(done))) == (8, 8)
    # Each batch, one segment here, organized once.
    lines = (data / "segments.jsonl").read_text().splitlines()
    organized = [json.loads(line)["times"][0] for line in lines]
    assert sorted(organized) == days
    bounds = sorted((r["start"], r["end"]) for r in runs())
    assert len(bounds) > 2 and all(a[1] == b[0] for a, b in pairwise(bounds))


async def test_a_last_batch_stopped_while_organized_is_organized_when_run_again(tmp_path):
    forum = FakeForum()
    client = forum.client()
    path, data = export(tmp_path), tmp_path / "data"

    async def run(llm):
        index = Index(client, embed, "test", None)
        await archive_exports(client, llm, index, [path], data, Tuning(), 1, until=EVENING)

    async def stopped(system, prompt, schema):
        raise Crash

    with pytest.raises(Crash):
        await run(stopped)
    # Its only batch is taken in, but not organized yet.
    assert len((data / "done.txt").read_text().split()) == 1
    await run(Script({"new": [QUESTION, ANSWER], "edit": []}))
    assert len(forum.threads) == 1


async def test_batches_in_none_of_the_runs_written_down_are_not_left_out_unsaid(tmp_path):
    forum = FakeForum()
    client = forum.client()
    path, data = export(tmp_path), tmp_path / "data"
    data.mkdir()
    (data / "runs.json").write_text(json.dumps([{"start": 0, "end": MORNING}]))
    with pytest.raises(SystemExit):
        await archive_exports(
            client, Script(), Index(client, embed, "test", None), [path], data, Tuning(), 1
        )
