"""Threads on the forum that look like asking the same, for a moderator to look over and merge
(the forum's design §8.20): by the similarity of their titles and openings, as the plugin finds
candidates, grouped where a few go together. A high score is no proof, as with "今年还有九推吗" and
"明年还有九推吗"; nothing is merged here.

uv run python -m tools.duplicates --config <插件的配置> [--at-least 0.85]
"""

import argparse
import asyncio
import json
from datetime import datetime
from itertools import combinations
from pathlib import Path

import numpy as np

from mousehole.forum import Forum
from mousehole.llm import Embedder
from mousehole.similar import Index

AT_LEAST = 0.85


def groups(ids: np.ndarray, vectors: np.ndarray, at_least: float) -> list[tuple[float, list[int]]]:
    """Threads at least that alike, joined where they share one; the most alike group first,
    with the similarity of its most alike two."""
    similar = vectors @ vectors.T
    group_of: dict[int, frozenset[int]] = {}
    for i, j in np.argwhere(np.triu(similar, 1) >= at_least).tolist():
        group = group_of.get(i, frozenset([i])) | group_of.get(j, frozenset([j]))
        for k in group:
            group_of[k] = group
    return sorted(
        (
            (
                max(float(similar[a, b]) for a, b in combinations(g, 2)),
                sorted(int(ids[k]) for k in g),
            )
            for g in set(group_of.values())
        ),
        reverse=True,
    )


async def find(args: argparse.Namespace) -> None:
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    forum = Forum(config["forum_url"], config["token"])
    e = config["embedding"]
    index = Index(forum, Embedder(e["base_url"], e["api_key"], e["model"]).embed, e["model"], None)
    try:
        # Made afresh, so threads merged or deleted since are left out.
        await index.load()
        for score, group in groups(index.ids, index.vectors, args.at_least):
            threads = [await forum.call("GET", f"/threads/{t}") for t in group]
            # The earliest is kept by default: its opening post stays the first floor.
            threads.sort(key=lambda t: t["created_at"])
            print(f"\n相似度 {score:.3f}")
            for i, t in enumerate(threads):
                when = datetime.fromtimestamp(t["created_at"]).strftime("%m-%d %H:%M")
                print(
                    f"  {'保留' if i == 0 else '合并'} #{t['id']}  {when}  {t['board']['name']}  "
                    f"{t['reply_count']} 条回复  {t['title']}"
                )
    finally:
        await forum.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="List threads that look like duplicates")
    parser.add_argument("--config", required=True, help="插件的配置")
    parser.add_argument("--at-least", type=float, default=AT_LEAST, help="相似度至少多少")
    asyncio.run(find(parser.parse_args()))


if __name__ == "__main__":
    main()
