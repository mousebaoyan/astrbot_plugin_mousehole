import json

import httpx
import pytest

from mousehole import judge
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

    async def __call__(self, system: str, prompt: str) -> tuple[str, int, int]:
        self.prompts.append((system, prompt))
        return self.answers.pop(0), 1000, 100


class FakeOneJev(judge.OneJev):
    """Answers with `scores`, which tests may change; `down` makes it unreachable for that
    many calls."""

    def __init__(self, question: float = 0.9, answer: float = 0.0, down: int = 0):
        self.scores = {"question": question, "answer": answer}
        self.calls = 0
        self.down = down

        def handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.down:
                self.down -= 1
                raise httpx.ConnectError("refused")
            asked = json.loads(request.content)["questions"]
            answers = {k: {"type": "noul", "noul": self.scores[k]} for k in asked}
            return httpx.Response(200, json={"answers": answers})

        super().__init__("http://onejev.test", httpx.MockTransport(handle))


async def embed(texts: list[str]) -> list[list[float]]:
    # Every text alike: which candidate is the same question is the LLM's call in these tests.
    return [[1.0, 0.0] for _ in texts]


def message(id: int, user: int, text: str = "", *, reply: int | None = None, segments=None):
    parts = [{"type": "reply", "data": {"id": str(reply)}}] if reply else []
    parts += [{"type": "text", "data": {"text": text}}] if text else []
    return Message(id, user, 1_791_500_000 + id, parts + (segments or []))


class Setup:
    def __init__(self, llm: Script, onejev: FakeOneJev | None = None):
        self.forum = FakeForum(tags=["夏令营", "预推免"])
        client = self.forum.client()
        self.llm = llm
        self.onejev = onejev or FakeOneJev()
        self.slept: list[float] = []

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        self.index = Index(client, embed, "test", None)
        self.archive = Archive(
            client, self.onejev, llm, self.index, prefixes=["/"], sleep=sleep, clock=lambda: 0
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
