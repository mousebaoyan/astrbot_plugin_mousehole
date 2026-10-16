import json
from pathlib import Path

import pytest

from mousehole import archive, forum
from mousehole.archive import Archive, Message
from mousehole.similar import Index
from tools.fake_forum import FakeForum

GROUP = 100200300
BOT = 20000
SCHEMA = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))


def defaults(section: str) -> dict:
    """A settings section's defaults: the wording the plugin ships with."""
    return {key: item["default"] for key, item in SCHEMA[section]["items"].items()}


class Script:
    """A fake LLM answering with what each test lines up, and keeping what it was asked."""

    def __init__(self, *answers: object):
        self.answers = [
            a if isinstance(a, str | Exception) else json.dumps(a, ensure_ascii=False)
            for a in answers
        ]
        self.prompts: list[tuple[str, str]] = []

    async def __call__(self, system: str, prompt: str, schema: dict) -> tuple[str, int, int]:
        """An exception lined up is raised."""
        self.prompts.append((system, prompt))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, 1000, 100


async def embed(texts: list[str]) -> list[list[float]]:
    # Every text alike: which candidate is the same question is the LLM's call in these tests.
    return [[1.0, 0.0] for _ in texts]


def message(id: int, user: int, text: str = "", *, reply: int | None = None, segments=None):
    parts = [{"type": "reply", "data": {"id": str(reply)}}] if reply else []
    parts += [{"type": "text", "data": {"text": text}}] if text else []
    return Message(id, user, 1_791_500_000 + id, parts + (segments or []))


class Setup:
    def __init__(self, llm: Script, data: Path, **options):
        self.forum = FakeForum(tags=["夏令营", "预推免"])
        client = self.forum.client()
        self.llm = llm
        self.now = 0.0
        # (group, segments) of each summary sent.
        self.sent: list[tuple[int, list[dict]]] = []

        async def notify(group: int, segments: list[dict]) -> None:
            self.sent.append((group, segments))

        self.index = Index(client, embed, "test", None)
        self.archive = Archive(
            client,
            llm,
            self.index,
            data=data,
            prefixes=["/"],
            site="https://forum.test",
            templates=defaults("archive"),
            notify=notify,
            clock=lambda: self.now,
            **options,
        )

    async def batch(self, *messages: Message) -> dict:
        """The messages taken in and archived as one batch; the last segment's report."""
        if not self.archive.boards:
            await self.archive.refresh_taxonomy()
        for m in messages:
            await self.archive.add(GROUP, m, BOT)
        self.archive.start_round(everything=True)
        self.now += 1
        return [report async for report in self.archive.batch()][-1]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Asking the forum again is tested on its own; history is read back without pauses."""
    monkeypatch.setattr(forum, "RETRY_WAITS", ())
    monkeypatch.setattr(archive, "HISTORY_GAP", 0)


@pytest.fixture
def setup(tmp_path):
    return lambda llm, **options: Setup(llm, tmp_path, **options)
