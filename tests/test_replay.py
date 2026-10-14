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


def test_exported_messages_become_onebot_segments_quoting_by_seq():
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
                    {"type": "image", "data": {"subType": "sticker", "url": "a"}},
                    {"type": "image", "data": {"subType": "photo", "url": "b"}},
                    {"type": "market_face", "data": {}},
                ],
            ),
        ],
    }
    group, bot, messages = convert(export)
    assert (group, bot, [m.id for m, _, _ in messages]) == (100200300, 20000, [11, 14])
    m, name, text = messages[1]
    assert (m.user, m.time, m.quoted, name, text) == (2, 1_791_500_014, 11, "鼠2", "原文")
    assert [s["type"] for s in m.segments] == ["reply", "at", "image", "image", "mface"]
    assert [s["data"].get("sub_type") for s in m.segments[2:4]] == [1, 0]
