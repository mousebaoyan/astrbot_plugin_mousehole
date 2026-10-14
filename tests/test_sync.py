import gzip
import json

import httpx
import pytest

from mousehole.forum import Forum
from mousehole.sync import Sync, delta

GROUP = 100200300


class FakeQQ:
    def __init__(self, members):
        self.members = members

    async def call(self, action, **params):
        assert params["group_id"] == GROUP
        return {"get_group_member_list": self.members, "get_group_info": {"group_name": "鼠群"}}[
            action
        ]


def recording():
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = request.content
        if request.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        sent.append((request.method, request.url.path, json.loads(body)))
        return httpx.Response(200, json={"held": False, "stale": False})

    return Forum("http://forum.test", "t", httpx.MockTransport(handle)), sent


@pytest.mark.parametrize(
    ("notice", "record"),
    [
        (
            {"notice_type": "group_decrease", "sub_type": "kick", "user_id": 1},
            {"user_id": 1, "in_group": False, "left_reason": "kick"},
        ),
        (
            {
                "notice_type": "group_decrease",
                "sub_type": "kick_me",
                "user_id": 1,
                "group_id": GROUP,
            },
            None,
        ),
        (
            {"notice_type": "group_admin", "sub_type": "unset", "user_id": 1},
            {"user_id": 1, "role": "member"},
        ),
        (
            {"notice_type": "group_card", "user_id": 1, "card_new": "26-某校"},
            {"user_id": 1, "card": "26-某校"},
        ),
        (
            {"notice_type": "notify", "sub_type": "title", "user_id": 1, "title": "26届"},
            {"user_id": 1, "title": "26届"},
        ),
        ({"notice_type": "notify", "sub_type": "poke", "user_id": 1}, None),
    ],
)
def test_notices_become_member_records(notice, record):
    assert delta(notice) == record


async def test_a_full_sync_sends_the_cached_list_with_when_members_were_last_seen_speaking():
    forum, sent = recording()
    members = [{"user_id": 1, "last_sent_time": 100}, {"user_id": 2, "last_sent_time": 500}]
    sync = Sync(forum, FakeQQ(members))
    sync.saw(GROUP, 1, 300)
    sync.saw(GROUP, 2, 400)
    await sync.full(GROUP)
    (_, path, body), (_, info_path, info) = sent
    assert path == f"/api/v1/bot/groups/{GROUP}/members"
    assert [m["last_sent_time"] for m in body["members"]] == [300, 500]
    assert info["group_name"] == "鼠群" and info["observed_at"] == body["observed_at"]


async def test_a_newcomer_comes_with_their_cached_record():
    forum, sent = recording()
    sync = Sync(forum, FakeQQ([{"user_id": 3, "card": "27-新人"}]))
    sync.groups = [GROUP]
    notice = {"notice_type": "group_increase", "group_id": GROUP, "user_id": 3, "time": 42}
    await sync.notice(notice)
    await sync.notice(notice | {"group_id": 1})
    assert sent == [
        (
            "PATCH",
            f"/api/v1/bot/groups/{GROUP}/members",
            {"observed_at": 42, "members": [{"user_id": 3, "in_group": True, "card": "27-新人"}]},
        )
    ]
