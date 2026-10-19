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
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from itertools import pairwise
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
    worth,
)
from mousehole.files import write_file
from mousehole.forum import Forum, ForumError, error_of
from mousehole.llm import LLM, Embedder, Settings
from mousehole.similar import Index
from tools.replay import clock, convert

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Batches are worked on side by side, as many as Step Plan answers at once.
WORKERS = 5
# Commands to the bot, not talk.
PREFIXES = ["/"]
# Batches start at these hours: unlike the plugin, which takes a talk in once it stops, the
# history has no hurry, and whole batches keep more talks whole in fewer calls.
BATCH_HOURS = (4, 18)
# Of what is left of a run, the least a worker done with its own takes over (design §8.10):
# less is done soon enough without a talk cut off.
TAKE_OVER_LEAST = 1000
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


@dataclass
class Batch:
    group: int
    messages: list[Message]
    # Those the LLM organizes, which the time it takes goes by.
    weight: int

    @property
    def start(self) -> float:
        return self.messages[0].time

    @property
    def name(self) -> str:
        # As start_round names its file.
        return f"{self.group}-{int(self.messages[-1].time)}"


@dataclass(eq=False)
class Run:
    """The batches from `start` to `end`, by their first messages, organized in order by one
    worker, which follows its threads from one into the next (design §8.10)."""

    start: float
    end: float
    # The first message of the batch being organized; whether a worker has the run.
    at: float = field(default=float("-inf"), compare=False)
    taken: bool = field(default=False, compare=False)


def cuts(work: list[Batch]) -> list[float]:
    """Where runs may be cut: at the first of BATCH_HOURS, when the group is quietest, for a
    talk cut off goes on without what was said before."""
    found = set()
    for b in work:
        t = datetime.fromtimestamp(b.start)
        if BATCH_HOURS[0] <= t.hour < BATCH_HOURS[1]:
            found.add(t.replace(hour=BATCH_HOURS[0], minute=0, second=0, microsecond=0))
    return sorted(t.timestamp() for t in found)


def nearest(work: list[Batch], points: list[float], goal: float) -> float | None:
    """The point with about `goal` of the work's weight before it."""
    return min(
        points, key=lambda c: abs(sum(b.weight for b in work if b.start < c) - goal), default=None
    )


def runs_of(work: list[Batch], count: int) -> list[Run]:
    """The work cut into `count` runs of about the same weight."""
    if not work:
        return []
    total = sum(b.weight for b in work)
    bounds = [work[0].start]
    for k in range(1, count):
        cut = nearest(work, cuts(work), total * k / count)
        if cut is not None and cut > bounds[-1]:
            bounds.append(cut)
    bounds.append(work[-1].start + 1)
    return [Run(a, b) for a, b in pairwise(bounds)]


def take_over(runs: list[Run], work: list[Batch], done: set[str]) -> Run | None:
    """For a worker done with its run: the later half of what is left of the run with most
    left, cut where runs may be, if that half weighs TAKE_OVER_LEAST at least."""

    def left(r: Run) -> list[Batch]:
        """After the batch at hand and those taken in, one of which may be organized still."""
        mine = [b for b in work if r.start <= b.start < r.end]
        begun = max([r.at] + [b.start for b in mine if b.name in done])
        return [b for b in mine if b.start > begun]

    run = max(runs, key=lambda r: sum(b.weight for b in left(r)))
    rest = left(run)
    points = [c for c in cuts(rest) if c > rest[0].start] if rest else []
    cut = nearest(rest, points, sum(b.weight for b in rest) / 2)
    if cut is None or sum(b.weight for b in rest if b.start >= cut) < TAKE_OVER_LEAST:
        return None
    later = Run(cut, run.end)
    run.end = cut
    runs.append(later)
    return later


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
    plugin would have, by `workers` side by side, `most` batches each at most."""
    data.mkdir(parents=True, exist_ok=True)
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
            # System messages have no sender; one not known for sure, below zero, no account.
            if not m["system"]
            and m["timestamp"] / 1000 < until
            and 0 < int(m["sender"]["uin"]) != bot
        )
        for p in (path.parent / "resources" / "images").glob("*"):
            pictures[p.name.split("_")[0].lower()] = p
    await patiently(lambda: enroll(forum, senders))
    work = sorted(
        (
            Batch(group, batch, sum(worth(m, bots[group], PREFIXES) for m in batch))
            for group, kept in chats.items()
            for batch in batches(sorted(kept.values(), key=lambda m: m.time))
        ),
        key=lambda b: b.start,
    )

    # Batches taken in: written down as each is, or, were it stopped just then, left in its
    # run's directory to be organized.
    done_file = data / "done.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    done.update(path.stem for path in data.glob("run-*/batch/*.jsonl"))
    segments_file = data / "segments.jsonl"
    runs_file = data / "runs.json"
    if runs_file.exists():
        runs = [Run(**r) for r in json.loads(runs_file.read_text())]
    else:
        runs = runs_of(work, workers)
    outside = [b for b in work if not any(r.start <= b.start < r.end for r in runs)]
    if outside:
        raise SystemExit(f"{len(outside)} batches are in none of the runs of {runs_file}")

    def save_runs() -> None:
        write_file(runs_file, json.dumps([{"start": r.start, "end": r.end} for r in runs]))

    save_runs()
    for n, r in enumerate(runs):
        log.info("Run %d: %s to %s", n, clock(r.start), clock(r.end))

    def finished(n: int) -> bool:
        """All its batches taken in, the last organized too."""
        r = runs[n]
        taken = all(b.name in done for b in work if r.start <= b.start < r.end)
        return taken and not any((data / f"run-{n}" / "batch").glob("*.jsonl"))

    async def nothing(group: int, segments: list[dict]) -> None:
        pass

    async def organize(n: int, run: Run, most: int | None) -> int:
        """Its batches, `most` at most, of which the number taken is returned. Its end may come
        nearer as it goes, by a take-over."""
        now = 0.0
        archive = Archive(
            forum,
            complete,
            index,
            data=data / f"run-{n}",
            prefixes=PREFIXES,
            site="",
            # No summary is sent (design §8.10).
            templates={},
            notify=nothing,
            tuning=tuning,
            clock=lambda: now,
        )
        # Threads the runs before this one open as they go are left to them.
        archive.elsewhere = (work[0].start, run.start)
        await patiently(archive.refresh_taxonomy)
        # A batch stopped halfway goes on; one stopped while being taken in is taken in again.
        for path in archive.pending.glob("*.jsonl"):
            path.unlink()

        def report(r: dict) -> None:
            with segments_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r | {"run": n}, ensure_ascii=False) + "\n")
            posted = sum(not d.startswith("tag ") for d in r["done"])
            print(
                f"run {n} group {r['group']} {clock(r['times'][0])}: "
                f"{posted} done, {len(r['dropped'])} dropped",
                file=sys.stderr,
            )

        async def go_on() -> None:
            async for r in archive.batch():
                report(r)

        await patiently(go_on)
        taken = 0
        for b in work:
            if not run.start <= b.start < run.end or b.name in done:
                continue
            if most is not None and taken == most:
                break
            run.at, now = b.start, b.messages[-1].time
            for m in b.messages:
                if worth(m, bots[b.group], PREFIXES):
                    await upload(forum, m, pictures)
                    archive.wait(b.group, m.record())
            archive.start_round(everything=True)
            with done_file.open("a") as f:
                f.write(b.name + "\n")
            done.add(b.name)
            taken += 1
            await patiently(go_on)
        return taken

    async def worker() -> None:
        taken = 0
        while most is None or taken < most:
            n = next((n for n, r in enumerate(runs) if not r.taken and not finished(n)), None)
            if n is None:
                if take_over(runs, work, done) is None:
                    return
                n = len(runs) - 1
                log.info(
                    "Run %d: %s to %s, taken over", n, clock(runs[n].start), clock(runs[n].end)
                )
            runs[n].taken = True
            save_runs()
            taken += await organize(n, runs[n], None if most is None else most - taken)
            if finished(n):
                log.info("Run %d: done", n)

    await asyncio.gather(*(worker() for _ in range(workers)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive exported group chats onto the forum")
    parser.add_argument("exports", nargs="+", help="QQChatExporter 导出的 JSON")
    parser.add_argument("--config", required=True, help="插件的配置，大模型可以另换")
    parser.add_argument("--data", required=True, help="进度和中间文件放在哪个目录")
    parser.add_argument("--workers", type=int, default=WORKERS, help="同时收几批")
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
