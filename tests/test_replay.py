from tools.replay import convert


def exported(id, seq, uin, elements, **flags):
    return {
        "id": id,
        "seq": str(seq),
        "timestamp": 1_791_500_000_000 + seq * 1000,
        "sender": {"uin": str(uin), "name": f"鼠{uin}"},
        "content": {"elements": elements, "text": "原文"},
        "system": flags.get("system", False),
        "recalled": flags.get("recalled", False),
    }


def test_exported_messages_become_onebot_segments_by_their_ids():
    export = {
        "chatInfo": {"peerUid": "100200300", "selfUin": "20000"},
        "messages": [
            exported("7690000000000000001", 11, 1, [{"type": "text", "data": {"text": "问"}}]),
            exported("7690000000000000002", 12, 2, [], system=True),
            exported("7690000000000000003", 13, 3, [], recalled=True),
            exported(
                "7690000000000000004",
                14,
                2,
                [
                    {"type": "reply", "data": {"referencedMessageId": "7690000000000000001"}},
                    {"type": "reply", "data": {"referencedMessageId": "gone"}},
                    {"type": "at", "data": {"uin": "1", "name": "鼠1"}},
                    {"type": "image", "data": {"subType": "sticker", "md5": "a"}},
                    {"type": "image", "data": {"subType": "photo", "md5": "b"}},
                    {"type": "market_face", "data": {}},
                ],
            ),
        ],
    }
    group, bot, messages = convert(export)
    ids = [7690000000000000001, 7690000000000000004]
    assert (group, bot, [m.id for m, _, _ in messages]) == (100200300, 20000, ids)
    m, name, text = messages[1]
    assert (m.user, m.time, m.quoted, name, text) == (2, 1_791_500_014, ids[0], "鼠2", "原文")
    assert [s["type"] for s in m.segments] == ["reply", "at", "image", "image", "mface"]
    assert [s["data"] for s in m.segments[2:4]] == [
        {"sub_type": 1, "md5": "a"},
        {"sub_type": 0, "md5": "b"},
    ]
