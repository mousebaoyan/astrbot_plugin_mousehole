import numpy as np

from mousehole.similar import Index
from tools.fake_forum import FakeForum


def vectors(table):
    async def embed(texts):
        return [table[t.split("\n")[0]] for t in texts]

    return embed


async def post(forum, title, message):
    return await forum.call(
        "POST",
        "/threads",
        body={
            "board": "qa",
            "kind": "question",
            "title": title,
            "tags": [],
            "content": {"onebot": [{"type": "text", "data": {"text": "正文"}}]},
            "source": {"chat_type": "group", "chat_id": 1, "message_ids": [message]},
        },
        act_as=1,
    )


async def test_the_index_is_built_from_the_forum_kept_in_a_file_and_rebuilt_for_another_model(
    tmp_path,
):
    fake = FakeForum()
    forum = fake.client()
    first = await post(forum, "夏令营材料", 1)
    second = await post(forum, "预推免撤回", 2)
    table = {"夏令营材料": [1.0, 0.0], "预推免撤回": [0.0, 1.0], "夏令营要什么材料": [0.9, 0.1]}
    path = tmp_path / "threads.npz"
    index = Index(forum, vectors(table), "bge-m3", path)
    await index.load()
    assert sorted(index.ids.tolist()) == [first["id"], second["id"]]

    index.remove(second["id"])
    again = Index(forum, vectors(table), "bge-m3", path)
    await again.load()
    assert again.ids.tolist() == [first["id"]]
    other = Index(forum, vectors(table), "another", path)
    await other.load()
    assert len(other.ids) == 2

    # Near enough by meaning, or sharing words.
    found = await other.candidates("夏令营要什么材料", "")
    assert found[0] == first["id"]
    assert np.allclose(np.linalg.norm(other.vectors, axis=1), 1)
