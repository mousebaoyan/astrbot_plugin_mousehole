"""Threads like a new question (design §8.5): vectors of every thread's title and opening, kept
here, together with the forum's own literal search."""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import numpy as np

from .forum import Forum

log = logging.getLogger("astrbot.mousehole")

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]

# The start of the opening post that goes with the title, as long as the forum's excerpt.
OPENING_CHARS = 100
SEMANTIC = 5
LITERAL = 5
# Cosine similarity below which a thread is no candidate. Tuned by replay (docs/replay.md §5.3).
MIN_SIMILARITY = 0.5
EMBED_BATCH = 32


def text_of(title: str, opening: str) -> str:
    return f"{title}\n{opening[:OPENING_CHARS]}"


class Index:
    def __init__(
        self,
        forum: Forum,
        embed: Embed,
        model: str,
        path: Path | None,
        min_similarity: float = MIN_SIMILARITY,
    ):
        self.forum = forum
        self.min_similarity = min_similarity
        self.embed = embed
        self.model = model
        # None keeps it in memory only, as replay does.
        self.path = path
        self.ids = np.empty(0, dtype=np.int64)
        self.vectors = np.empty((0, 0), dtype=np.float32)

    async def load(self) -> None:
        """From the file, or from the forum when there is none or it was made by another model."""
        if self.path is not None and self.path.exists():
            with np.load(self.path) as saved:
                if str(saved["model"]) == self.model:
                    self.ids, self.vectors = saved["ids"], saved["vectors"]
                    return
        await self.rebuild()

    async def rebuild(self) -> None:
        texts: dict[int, str] = {}
        cursor = None
        while True:
            params = {"view": "created"} | ({"cursor": cursor} if cursor else {})
            page = await self.forum.call("GET", "/threads", params=params)
            for t in page["items"]:
                detail = await self.forum.call("GET", f"/threads/{t['id']}")
                texts[t["id"]] = text_of(detail["title"], detail["excerpt"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        vectors = []
        values = list(texts.values())
        for i in range(0, len(values), EMBED_BATCH):
            vectors += await self.embed(values[i : i + EMBED_BATCH])
        self.ids = np.array(list(texts), dtype=np.int64)
        self.vectors = normalized(vectors) if vectors else np.empty((0, 0), dtype=np.float32)
        self.save()
        log.info("Rebuilt the thread index: %d threads", len(self.ids))

    def save(self) -> None:
        if self.path is None:
            return
        part = self.path.with_suffix(".part")
        with part.open("wb") as f:
            np.savez(f, ids=self.ids, vectors=self.vectors, model=np.array(self.model))
        part.replace(self.path)

    async def put(self, thread: int, title: str, opening: str) -> None:
        vector = normalized(await self.embed([text_of(title, opening)]))
        keep = self.ids != thread
        self.vectors = np.vstack([self.vectors[keep], vector]) if len(self.ids) else vector
        self.ids = np.append(self.ids[keep], thread)
        self.save()

    def remove(self, thread: int) -> None:
        keep = self.ids != thread
        self.ids, self.vectors = self.ids[keep], self.vectors[keep]
        self.save()

    async def candidates(self, title: str, body: str) -> list[int]:
        """Threads that may ask the same: the semantically nearest first, then literal matches."""
        found: list[int] = []
        if len(self.ids):
            query = normalized(await self.embed([text_of(title, body)]))[0]
            similarity = self.vectors @ query
            for i in np.argsort(-similarity)[:SEMANTIC]:
                if similarity[i] >= self.min_similarity:
                    found.append(int(self.ids[i]))
        text = f"{title}\n{body}"[:2000]
        literal = await self.forum.call("GET", "/search/similar", params={"text": text})
        for item in literal["items"][:LITERAL]:
            if item["thread"]["id"] not in found:
                found.append(item["thread"]["id"])
        return found

    async def thread_created(self, thread: int) -> None:
        detail = await self.forum.call("GET", f"/threads/{thread}")
        await self.put(thread, detail["title"], detail["excerpt"])


def normalized(vectors: list[list[float]]) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    return array / np.linalg.norm(array, axis=1, keepdims=True)
