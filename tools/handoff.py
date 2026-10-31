"""Hands the history archiving to someone with an LLM of their own, and takes back what they made
(docs/history.md). `pack` writes all they need: the exports, how far the archiving got, and a
snapshot of the forum to archive onto offline. `import` makes on the forum what they wrote to the
snapshot.

uv run python -m tools.handoff pack EXPORT... --config data/history-prod.json --data history \
    --out handoff
uv run python -m tools.handoff import EXPORT... --config data/history-prod.json --data handoff
"""

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
from collections import Counter
from functools import partial
from pathlib import Path

from mousehole.archive import load
from mousehole.files import append_line
from mousehole.forum import Forum, ForumError
from tools.fake_forum import OFFLINE_IDS
from tools.history import Patient, enroll, kind_of, organized, patiently, read_exports

# Floors the forum lists at a time.
PAGE = 20


# ---- Packing


async def pack(args: argparse.Namespace) -> None:
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    data, out = Path(args.data), Path(args.out)
    if (data / "forum").exists():
        raise SystemExit(f"{data} is archived offline already: hand it on as it is")
    exports = [Path(x) for x in args.exports]
    (out / "export").mkdir(parents=True)
    for path in exports:
        shutil.copy(path, out / "export" / path.name)
    for name in ("runs.json", "done.txt"):
        shutil.copy(data / name, out / name)
    for run in sorted(data.glob("run-*")):
        shutil.copytree(run / "batch", out / run.name / "batch")
        (out / run.name / "pending").mkdir()
        shutil.copy(run / "archive.json", out / run.name / "archive.json")

    forum = Forum(config["forum_url"], config["token"], Patient())
    wanted = quoted(exports, data)
    snapshot = await patiently(lambda: take_snapshot(forum, wanted))
    (out / "forum").mkdir()
    text = json.dumps(snapshot, ensure_ascii=False)
    (out / "forum" / "snapshot.json").write_text(text, encoding="utf-8")
    await forum.close()
    print(
        f"{out}: {len(snapshot['threads'])} threads, "
        f"{len(snapshot['links'])} messages quoted from the forum",
        file=sys.stderr,
    )


def quoted(exports: list[Path], data: Path) -> set[tuple[int, int]]:
    """Messages that what is left to archive quotes, but which are not in it: archived on the
    forum, maybe, where the snapshot must find them to follow their threads."""
    work = read_exports(exports).work
    taken = organized(data, work)
    left = {path.stem: {m.id for m in load(path)} for path in data.glob("run-*/batch/*.jsonl")}
    todo = [
        (b.group, m)
        for b in work
        if taken[b.name] < b.weight
        for m in b.messages
        if b.name not in left or m.id in left[b.name]
    ]
    ids = {(group, m.id) for group, m in todo}
    return {(group, m.quoted) for group, m in todo if m.quoted is not None} - ids


async def take_snapshot(forum: Forum, quoted: set[tuple[int, int]]) -> dict:
    """Every thread on the forum with its floors, the boards and the tags, and where the quoted
    messages are archived (tools/fake_forum.py reads it)."""
    boards = (await forum.call("GET", "/boards"))["items"]
    tags = [t["name"] for t in (await forum.call("GET", "/tags"))["items"]]
    threads = []
    cursor = None
    while True:
        params = {"view": "created"} | ({"cursor": cursor} if cursor else {})
        page = await forum.call("GET", "/threads", params=params)
        for t in page["items"]:
            threads.append(
                {
                    "id": t["id"],
                    "board": t["board"]["slug"],
                    "kind": t["kind"],
                    "title": t["title"],
                    "tags": t["tags"],
                    "created_at": t["created_at"],
                    "locked": t["locked"],
                    "posts": await posts_of(forum, t["id"]),
                }
            )
        cursor = page["next_cursor"]
        if cursor is None:
            break
    posts = {p["id"] for t in threads for p in t["posts"]}
    links = []
    for group, message in sorted(quoted):
        try:
            link = await forum.call("GET", f"/bot/messages/group/{group}/{message}")
        except ForumError as e:
            if e.code != "message_not_linked":
                raise
            continue
        # Not in a thread the snapshot has, deleted or merged away, it is no use following.
        if link["post_id"] in posts:
            links.append([group, message, link["post_id"]])
    return {"boards": boards, "tags": tags, "threads": threads, "links": links}


async def posts_of(forum: Forum, thread: int) -> list[dict]:
    posts: list[dict] = []
    while True:
        params = {"after": posts[-1]["floor"] if posts else 0, "body": "markdown"}
        page = (await forum.call("GET", f"/threads/{thread}/posts", params=params))["items"]
        posts += [
            {
                "id": p["id"],
                "floor": p["floor"],
                "body": p["body"],
                "reply_to": p["reply_to"]["id"] if p["reply_to"] else None,
                "created_at": p["created_at"],
                "edited_at": p["edited_at"],
                "deleted": p["deleted"],
            }
            for p in page
        ]
        if len(page) < PAGE:
            return posts


# ---- Importing


class Skipped(Exception):
    """A write the forum won't take now, or one that goes with a write skipped."""


async def import_(args: argparse.Namespace) -> None:
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    forum = Forum(config["forum_url"], config["token"], Patient())
    # Who left before the forum knew them gets an account first, by the names exported.
    senders = read_exports([Path(x) for x in args.exports]).senders
    await patiently(lambda: enroll(forum, senders))
    skipped = await import_journal(forum, Path(args.data))
    await forum.close()
    print(f"all imported, {sum(skipped.values()):,} skipped:", file=sys.stderr)
    for why, count in skipped.most_common():
        print(f"  {why}: {count:,}", file=sys.stderr)


async def import_journal(forum: Forum, data: Path) -> Counter[str]:
    """The writes of the journal not imported yet made on the forum, in order; why those
    skipped were, all told."""
    journal = (data / "forum" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    # Each write imported, a line each: what the IDs made offline are on the forum, or why it
    # was skipped. Stopped any time, the import goes on after the last.
    imported = data / "forum" / "imported.jsonl"
    ids: dict[int, int] = {}
    skipped: Counter[str] = Counter()
    position = 0
    if imported.exists():
        for line in imported.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            ids.update({int(k): v for k, v in record["ids"].items()})
            position = record["line"] + 1
            if "skipped" in record:
                skipped[record["skipped"]] += 1
    for n, line in enumerate(journal[position:], position):
        entry = json.loads(line)
        record: dict = {"line": n}
        try:
            made = await patiently(partial(make, forum, entry, ids))
            record["ids"] = made
            ids.update(made)
        except Skipped as e:
            record |= {"ids": {}, "skipped": f"{kind_of(entry)}: {e}"}
            skipped[record["skipped"]] += 1
        append_line(imported, json.dumps(record, ensure_ascii=False))
        if (n + 1) % 500 == 0:
            print(f"{n + 1:,} of {len(journal):,} imported", file=sys.stderr)
    return skipped


async def make(forum: Forum, entry: dict, ids: dict[int, int]) -> dict[int, int]:
    """One write of the journal made on the forum; the IDs it made offline, and those they are
    here."""

    def real(n: int) -> int:
        if n < OFFLINE_IDS:
            return n
        if n not in ids:
            raise Skipped("goes with one skipped")
        return ids[n]

    path = re.sub(r"\d+", lambda m: str(real(int(m.group()))), entry["path"])
    body = None if entry["body"] is None else dict(entry["body"])
    if body is not None and "reply_to_id" in body:
        target = body.pop("reply_to_id")
        # Replying to a floor skipped, a reply goes without the link, as the plugin's do.
        if target < OFFLINE_IDS or target in ids:
            body["reply_to_id"] = real(target)
    creates = entry["made"]
    while True:
        try:
            res = await forum.call(
                entry["method"], path, body=body, act_as=entry["act_as"], key=entry["key"]
            )
            break
        except ForumError as e:
            if e.code == "thread_merged" and path.endswith("/posts"):
                # Merged into another since, where the floor goes now.
                path = f"/threads/{e.details['thread_id']}/posts"
            elif e.status == 400 and body is not None and "reply_to_id" in body:
                # The floor it replies to is gone.
                del body["reply_to_id"]
            elif e.code == "source_exists" and creates:
                # Archived already: by the plugin, or by an import stopped halfway.
                res = {"id": e.details["thread_id"], "post": e.details["post_id"]}
                break
            elif e.code == "idempotency_key_reused" and creates:
                # Made in another thread by an import stopped halfway, before its thread was
                # merged on the forum.
                source = body["source"]
                path = f"/bot/messages/group/{source['chat_id']}/{source['message_ids'][0]}"
                found = await forum.call("GET", path)
                res = {"id": found["thread_id"], "post": found["post_id"]}
                break
            elif e.code == "tag_exists":
                return {}
            elif e.status in (400, 403, 404, 409):
                raise Skipped(e.code) from e
            else:
                raise
    if "thread" in creates:
        post = res.get("post")
        if post is None:
            params = {"after": 0}
            opening = await forum.call("GET", f"/threads/{res['id']}/posts", params=params)
            post = opening["items"][0]["id"]
        return {creates["thread"]: res["id"], creates["post"]: post}
    if "post" in creates:
        return {creates["post"]: res.get("post", res["id"])}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把历史收录交给别人离线跑，再把结果导入论坛（docs/history.md）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pack_ = commands.add_parser("pack", help="打包：导出、收录到哪了、论坛的快照")
    import__ = commands.add_parser("import", help="把离线收录发出的内容发到论坛")
    for command in (pack_, import__):
        command.add_argument("exports", nargs="+", help="QQChatExporter 导出的 JSON")
        command.add_argument("--config", required=True, help="插件的配置，要论坛的地址和 token")
    pack_.add_argument("--data", required=True, help="在线收录的数据目录")
    pack_.add_argument("--out", required=True, help="打包到哪个目录，要还没有")
    import__.add_argument("--data", required=True, help="离线收录的数据目录")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(pack(args) if args.command == "pack" else import_(args))


if __name__ == "__main__":
    main()
