"""Makes the boards and tags replay settled on (design §8.9, docs/replay.md §6) on the forum. The
token needs `admin` for boards and `moderate` for tags; those there already are left as they are.

uv run python -m tools.boards data/boards.json --config <AstrBot 的插件配置文件>
"""

import argparse
import asyncio
import json
from pathlib import Path

from mousehole.forum import Forum, ForumError


async def create(forum: Forum, path: str, body: dict, exists: str, what: str) -> None:
    try:
        await forum.call("POST", path, body=body)
        print(f"建好了{what}")
    except ForumError as e:
        if e.code != exists:
            raise
        print(f"{what}已经有了")


async def make(args: argparse.Namespace) -> None:
    taxonomy = json.loads(Path(args.boards).read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    forum = Forum(config["forum_url"], config["token"])
    try:
        for b in taxonomy["boards"]:
            body = {"slug": b["slug"], "name": b["name"], "description": b.get("description", "")}
            await create(forum, "/boards", body, "board_exists", f"版块 {b['name']}")
        for t in taxonomy["tags"]:
            body = {"name": t["name"], "kind": t["kind"]}
            await create(forum, "/tags", body, "tag_exists", f"标签 {t['name']}")
    finally:
        await forum.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Make the boards and tags on the forum")
    parser.add_argument("boards", help="版块和标签的 JSON，格式同回放的 --boards")
    parser.add_argument("--config", required=True, help="插件的配置文件，用里面的论坛地址和 token")
    asyncio.run(make(parser.parse_args()))


if __name__ == "__main__":
    main()
