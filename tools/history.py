"""Archives exported group chats onto the forum, as auto-archiving would have done as they came
(design §8.10). Every segment is posted as it is done and the batches taken in are noted, so a
run stopped any time goes on where it was when started again with the same arguments.

uv run python -m tools.history EXPORT... --config data/history.json --data history
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from pathlib import Path

import httpx

from mousehole.archive import (
    PAUSE,
    Archive,
    Complete,
    Message,
    Tuning,
    is_sticker,
    unavailable,
    until,
)
from mousehole.forum import Forum, ForumError, error_of
from mousehole.llm import LLM, Embedder, Settings
from mousehole.similar import Index
from tools.replay import clock, convert

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Batches are worked on side by side, as many as Step Plan answers at once.
WORKERS = 5
# Batches start at these hours: unlike the plugin, which takes a talk in once it stops, the
# history has no hurry, and whole batches keep more talks whole in fewer calls.
BATCH_HOURS = (4, 18)
# Step Plan's peak hours, Beijing time on weekdays, when Step 5 Preview costs twice as much.
PEAK = ((9, 12), (14, 18))


class Patient(httpx.AsyncBaseTransport):
    """Waits out the forum's rate limits instead of losing what would have been posted."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        while True:
            res = await self.inner.handle_async_request(request)
            if res.status_code != 429:
                return res
            await res.aread()
            wait = int(res.headers.get("Retry-After", "60"))
            log.warning("The forum limits %s; asking again in %d seconds", request.url.path, wait)
            await asyncio.sleep(wait)

    async def aclose(self) -> None:
        await self.inner.aclose()


def off_peak(complete: Complete) -> Complete:
    """The LLM asked only outside PEAK."""

    async def asked(system: str, prompt: str, schema: dict) -> tuple[str, int, int]:
        while (now := datetime.now()).weekday() < 5 and any(a <= now.hour < b for a, b in PEAK):
            await asyncio.sleep(60)
        return await complete(system, prompt, schema)

    return asked


def batches(messages: list[Message]) -> list[list[Message]]:
    """The messages in batches at BATCH_HOURS."""
    found: list[list[Message]] = []
    end = float("-inf")
    for m in messages:
        if m.time >= end:
            found.append([])
            end = m.time + until(BATCH_HOURS, m.time)
        found[-1].append(m)
    return found


def shares(work: list[tuple[int, list[Message]]], count: int) -> list[list[tuple[int, list]]]:
    """Batches in order of time, cut into `count` runs of about as many messages each."""
    total = sum(len(b) for _, b in work)
    runs: list[list[tuple[int, list]]] = [[] for _ in range(count)]
    seen = 0
    for group, batch in work:
        runs[min(count - 1, seen * count // total)].append((group, batch))
        seen += len(batch)
    return runs


async def upload(forum: Forum, m: Message, pictures: dict[str, Path]) -> None:
    """Its pictures copied to the forum from the export, those the export kept."""
    for s in m.segments:
        path = pictures.get((s["data"].get("md5") or "").lower())
        if s["type"] != "image" or is_sticker(s) or path is None:
            continue
        try:
            url = await patiently(partial(upload_picture, forum, path, m.user))
        except ForumError as e:
            log.warning("A picture in message %s could not be copied: %s", m.id, e)
            continue
        m.uploads.append(url)


async def upload_picture(forum: Forum, path: Path, user: int) -> str:
    """Uploaded as the sender, or as the bot for one who left: what is archived for them must
    come from their messages, and an upload doesn't."""
    file = {"file": (path.name, path.read_bytes())}
    res = await forum.http.post("/uploads", files=file, headers={"X-Act-As": f"qq:{user}"})
    if res.status_code == 403 and res.json()["error"]["code"] == "member_frozen":
        res = await forum.http.post("/uploads", files=file)
    if res.is_error:
        raise error_of(res)
    return res.json()["url"]


async def enroll(forum: Forum, senders: dict[int, dict[int, dict]]) -> None:
    """Accounts for those who left a group before the forum knew them, as if they had left now:
    frozen like the others who left, they are archived under their names (design §8.10)."""
    for group, people in senders.items():
        missing = []
        for qq, sender in people.items():
            try:
                await forum.call("GET", f"/users/qq:{qq}")
            except ForumError as e:
                if e.status != 404:
                    raise
                names = {"nickname": sender.get("nickname"), "card": sender.get("groupCard")}
                # Some had no card in the group, a few no nickname either.
                missing.append(
                    {"user_id": qq, "in_group": False} | {k: v for k, v in names.items() if v}
                )
        for i in range(0, len(missing), 500):
            body = {"observed_at": int(time.time()), "members": missing[i : i + 500]}
            await forum.call("PATCH", f"/bot/groups/{group}/members", body=body)
        log.info("Group %s: made accounts for %d who left before the forum", group, len(missing))


async def patiently[T](work: Callable[[], Awaitable[T]]) -> T:
    """What the forum, the LLM or the embedding model being down stops, done once they are up."""
    while True:
        try:
            return await work()
        except Exception as e:
            if not unavailable(e):
                raise
            log.warning("Waiting %d minutes: %r", PAUSE // 60, e)
            await asyncio.sleep(PAUSE)


async def history(args: argparse.Namespace) -> None:
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    forum = Forum(config["forum_url"], config["token"], Patient())
    llm = LLM(Settings.of(config["llm"] | {"tokens_per_day": 0}))
    e = config["embedding"]
    embedder = Embedder(e["base_url"], e["api_key"], e["model"])
    index = Index(forum, embedder.embed, e["model"], data / "threads.npz", e["min_similarity"])
    complete = off_peak(llm.complete) if args.off_peak else llm.complete
    await archive_exports(
        forum,
        complete,
        index,
        [Path(x) for x in args.exports],
        data,
        Tuning.of(config),
        args.workers,
        args.batches,
        datetime.fromisoformat(args.until).timestamp() if args.until else float("inf"),
    )
    await forum.close()
    await llm.close()
    await embedder.close()


async def archive_exports(
    forum: Forum,
    complete: Complete,
    index: Index,
    exports: list[Path],
    data: Path,
    tuning: Tuning,
    workers: int,
    most: int | None = None,
    until: float = float("inf"),
) -> None:
    """Each export's messages from before `until` taken in and archived batch by batch, as the
    plugin would have, `most` batches a worker at most."""
    stranded = [p for p in data.glob("worker-*/batch/*.jsonl") if int(p.parts[-3][7:]) >= workers]
    if stranded:
        raise SystemExit(f"{len(stranded)} batches are left by more workers than {workers}")
    # One index for all, so each worker sees the threads the others post; rebuilt, for those
    # the plugin posted since the last run.
    await patiently(index.rebuild)
    chats: dict[int, dict[int, Message]] = {}
    bots: dict[int, int] = {}
    senders: dict[int, dict[int, dict]] = {}
    pictures: dict[str, Path] = {}
    for path in exports:
        export = json.loads(path.read_text(encoding="utf-8"))
        group, bot, messages = convert(export)
        bots[group] = bot
        # Exports may overlap: each message once.
        chats.setdefault(group, {}).update((m.id, m) for m, _, _ in messages if m.time < until)
        senders.setdefault(group, {}).update(
            (int(m["sender"]["uin"]), m["sender"])
            for m in export["messages"]
            # System messages have no sender.
            if not m["system"] and m["timestamp"] / 1000 < until and m["sender"]["uin"] != str(bot)
        )
        for p in (path.parent / "resources" / "images").glob("*"):
            pictures[p.name.split("_")[0].lower()] = p
    await patiently(lambda: enroll(forum, senders))
    work = [
        (group, batch)
        for group, kept in chats.items()
        for batch in batches(sorted(kept.values(), key=lambda m: m.time))
    ]
    work.sort(key=lambda w: w[1][0].time)

    done_file = data / "done.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    segments_file = data / "segments.jsonl"

    async def nothing(group: int, segments: list[dict]) -> None:
        pass

    async def worker(n: int, share: list[tuple[int, list[Message]]]) -> None:
        now = 0.0
        archive = Archive(
            forum,
            complete,
            index,
            data=data / f"worker-{n}",
            prefixes=["/"],
            site="",
            # No summary is sent (design §8.10).
            templates={},
            notify=nothing,
            tuning=tuning,
            clock=lambda: now,
        )
        archive.indexed = True
        await patiently(archive.refresh_taxonomy)
        # A batch stopped halfway goes on; one stopped while being taken in is taken in again.
        for path in archive.pending.glob("*.jsonl"):
            path.unlink()
        done.update(path.stem for path in archive.batches.glob("*.jsonl"))

        def report(r: dict) -> None:
            with segments_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            posted = sum(not d.startswith("tag ") for d in r["done"])
            print(
                f"worker {n} group {r['group']} {clock(r['times'][0])}: "
                f"{posted} done, {len(r['dropped'])} dropped",
                file=sys.stderr,
            )

        async def run() -> None:
            async for r in archive.batch():
                report(r)

        await patiently(run)
        taken = 0
        for group, batch in share:
            # As start_round names its file.
            now = batch[-1].time
            name = f"{group}-{int(now)}"
            if name in done:
                continue
            if most is not None and taken == most:
                break
            for m in batch:
                if archive.worth(m, bots[group]):
                    await upload(forum, m, pictures)
                    archive.wait(group, m.record())
            archive.start_round(everything=True)
            with done_file.open("a") as f:
                f.write(name + "\n")
            taken += 1
            await patiently(run)

    await asyncio.gather(*(worker(n, s) for n, s in enumerate(shares(work, workers))))


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive exported group chats onto the forum")
    parser.add_argument("exports", nargs="+", help="QQChatExporter 导出的 JSON")
    parser.add_argument("--config", required=True, help="插件的配置，大模型可以另换")
    parser.add_argument("--data", required=True, help="进度和中间文件放在哪个目录")
    parser.add_argument("--workers", type=int, default=WORKERS, help="同时收几批，续跑时不要改")
    parser.add_argument("--batches", type=int, help="每个 worker 最多收这么多批，先小范围试时用")
    parser.add_argument(
        "--until", help="只收这一刻（YYYY-MM-DD HH:MM）之前的消息，之后的由插件的自动收录来收"
    )
    parser.add_argument("--off-peak", action="store_true", help="避开 Step Plan 的高峰时段")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Not every request it makes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(history(parser.parse_args()))


if __name__ == "__main__":
    main()
