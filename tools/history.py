"""Archives exported group chats onto the forum, as auto-archiving would have done as they came
(design §8.10), or onto a snapshot of it offline, to be imported later (docs/history.md). Every
segment is posted as it is done and the batches taken in are noted, so a run stopped any time
goes on where it was when started again with the same arguments.

uv run python -m tools.history run EXPORT... --config data/history.json --data history
uv run python -m tools.history status EXPORT... --data history
uv run python -m tools.history show EXPORT... --data history --out threads.md
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter, defaultdict
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
    load,
    shown_text,
    unavailable,
    until,
    worth,
)
from mousehole.files import append_line, write_file
from mousehole.forum import Forum, ForumError, error_of
from mousehole.llm import LLM, Embedder, Settings
from mousehole.similar import Index
from tools.fake_forum import FakeForum
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
# Of each LLM call, the tokens kept count of: `input` holds `cached` and `written`, the input
# read from the cache and written to it; `output` holds `reasoning`.
TOKENS = ("input", "cached", "written", "output", "reasoning")


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
    snapshot = data / "forum" / "snapshot.json"
    offline = snapshot.exists()
    if offline:
        fake = FakeForum.of_snapshot(json.loads(snapshot.read_text(encoding="utf-8")))
        forum = fake.journaled(data / "forum" / "journal.jsonl")
    else:
        forum = Forum(config["forum_url"], config["token"], Patient())
    settings = Settings.of(config["llm"] | {"tokens_per_day": 0})
    meter = Meter(data / "usage.jsonl", settings.model, config.get("prices"))
    llm = LLM(settings, record=meter.record)
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
        segments=args.segments,
        until=datetime.fromisoformat(args.until).timestamp() if args.until else float("inf"),
        since=datetime.fromisoformat(args.start).timestamp() if args.start else -float("inf"),
        offline=offline,
        meter=meter,
    )
    await forum.close()
    await llm.close()
    await embedder.close()


class Meter:
    """The LLM's usage written down call by call in `usage.jsonl`, with what each call cost at
    the prices of the config, per million tokens; and totalled for this run."""

    def __init__(self, path: Path, model: str, prices: dict | None):
        self.path = path
        self.model = model
        self.prices = prices
        self.tokens: Counter[str] = Counter()
        self.cost = 0.0

    def record(self, usage: dict) -> None:
        prompt = usage.get("prompt_tokens_details") or {}
        completion = usage.get("completion_tokens_details") or {}
        call = {
            "at": int(time.time()),
            "model": self.model,
            "input": usage.get("prompt_tokens", 0),
            "cached": prompt.get("cached_tokens", 0),
            "written": prompt.get("cache_write_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "reasoning": completion.get("reasoning_tokens", 0),
        }
        if self.prices:
            call |= {"cost": cost_of(call, self.prices), "currency": self.prices["currency"]}
        else:
            call["cost"] = None
        append_line(self.path, json.dumps(call))
        self.tokens.update({k: call[k] for k in TOKENS})
        self.cost += call["cost"] or 0

    def told(self, done: int, todo: int) -> str:
        """So far, and, at that rate, for all that was to do."""
        t = self.tokens
        used = f"{mega(t['input'])} in ({mega(t['cached'])} cached), {mega(t['output'])} out"
        if not self.prices:
            return used
        money = self.prices["currency"]
        return (
            f"{money}{self.cost:.2f} ({used}), about {money}{self.cost / done * todo:.2f} for all"
        )


def cost_of(call: dict, prices: dict) -> float:
    plain = call["input"] - call["cached"] - call["written"]
    return (
        plain * prices["input"]
        + call["cached"] * prices["cached_input"]
        + call["written"] * prices["cache_write"]
        + call["output"] * prices["output"]
    ) / 1e6


def mega(tokens: float) -> str:
    return f"{tokens / 1e6:.1f}M" if tokens >= 100_000 else f"{tokens / 1e3:.1f}k"


@dataclass
class Exports:
    """What the exports hold, each message once."""

    chats: dict[int, dict[int, Message]]
    bots: dict[int, int]
    # Who wrote in each group, as exported.
    senders: dict[int, dict[int, dict]]
    pictures: dict[str, Path]
    # The batches by their starts.
    work: list[Batch]


def read_exports(paths: list[Path], until: float = float("inf")) -> Exports:
    """The messages from before `until`."""
    chats: dict[int, dict[int, Message]] = {}
    bots: dict[int, int] = {}
    senders: dict[int, dict[int, dict]] = {}
    pictures: dict[str, Path] = {}
    for path in paths:
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
    work = sorted(
        (
            Batch(group, batch, sum(worth(m, bots[group], PREFIXES) for m in batch))
            for group, kept in chats.items()
            for batch in batches(sorted(kept.values(), key=lambda m: m.time))
        ),
        key=lambda b: b.start,
    )
    return Exports(chats, bots, senders, pictures, work)


def organized(data: Path, work: list[Batch]) -> dict[str, int]:
    """Of each batch, how many of the messages for the LLM it has organized: all of one taken
    in, but those left in its run's directory of one stopped halfway."""
    done_file = data / "done.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    left = {path.stem: len(load(path)) for path in data.glob("run-*/batch/*.jsonl")}
    return {
        b.name: b.weight - left[b.name] if b.name in left else b.weight * (b.name in done)
        for b in work
    }


async def archive_exports(
    forum: Forum,
    complete: Complete,
    index: Index,
    exports: list[Path],
    data: Path,
    tuning: Tuning,
    workers: int,
    *,
    segments: int | None = None,
    until: float = float("inf"),
    since: float = -float("inf"),
    offline: bool = False,
    meter: Meter | None = None,
) -> None:
    """Each export's messages from before `until` taken in and archived batch by batch, as the
    plugin would have, by `workers` side by side, those begun before `since` left for later;
    stopped after `segments` segments or so, to try a few. `offline` is the forum a snapshot,
    which nobody else posts to."""
    data.mkdir(parents=True, exist_ok=True)
    # One index for all, so each worker sees the threads the others post; rebuilt online, for
    # those the plugin posted since the last run, while offline nobody else posts.
    await patiently(index.load if offline else index.rebuild)
    read = read_exports(exports, until)
    if offline and read.pictures:
        # Uploaded to the snapshot, they would be nowhere to import from.
        raise SystemExit("Offline runs take exports without pictures")
    bots, pictures = read.bots, read.pictures
    await patiently(lambda: enroll(forum, read.senders))
    work = read.work

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
    # The runs are cut from all, for a run without `since` to go on with the rest.
    work = [b for b in work if b.start >= since]
    already = organized(data, work)
    todo = sum(b.weight - already[b.name] for b in work)
    started = time.monotonic()
    # Segments, and the messages in them, organized in this run.
    count = moved = 0

    def enough() -> bool:
        return segments is not None and count >= segments

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

    async def organize(n: int, run: Run) -> None:
        """Its batches. Its end may come nearer as it goes, by a take-over."""
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
            nonlocal count, moved
            model = {"model": meter.model} if meter else {}
            with segments_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r | {"run": n} | model, ensure_ascii=False) + "\n")
            count += 1
            # A segment split is organized again in halves.
            if not any(d.startswith("split:") for d in r["dropped"]):
                moved += r["numbers"][1] - r["numbers"][0] + 1
            posted = sum(not d.startswith("tag ") for d in r["done"])
            line = (
                f"run {n} group {r['group']} {clock(r['times'][0])}: "
                f"{posted} done, {len(r['dropped'])} dropped; {moved:,} of {todo:,} messages"
            )
            if moved:
                hours = (time.monotonic() - started) / moved * (todo - moved) / 3600
                line += f", {hours:.1f} h to go"
                if meter:
                    line += f"; {meter.told(moved, todo)}"
            print(line, file=sys.stderr)

        async def go_on() -> None:
            async for r in archive.batch():
                report(r)
                if enough():
                    return

        await patiently(go_on)
        for b in work:
            if enough():
                return
            if not run.start <= b.start < run.end or b.name in done:
                continue
            run.at, now = b.start, b.messages[-1].time
            for m in b.messages:
                if worth(m, bots[b.group], PREFIXES):
                    await upload(forum, m, pictures)
                    archive.wait(b.group, m.record())
            archive.start_round(everything=True)
            with done_file.open("a") as f:
                f.write(b.name + "\n")
            done.add(b.name)
            await patiently(go_on)

    async def worker() -> None:
        while not enough():
            n = next((n for n, r in enumerate(runs) if not r.taken and not finished(n)), None)
            if n is None:
                if take_over(runs, work, done) is None:
                    return
                n = len(runs) - 1
                was = next(i for i, r in enumerate(runs) if r.end == runs[n].start)
                start, end = clock(runs[n].start), clock(runs[n].end)
                log.info("Run %d: %s to %s, taken over from run %d", n, start, end, was)
            runs[n].taken = True
            save_runs()
            await organize(n, runs[n])
            if finished(n):
                log.info("Run %d: done", n)

    await asyncio.gather(*(worker() for _ in range(workers)))


def status(args: argparse.Namespace) -> None:
    """How far the archiving got, day by day, and what the LLM cost."""
    data = Path(args.data)
    work = read_exports([Path(x) for x in args.exports]).work
    taken = organized(data, work)
    # Days from the first of BATCH_HOURS, as batches go: (messages for the LLM, organized)
    days: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for b in work:
        day = datetime.fromtimestamp(b.start - BATCH_HOURS[0] * 3600).strftime("%Y-%m-%d")
        days[day][0] += b.weight
        days[day][1] += taken[b.name]
    total, done = sum(d[0] for d in days.values()), sum(d[1] for d in days.values())
    print(f"交给大模型的消息：收了 {done:,} 条，共 {total:,} 条（{done / total:.1%}）")
    months: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for day, (all_, got) in days.items():
        months[day[:7]][0] += all_
        months[day[:7]][1] += got
    for month, (all_, got) in sorted(months.items()):
        print(f"  {month}：{got:,} / {all_:,}")
    half = [
        f"{d[5:]}（{got:,}/{all_:,}）" for d, (all_, got) in sorted(days.items()) if 0 < got < all_
    ]
    untouched = [d[5:] for d, (all_, got) in sorted(days.items()) if all_ and not got]
    print(f"收了一半的 {len(half)} 天：{'、'.join(half) or '无'}")
    print(f"没收的 {len(untouched)} 天：{'、'.join(untouched) or '无'}")

    calls = lines_of(data / "usage.jsonl")
    # Messages each model organized, from the segments that say which.
    reports = lines_of(data / "segments.jsonl")
    moved: Counter[str] = Counter()
    for r in reports:
        if "model" in r and not any(d.startswith("split:") for d in r["dropped"]):
            moved[r["model"]] += r["numbers"][1] - r["numbers"][0] + 1
    print("\n大模型：")
    for model in sorted({c["model"] for c in calls}):
        mine = [c for c in calls if c["model"] == model]
        t = {k: sum(c[k] for c in mine) for k in TOKENS}
        line = (
            f"  {model}：{len(mine):,} 次，输入 {mega(t['input'])}（缓存读 {mega(t['cached'])}，"
            f"写 {mega(t['written'])}），输出 {mega(t['output'])}（思考 {mega(t['reasoning'])}）"
        )
        if mine[0]["cost"] is not None:
            cost, money = sum(c["cost"] for c in mine), mine[0]["currency"]
            line += f"，花了 {money}{cost:.2f}"
            if moved[model]:
                each = cost / moved[model]
                line += (
                    f"；收了 {moved[model]:,} 条，每千条 {money}{each * 1000:.3f}，"
                    f"照这样收完剩下的 {total - done:,} 条要 {money}{each * (total - done):.2f}"
                )
        print(line)
    dropped = Counter(d.split(":")[0] for r in reports if "model" in r for d in r["dropped"])
    print(f"  丢掉的项：{dict(dropped.most_common()) or '无'}")

    if (data / "forum").exists():
        kinds = Counter(kind_of(e) for e in lines_of(data / "forum" / "journal.jsonl"))
        made = "，".join(f"{k} {n:,}" for k, n in kinds.most_common()) or "还没有"
        print(
            f"\n离线发出的：{made}；已经导入论坛 {len(lines_of(data / 'forum' / 'imported.jsonl')):,} 条"
        )


def lines_of(path: Path) -> list[dict]:
    """A file of a JSON object a line, empty before the first is written."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def kind_of(entry: dict) -> str:
    """What a write in the journal is, for people."""
    match entry["method"], entry["path"].strip("/").split("/"):
        case "POST", ["threads"]:
            return "新帖"
        case "POST", ["threads", _, "posts"]:
            return "回复"
        case "PATCH", ["posts", _]:
            return "更新"
        case "PATCH", ["threads", _]:
            return "改标题"
        case "PUT", ["posts", _, "reaction"]:
            return "表情回应"
        case "PUT", ["posts", _, "boost"]:
            return "短评"
        case "POST", ["tags"]:
            return "新标签"
    return f"{entry['method']} {entry['path']}"


def show(args: argparse.Namespace) -> None:
    """What an offline run made, for people to read: the threads it opened, and its floors in
    threads the forum had, each floor with the messages it came from."""
    data = Path(args.data)
    chats = read_exports([Path(x) for x in args.exports]).chats
    messages = {(group, m.id): m for group, chat in chats.items() for m in chat.values()}
    snapshot = json.loads((data / "forum" / "snapshot.json").read_text(encoding="utf-8"))
    fake = FakeForum.of_snapshot(snapshot)
    journal = data / "forum" / "journal.jsonl"
    fake.journaled(journal)
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    made = {e["made"]["post"] for e in entries if "post" in e["made"]}
    threads = sorted({fake.posts[p].thread for p in made}, key=lambda t: fake.threads[t].created_at)
    sources: dict[int, list[Message]] = defaultdict(list)
    for key, post in fake.links.items():
        if post in made:
            sources[post].append(messages[key])
    lines = []
    for t in (fake.threads[t] for t in threads):
        kind = "讨论" if t.kind == "discussion" else "问答"
        old = "" if t.posts[0].id in made else f"（论坛上已有的帖子 {t.id}，只列新的楼层）"
        lines += [
            f"## {t.title}{old}",
            f"{kind}，版块 {t.board}，标签 {'、'.join(t.tags) or '无'}",
            "",
        ]
        for post in t.posts:
            if post.id not in made:
                continue
            said = sorted(sources[post.id], key=lambda m: m.time)
            notes = [f"回复 {fake.posts[post.reply_to].floor} 楼"] if post.reply_to else []
            if post.revisions:
                notes.append(f"更新过 {len(post.revisions)} 次")
            note = f"（{'，'.join(notes)}）" if notes else ""
            lines += [f"### {post.floor} 楼 · 成员 {said[0].user}{note}", "", post.body, ""]
            for user, emoji in post.reactions.items():
                lines.append(f"- 回应 · 成员 {user}：{emoji}")
            for user, boost in post.boosts.items():
                lines.append(f"- 短评 · 成员 {user}：{boost}")
            lines.append("原消息：")
            lines += [
                f"- {clock(m.time)} 成员 {m.user}：{shown_text(m, lambda u: f'成员 {u}')}"
                for m in said
            ]
            lines.append("")
    text = f"# 离线收录发出的帖子（{len(threads)} 个）\n\n" + "\n".join(lines)
    Path(args.out).write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="历史收录（设计第 8.10 节，docs/history.md）")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="收录：数据目录里有论坛的快照时离线收，否则发到论坛")
    run.add_argument("exports", nargs="+", help="QQChatExporter 导出的 JSON")
    run.add_argument("--config", required=True, help="插件的配置，大模型可以另换")
    run.add_argument("--data", required=True, help="进度和中间文件放在哪个目录")
    run.add_argument("--workers", type=int, default=WORKERS, help="同时收几批")
    run.add_argument("--segments", type=int, help="收了这么多段左右就停，试跑时用")
    run.add_argument(
        "--until", help="只收这一刻（YYYY-MM-DD HH:MM）之前的消息，之后的由插件的自动收录来收"
    )
    run.add_argument(
        "--from",
        dest="start",
        help="只收这一刻（YYYY-MM-DD HH:MM）以后开始的批次，先收要紧的一段；之前的留给不带它的下一次",
    )
    run.add_argument("--off-peak", action="store_true", help="避开 Step Plan 的高峰时段")
    status_ = commands.add_parser("status", help="收到哪了、花了多少")
    show_ = commands.add_parser("show", help="把离线收录发出的帖子和原消息排成 Markdown")
    for command in (status_, show_):
        command.add_argument("exports", nargs="+", help="QQChatExporter 导出的 JSON")
        command.add_argument("--data", required=True, help="收录的数据目录")
    show_.add_argument("--out", required=True, help="写到哪个文件")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Not every request it makes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "run":
        asyncio.run(history(args))
    elif args.command == "status":
        status(args)
    else:
        show(args)


if __name__ == "__main__":
    main()
