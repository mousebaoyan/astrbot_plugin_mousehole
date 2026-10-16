from mousehole.qq import at, fill, text
from mousehole.welcome import GAP, MENTIONS, WAIT, Welcome, welcome_message

GROUP = 100200300
BOT = 20000


def test_the_template_ats_each_newcomer_and_names_the_forum():
    segments = welcome_message("欢迎 {at} 来到鼠群\n鼠洞：{forum}", [1, 2], "https://bbs.test")
    assert segments == [
        {"type": "text", "data": {"text": "欢迎 "}},
        {"type": "at", "data": {"qq": "1"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "at", "data": {"qq": "2"}},
        {"type": "text", "data": {"text": " 来到鼠群\n鼠洞：https://bbs.test"}},
    ]


def test_names_a_template_is_not_given_are_left_as_written():
    assert fill("{at}：{titel}", at=[at(1)]) == [at(1), text("：{titel}")]


async def test_newcomers_share_welcomes_hours_apart_and_members_of_another_group_get_none():
    sent = []

    class FakeQQ:
        async def send_group(self, group, message):
            sent.append((group, [s["data"]["qq"] for s in message if s["type"] == "at"]))

    clock = {"t": 0.0}
    sync = type("Sync", (), {"groups": [GROUP, GROUP + 1], "roster": {GROUP + 1: {3}}})()
    w = Welcome(FakeQQ(), sync, groups=[], template="欢迎 {at}", site="s", clock=lambda: clock["t"])
    w.joined(GROUP, 1, BOT)
    clock["t"] = WAIT - 1
    w.joined(GROUP, 2, BOT)
    w.joined(GROUP, 3, BOT)  # in the other group already
    w.joined(GROUP, BOT, BOT)
    w.joined(GROUP + 2, 4, BOT)  # not a synced group
    await w.send_due()
    assert sent == []
    clock["t"] = WAIT
    await w.send_due()
    assert sent == [(GROUP, ["1", "2"])]
    await w.send_due()
    assert len(sent) == 1

    for user in range(100, 100 + MENTIONS + 5):
        w.joined(GROUP, user, BOT)
    clock["t"] += WAIT
    await w.send_due()
    assert len(sent) == 1  # the last welcome was less than GAP ago
    clock["t"] = WAIT + GAP
    await w.send_due()
    assert len(sent[-1][1]) == MENTIONS
