import json
from datetime import datetime

from conftest import GROUP, Script, embed

from mousehole.archive import Tuning
from mousehole.similar import Index
from tools.fake_forum import FakeForum
from tools.history import archive_exports
from tools.legacy import LEGACY_SEQ, Content, convert_legacy, read_txt, sure_of
from tools.replay import convert

ASKER, ANSWERER, OTHER = 10001, 10002, 10003
BOT = 20000
DAY = datetime(2026, 4, 1).timestamp()
TXT = """﻿QQ群：四非计算机保研群（742203503）
消息数：6
========================================================================

[2026-04-01 10:00:00] 23-某校-问问: 夏令营要准备什么
材料清单有吗
[2026-04-01 10:00:00] 23-某校-问问: 夏令营要准备什么
材料清单有吗
[2026-04-01 10:01:00] 22-学长: [回复：夏令营要准备什么材料清单有吗]@23-某校-问问成绩单和推荐信[图片：ab.jpg]
[2026-04-01 10:02:00] 路过的: [图片：cd.jpg][动画表情]
[2026-04-01 10:03:00] 路过的: [回复：[表情：[捂脸]]]@22-学长谢谢[表情：赞]
[2026-04-01 10:04:00] 22-学长: [系统：路过的撤回了一条消息]
"""


def read_txt_from(tmp_path):
    path = tmp_path / "group.txt"
    path.write_text(TXT, encoding="utf-8")
    return read_txt(path)


def test_the_txt_has_each_message_once_with_the_lines_after_the_first(tmp_path):
    messages = read_txt_from(tmp_path)
    assert messages[0]["text"] == "夏令营要准备什么\n材料清单有吗"
    assert len(messages) == 5


def test_an_html_message_is_read_into_segments():
    content = Content()
    content.feed(
        '<div class="content"><div class="reply-content" data-reply-to="msg-42">'
        '<strong>某人</strong><div class="reply-content-text">原话</div></div>'
        '<span class="at-mention">@22-学长</span><span class="text-content">成绩单<br>'
        '<a href="x">链接</a></span><span class="native-face-wrap"><img alt="/捂脸"></span>'
        '<div class="image-content"><img src="a.jpg"></div></div>'
    )
    assert content.reply == "42"
    assert content.segments == [
        {"type": "at", "name": "@22-学长"},
        {"type": "text", "data": {"text": "成绩单\n链接"}},
        {"type": "face", "data": {}},
        {"type": "text", "data": {"text": "[图片已丢失]"}},
    ]


def test_a_name_is_sure_when_the_html_shows_it_as_one_persons_and_they_had_joined():
    txt = [{"time": DAY, "name": n, "text": "x"} for n in ("甲", "乙", "丙", "丁", "戊")]
    shown = {"甲": {"1"}, "乙": {"2"}, "丙": {"3"}, "丁": {"4"}}
    # 乙 is someone else's name too in a later export; 丙 joined after saying it; 丁 is
    # unknown to the forum, as one who left before it; 戊 isn't shown at all.
    claims = shown | {"乙": {"2", "9"}}
    joined = {1: DAY - 100, 3: DAY + 7 * 86400}
    assert sure_of(txt, shown, claims, joined) == {"甲": 1, "丁": 4}


def legacy(tmp_path):
    txt = read_txt_from(tmp_path)
    shown = {"23-某校-问问": {str(ASKER)}, "22-学长": {str(ANSWERER)}}
    html = [
        {
            "id": "7001",
            "time": DAY + 86400,
            "system": False,
            "qq": str(ANSWERER),
            "name": "22-学长",
            "segments": [
                {"type": "at", "name": "@23-某校-问问"},
                {"type": "text", "data": {"text": "还有简历"}},
            ],
            "reply": "7000",
        }
    ]
    later = [{"messages": []}]
    return convert_legacy(txt, html, shown, later, {}, GROUP, BOT)


def test_the_old_exports_become_one_for_the_history_tool(tmp_path):
    export, tally = legacy(tmp_path)
    group, bot, messages = convert(export)
    assert (group, bot) == (GROUP, BOT)
    said = [(m.user, m.segments) for m, _, _ in messages]
    asked, answered, pictures, thanked, later = said
    assert asked[0] == ASKER and answered[0] == ANSWERER
    # The quote found, the @ to who it names, the lost picture said to be lost.
    reply, at, *rest = answered[1]
    assert (reply["type"], at) == ("reply", {"type": "at", "data": {"qq": str(ASKER)}})
    assert [s["data"]["text"] for s in rest] == ["成绩单和推荐信", "[图片已丢失]"]
    # Someone not known for sure is below zero; nothing but a lost picture says nothing.
    assert pictures[0] < 0 and pictures[1] == [
        {"type": "image", "data": {"sub_type": 1, "md5": None}}
    ]
    assert thanked[0] == pictures[0]
    assert thanked[1][0] == {"type": "text", "data": {"text": "（回复：[表情：[捂脸]]）"}}
    assert later[1][0] == {"type": "at", "data": {"qq": str(ASKER)}}
    assert export["messages"][0]["seq"] == str(LEGACY_SEQ)
    assert (tally["replies found"], tally["replies not found"], tally["system"]) == (1, 1, 1)


async def test_what_someone_not_known_said_is_shown_but_not_archived(tmp_path):
    export, _ = legacy(tmp_path)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    forum = FakeForum()
    client = forum.client()
    stranger = {
        "ref": "N1",
        "type": "question",
        "messages": [4],
        "title": "谢谢学长的回答呀",
        "body": "谢谢",
        "board": "qa",
        "tags": [],
        "same_as": None,
    }
    nothing = {"new": [], "edit": [], "react": []}
    llm = Script({"new": [stranger], "edit": []}, nothing)
    await archive_exports(
        client, llm, Index(client, embed, "test", None), [path], tmp_path / "d", Tuning(), 1
    )
    assert forum.threads == {}
    assert "〔不收〕" in llm.prompts[0][1]
    assert not forum.synced
