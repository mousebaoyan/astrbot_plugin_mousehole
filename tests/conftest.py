import json

import pytest

from mousehole.archive import Archive, Message
from mousehole.similar import Index
from tools.fake_forum import FakeForum

GROUP = 100200300
BOT = 20000


class Script:
    """A fake LLM answering with what each test lines up, and keeping what it was asked."""

    def __init__(self, *answers: object):
        self.answers = [
            a if isinstance(a, str) else json.dumps(a, ensure_ascii=False) for a in answers
        ]
        self.prompts: list[tuple[str, str]] = []

    async def __call__(self, system: str, prompt: str, schema: dict) -> tuple[str, int, int]:
        self.prompts.append((system, prompt))
        return self.answers.pop(0), 1000, 100


async def embed(texts: list[str]) -> list[list[float]]:
    # Every text alike: which candidate is the same question is the LLM's call in these tests.
    return [[1.0, 0.0] for _ in texts]


def message(id: int, user: int, text: str = "", *, reply: int | None = None, segments=None):
    parts = [{"type": "reply", "data": {"id": str(reply)}}] if reply else []
    parts += [{"type": "text", "data": {"text": text}}] if text else []
    return Message(id, user, 1_791_500_000 + id, parts + (segments or []))


class Setup:
    def __init__(self, llm: Script):
        self.forum = FakeForum(tags=["夏令营", "预推免"])
        client = self.forum.client()
        self.llm = llm
        self.now = 0.0
        # (group, segments) of each receipt sent.
        self.sent: list[tuple[int, list[dict]]] = []

        async def notify(group: int, segments: list[dict]) -> None:
            self.sent.append((group, segments))

        self.index = Index(client, embed, "test", None)
        self.archive = Archive(
            client,
            llm,
            self.index,
            prefixes=["/"],
            site="https://forum.test",
            notify=notify,
            clock=lambda: self.now,
        )

    async def round(self, *messages: Message) -> dict:
        """The messages taken in and worked through, as one round unless there are more than a
        round holds. The last round's report."""
        if not self.archive.boards:
            await self.archive.refresh_taxonomy()
        for m in messages:
            self.archive.add(GROUP, m, BOT)
        if self.archive.chats[GROUP].pending:
            self.archive.close(self.archive.chats[GROUP])
        while not self.archive.queue.empty():
            report = await self.archive.handle(self.archive.queue.get_nowait())
        return report


@pytest.fixture
def setup():
    return Setup
