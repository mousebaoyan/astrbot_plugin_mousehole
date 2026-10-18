"""Auto-archiving (design §8). Group messages wait on disk till the talk stops; the LLM organizes
what each segment of a round has worth keeping into threads and floors under their
writers' names. The questions stay followed, so later answers and additions find their place,
and each morning a summary in the group names the threads."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import judge
from .files import append_line, write_file
from .forum import Forum, ForumError
from .llm import Truncated, Unavailable
from .qq import at, fill, joined
from .similar import Index

# The logger AstrBot sets up for the plugin, which its handlers need the fields of.
log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# (system prompt, prompt, JSON schema) → (answer, input tokens, output tokens)
Complete = Callable[[str, str, dict], Awaitable[tuple[str, int, int]]]
# (group, OneBot message segments): sends to the group
Notify = Callable[[int, list[dict]], Awaitable[None]]
# (group, a message's ID or None for the latest): the OneBot messages of the group's history up
# to it, as NapCat's get_group_msg_history gives them
History = Callable[[int, int | None], Awaitable[list[dict]]]


@dataclass
class Tuning:
    """What replay tunes (docs/replay.md §1), which the plugin's settings may change."""

    # A segment, organized in one call, holds at most this many messages: the LLM thinks longer
    # the more there are, and past its output limit the whole segment is lost.
    segment_messages: int = 50
    # A question is followed until this many messages have passed its last related one.
    follow_messages: int = 200

    @classmethod
    def of(cls, config: dict) -> Tuning:
        """From the plugin's settings (_conf_schema.json)."""
        archive = config["archive"]
        return cls(
            segment_messages=archive["segment_messages"],
            follow_messages=archive["follow_messages"],
        )


# Messages before a segment that the LLM sees too.
ORGANIZE_BEFORE = 20
# Messages kept per group before the segment worked on, for context and quotes.
KEPT = 1000
# Seconds between looks at the messages waiting, and between rounds of 猜你想问.
ROUND = 600
# A group's waiting messages are taken in once it has been QUIET for that many seconds, if
# LEAST of them wait or the first has waited LONGEST: a few don't make a call of their own. At
# BACKLOG, with the talk going on, those up to a lull are (design §8.1).
QUIET = 1800
LEAST = 20
LONGEST = 3 * 3600
BACKLOG = 150
SUMMARY_HOUR = 9
# Threads a summary names, so that it @s few and doesn't flood the group.
SUMMARY_ITEMS = 10
# Seconds before a round the LLM, the embedding model or the forum couldn't be had for, or a
# summary that couldn't be sent, is tried again.
PAUSE = 600
NEW_TAGS_PER_DAY = 10
# Seconds between pages of a group's history read back, as a person scrolling up.
HISTORY_GAP = 1

PLACEHOLDERS = {
    "file": "[文件]",
    "forward": "[聊天记录]",
    "json": "[卡片]",
    "record": "[语音]",
    "video": "[视频]",
}


def is_sticker(segment: dict) -> bool:
    return segment["type"] == "image" and str(segment["data"].get("sub_type", 0)) == "1"


def shows(segment: dict) -> bool:
    """Whether a segment says anything: stickers and faces don't."""
    if segment["type"] == "text":
        return bool(segment["data"]["text"].strip())
    return segment["type"] not in ("face", "mface", "reply") and not is_sticker(segment)


def plain(segments: list[dict]) -> str:
    return "".join(s["data"]["text"] for s in segments if s["type"] == "text")


def unavailable(e: Exception) -> bool:
    """Whether something failed for want of a service rather than for what it held: the segment
    is tried again later, not lost (design §8.2)."""
    # 401 is the bot token turned away, for every call alike.
    return isinstance(e, Unavailable | httpx.TransportError) or (
        isinstance(e, ForumError) and (e.status == 401 or e.status >= 500)
    )


def until(hours: tuple[int, ...], now: float) -> float:
    """Seconds from `now` to the next of these hours o'clock."""
    hour = datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
    times = [(hour.replace(hour=h) + timedelta(days=d)).timestamp() for d in (0, 1) for h in hours]
    return min(t for t in times if t > now) - now


def to_take(messages: list[Message], now: float) -> int:
    """How many of a group's waiting messages are taken in now (design §8.1)."""
    if not messages:
        return 0
    if now - messages[-1].time >= QUIET and (
        len(messages) >= LEAST or now - messages[0].time >= LONGEST
    ):
        return len(messages)
    if len(messages) < BACKLOG:
        return 0
    # Where the group was quiet longest, at least LEAST taken and the newest half left.
    return max(
        range(LEAST, len(messages) - BACKLOG // 2 + 1),
        key=lambda i: messages[i].time - messages[i - 1].time,
    )


def segments(messages: list[Message], most: int) -> list[list[Message]]:
    """A round cut where the group was quiet longest until no piece holds more than `most`, so a
    talk mostly stays whole; then neighbours joined while they fit, so the night's few messages
    don't each make a call (design §8.1)."""
    pieces: list[list[Message]] = []
    todo = [messages]
    while todo:
        ms = todo.pop()
        if len(ms) <= most:
            pieces.append(ms)
            continue
        i = max(range(1, len(ms)), key=lambda i: ms[i].time - ms[i - 1].time)
        todo += [ms[i:], ms[:i]]
    joined: list[list[Message]] = []
    for piece in pieces:
        if joined and len(joined[-1]) + len(piece) <= most:
            joined[-1] = joined[-1] + piece
        else:
            joined.append(piece)
    return joined


@dataclass(eq=False)
class Message:
    id: int
    user: int
    time: float
    segments: list[dict]
    # Where its pictures are on the forum, copied there as it came.
    uploads: list[str] = field(default_factory=list)
    # Its number in the group, which the models refer to it by.
    number: int = 0
    floor: Floor | None = None
    # The floor it was a reaction to, when it said no more than that.
    reacted: Floor | None = None
    recalled: bool = False

    @property
    def quoted(self) -> int | None:
        return next((int(s["data"]["id"]) for s in self.segments if s["type"] == "reply"), None)

    @property
    def pictures_only(self) -> bool:
        shown = [s for s in self.segments if shows(s)]
        return bool(shown) and all(s["type"] == "image" for s in shown)

    @classmethod
    def from_onebot(cls, event: dict) -> Message:
        return cls(event["message_id"], event["user_id"], event["time"], event["message"])

    def record(self) -> dict:
        """As it waits on disk."""
        return {
            "id": self.id,
            "user": self.user,
            "time": self.time,
            "segments": self.segments,
            "uploads": self.uploads,
        }


def load(path: Path) -> list[Message]:
    """A batch's messages, without those taken back."""
    messages: dict[int, Message] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # Cut short by the machine going down as it was written.
            log.warning("A line in %s is not whole and is left out", path.name)
            continue
        if "recall" in record:
            messages.pop(record["recall"], None)
        else:
            messages[record["id"]] = Message(**record)
    # Pictures copied as a message came may have put it after a later one.
    return sorted(messages.values(), key=lambda m: m.time)


def content(messages: list[Message], body: str) -> dict:
    """The organized Markdown with the pictures in the messages, copied to the forum already:
    where it kept the [图片] the LLM was shown for them, and the rest after it."""
    pictures = iter([f"![]({u})" for m in messages for u in m.uploads])
    body = re.sub(r"\[图片\]", lambda _: next(pictures, ""), body)
    return {"markdown": "\n\n".join([body, *pictures])}


def write(path: Path, records: list[dict]) -> None:
    write_file(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


@dataclass(eq=False)
class Floor:
    post: int
    thread: int
    # None for a floor fetched back from the forum, there to read only.
    author: int | None
    messages: list[Message]
    body: str
    # As the bot left it: a different one means its writer changed it on the forum.
    edited_at: int | None
    question: Question


@dataclass(eq=False)
class Question:
    title: str
    # Whether the question is its thread's opening post, rather than a floor in an older thread
    # that asked the same.
    opening: bool
    # The number of its last related message.
    last: int
    floor: Floor = field(init=False)
    answers: list[Floor] = field(default_factory=list)


class Chat:
    """What is kept of one group."""

    def __init__(self, group: int):
        self.group = group
        self.count = 0
        self.kept: deque[Message] = deque()
        self.by_id: dict[int, Message] = {}
        # Writers are U1, U2… to the models, which never see QQ numbers or names.
        self.aliases: dict[int, str] = {}
        self.following: list[Question] = []

    def alias(self, user: int) -> str:
        return self.aliases.setdefault(user, f"U{len(self.aliases) + 1}")

    def keep(self, m: Message) -> None:
        self.count += 1
        m.number = self.count
        self.kept.append(m)
        self.by_id[m.id] = m

    def forget(self, number: int) -> None:
        """Messages more than KEPT before this one."""
        while self.kept and self.kept[0].number <= number - KEPT:
            self.by_id.pop(self.kept.popleft().id, None)


class Archive:
    def __init__(
        self,
        forum: Forum,
        complete: Complete,
        index: Index,
        *,
        data: Path,
        prefixes: list[str],
        site: str,
        templates: dict[str, str],
        notify: Notify | None = None,
        history: History | None = None,
        tuning: Tuning | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`data` is where messages wait and the summary is kept; `site` is the forum's address
        for links in the summary, `templates` the archive settings with its wording; without
        `notify` none is sent, without `history` messages missed are not looked for."""
        self.forum = forum
        self.site = site.rstrip("/")
        self.templates = templates
        self.notify = notify
        self.history = history
        self.complete = complete
        self.index = index
        self.tuning = tuning or Tuning()
        self.prefixes = [p for p in prefixes if p]
        self.clock = clock
        self.pending = data / "pending"
        self.batches = data / "batch"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.batches.mkdir(exist_ok=True)
        self.summary_file = data / "summary.json"
        # What the next summary in each group names.
        self.summary: dict[int, list[dict]] = {}
        if self.summary_file.exists():
            saved = json.loads(self.summary_file.read_text(encoding="utf-8"))
            self.summary = {int(group): entries for group, entries in saved.items()}
        # Up to when each group's messages were taken in, and when the last summary went: what a
        # restart goes on from.
        self.state_file = data / "archive.json"
        self.state: dict[str, Any] = {"since": {}, "summary": clock()}
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.chats: dict[int, Chat] = {}
        self.boards: list[dict] = []
        self.tags: list[str] = []
        # The day new tags are counted for, and how many were made on it.
        self.tags_day = ""
        self.tags_made = 0
        # The latest floor known of each thread: a floor said earlier is dated no earlier, so
        # the thread reads in order of time.
        self.latest: dict[int, int] = {}
        # When others archive the history side by side (design §8.10): the threads opened in
        # this span are theirs, as they may still be posting there.
        self.elsewhere = (0.0, 0.0)

    # ---- Taking messages in

    def worth(self, m: Message, bot: int) -> bool:
        """Not the bot's own, not a command, and more than stickers and faces."""
        if m.user == bot or plain(m.segments).lstrip().startswith(tuple(self.prefixes)):
            return False
        return any(shows(s) for s in m.segments)

    async def add(self, group: int, m: Message, bot: int) -> bool:
        """Kept on disk for a round to take in, its pictures copied to the forum now: QQ's links
        expire within hours (design §8.1). False for one not worth keeping."""
        if str(group) not in self.state["since"]:
            self.state["since"][str(group)] = self.clock()
            self.save_state()
        if not self.worth(m, bot):
            return False
        for s in m.segments:
            if s["type"] != "image" or is_sticker(s) or not s["data"].get("url"):
                continue
            try:
                picture = {"url": s["data"]["url"]}
                stored = await self.forum.call(
                    "POST", "/uploads/fetch", body=picture, act_as=m.user
                )
            except (ForumError, httpx.HTTPError) as e:
                log.warning("A picture in message %s could not be copied: %s", m.id, e)
                continue
            m.uploads.append(stored["url"])
        self.wait(group, m.record())
        return True

    def recall(self, group: int, message_id: int) -> None:
        """Someone took back their own message: left out unless archived already (design
        §8.7)."""
        self.wait(group, {"recall": message_id})
        chat = self.chats.get(group)
        # In the round being worked on.
        m = chat.by_id.get(message_id) if chat else None
        if m is not None:
            m.recalled = True

    def wait(self, group: int, record: dict) -> None:
        append_line(self.pending / f"{group}.jsonl", json.dumps(record, ensure_ascii=False))

    # ---- Batches

    async def run(self) -> None:
        await asyncio.gather(self.work_batches(), self.send_summaries())

    async def work_batches(self) -> None:
        # A round a restart broke off is gone on with first.
        while True:
            try:
                await self.catch_up()
                self.start_round()
                if any(self.batches.glob("*.jsonl")):
                    await self.ready()
                    async for report in self.batch():
                        # What the LLM wrote stays out of the logs: it is the members' words.
                        tokens = sum(c["input_tokens"] + c["output_tokens"] for c in report["llm"])
                        log.info(
                            "Archive %s of group %s: %d tokens, done %s, dropped %s",
                            report["numbers"],
                            report["group"],
                            tokens,
                            report["done"],
                            report["dropped"],
                        )
            except Exception as e:
                if unavailable(e):
                    log.warning("Archiving waits %d minutes: %r", PAUSE // 60, e)
                    await asyncio.sleep(PAUSE)
                    continue
                log.exception("Archiving failed")
            await asyncio.sleep(ROUND)

    async def send_summaries(self) -> None:
        while True:
            # One missed while the plugin was down goes now.
            if not self.due("summary", (SUMMARY_HOUR,)):
                await self.wait_until((SUMMARY_HOUR,))
            while not await self.send_summary():
                await asyncio.sleep(PAUSE)
            self.state["summary"] = self.clock()
            self.save_state()

    def due(self, what: str, hours: tuple[int, ...]) -> bool:
        """Whether one of the hours came since `what` last happened."""
        last = self.state[what]
        return self.clock() >= last + until(hours, last)

    def save_state(self) -> None:
        write_file(self.state_file, json.dumps(self.state))

    async def catch_up(self) -> None:
        """Messages the plugin didn't hear, while it or AstrBot was down or cut off from NapCat,
        read back from each group's history since its last round took in (design §8.1)."""
        if self.history is None:
            return
        for key, since in self.state["since"].items():
            group = int(key)
            path = self.pending / f"{group}.jsonl"
            kept = load(path) if path.exists() else []
            # By ID, and by who and when as well, should the history's IDs not be the events'.
            heard = {m.id for m in kept} | {(m.user, int(m.time)) for m in kept}
            missed: dict[int, dict] = {}
            before = None
            read = 0
            oldest = self.clock()
            try:
                while True:
                    page = sorted(await self.history(group, before), key=lambda r: r["time"])
                    if not page:
                        break
                    read += len(page)
                    oldest = page[0]["time"]
                    for r in page:
                        said = (r["user_id"], int(r["time"]))
                        if r["time"] > since and not {r["message_id"], said} & heard:
                            missed[r["message_id"]] = r
                    if page[0]["time"] <= since or page[0]["message_id"] == before:
                        break
                    before = page[0]["message_id"]
                    await asyncio.sleep(HISTORY_GAP)
            except Exception:
                # The round goes on with what was heard and what was read back so far.
                log.exception("Reading the history of group %s failed", group)
            added = [
                await self.add(group, Message.from_onebot(r), r["self_id"])
                for r in sorted(missed.values(), key=lambda r: r["time"])
            ]
            log.info(
                "Read back %d messages of group %s to %s, %d of them missed",
                read,
                group,
                datetime.fromtimestamp(oldest).strftime("%m-%d %H:%M"),
                sum(added),
            )

    async def wait_until(self, hours: tuple[int, ...]) -> None:
        """Till the next of these hours o'clock by the wall clock, which goes on while the
        machine sleeps."""
        end = self.clock() + until(hours, self.clock())
        while (left := end - self.clock()) > 0:
            await asyncio.sleep(min(left, 60))

    async def ready(self) -> None:
        """The forum and the embedding model there before the LLM is paid for a segment they
        would fail, with the boards and tags of the day and the thread index."""
        await self.refresh_taxonomy()
        await self.index.load()
        await self.index.embed(["鼠洞"])

    async def refresh_taxonomy(self) -> None:
        boards = (await self.forum.call("GET", "/boards"))["items"]
        self.boards = [b for b in boards if b["post_role"] == "user"]
        self.tags = [t["name"] for t in (await self.forum.call("GET", "/tags"))["items"]]

    def start_round(self, everything: bool = False) -> None:
        """Each group's waiting messages that are due, or all of them, to be archived now; the
        rest and what comes meanwhile wait on. A round not finished stays, in a file of its
        own."""
        stamp = int(self.clock())
        for path in self.pending.glob("*.jsonl"):
            messages = load(path)
            count = len(messages) if everything else to_take(messages, self.clock())
            if not count:
                continue
            # Written before the rest, so that a crash between leaves them twice, not lost:
            # the idempotency keys keep them from being posted twice.
            write(
                self.batches / f"{path.stem}-{stamp}.jsonl", [m.record() for m in messages[:count]]
            )
            if rest := [m.record() for m in messages[count:]]:
                write(path, rest)
            else:
                path.unlink()
            self.state["since"][path.stem] = messages[count - 1].time
        self.save_state()

    async def batch(self) -> AsyncIterator[dict[str, Any]]:
        """Archives the batches there are, a group's after another's and segment by segment,
        yielding each segment's report. What is left of a batch stays on disk till it is
        done."""
        for path in sorted(self.batches.glob("*.jsonl")):
            group = int(path.stem.split("-")[0])
            chat = self.chats.setdefault(group, Chat(group))
            messages = load(path)
            if not messages:
                path.unlink()
                continue
            # The ones kept already, when a batch is tried again, go on with what they were
            # archived into.
            messages = [chat.by_id.get(m.id) or m for m in messages]
            for m in messages:
                if m.number == 0:
                    chat.keep(m)
            done = 0
            todo = segments(messages, self.tuning.segment_messages)
            while todo:
                segment = todo.pop(0)
                report = {
                    "group": group,
                    "numbers": [segment[0].number, segment[-1].number],
                    "times": [segment[0].time, segment[-1].time],
                    "llm": [],
                    "done": [],
                    "dropped": [],
                    # Questions posted as floors in threads that asked them before.
                    "asked_again": [],
                    # What the summary is to name.
                    "summary": [],
                }
                try:
                    await self.work(chat, segment, report)
                except Truncated as e:
                    if len(segment) > 1:
                        # Thought too long over it: its halves, each thought shorter over.
                        report["dropped"].append(f"split: {e}")
                        todo[:0] = segments(segment, (len(segment) + 1) // 2)
                        self.remember(group, report["summary"])
                        yield report
                        continue
                    report["dropped"].append(f"failed: {e!r}")
                except Exception as e:
                    if unavailable(e):
                        # The segment stays in the file, to be tried again.
                        self.remember(group, report["summary"])
                        raise
                    # The segment is lost, and the next goes on (design §8.2).
                    log.exception("Archiving a segment of group %s failed", group)
                    report["dropped"].append(f"failed: {e!r}")
                self.remember(group, report["summary"])
                chat.forget(segment[-1].number)
                done += len(segment)
                rest = [m.record() for m in messages[done:] if not m.recalled]
                if rest:
                    write(path, rest)
                else:
                    path.unlink()
                yield report

    # ---- What the models see

    def text(self, chat: Chat, m: Message) -> str:
        parts = []
        for s in m.segments:
            kind, data = s["type"], s["data"]
            if kind == "text":
                parts.append(data["text"])
            elif kind == "at":
                qq = str(data["qq"])
                parts.append("@全体成员" if qq == "all" else f"@{chat.alias(int(qq))}")
            elif kind == "image":
                parts.append("" if is_sticker(s) else "[图片]")
            elif kind not in ("face", "mface", "reply"):
                parts.append(PLACEHOLDERS.get(kind, "[消息]"))
        return " ".join("".join(parts).split())

    def line(self, chat: Chat, m: Message, names: dict[Floor, str]) -> str:
        parts = [f"#{m.number}", chat.alias(m.user)]
        if m.quoted is not None:
            target = chat.by_id.get(m.quoted)
            parts.append(f"↩#{target.number}" if target else "↩更早的消息")
        parts.append(self.text(chat, m))
        if m.floor is not None:
            parts.append(f"〔已收：{names[m.floor]}〕" if m.floor in names else "〔已收〕")
        elif m.reacted is not None:
            name = names.get(m.reacted)
            parts.append(f"〔已收：回应 {name}〕" if name else "〔已收：回应〕")
        return " ".join(parts)

    def before(self, chat: Chat, first: Message, count: int) -> list[Message]:
        kept = list(chat.kept)
        i = kept.index(first)
        return [m for m in kept[max(0, i - count) : i] if not m.recalled]

    def names(self, chat: Chat) -> tuple[dict[str, Question], dict[str, Floor]]:
        """What Q1, Q2… and A1, A2… stand for in this segment's prompts."""
        questions = {f"Q{i}": q for i, q in enumerate(chat.following, 1)}
        floors: dict[str, Floor] = {ref: q.floor for ref, q in questions.items()}
        answers = [a for q in chat.following for a in q.answers]
        floors |= {f"A{i}": a for i, a in enumerate(answers, 1)}
        return questions, floors

    def floor_text(self, chat: Chat, ref: str, floor: Floor, names: dict[Floor, str]) -> str:
        who = chat.alias(floor.author) if floor.author is not None else "论坛上"
        lines = [f"{ref}（{who}）：{floor.body}"]
        lines += [f"  原消息 {self.line(chat, m, names)}" for m in floor.messages]
        return "\n".join(lines)

    def question_text(self, chat: Chat, ref: str, q: Question, names: dict[Floor, str]) -> str:
        lines = [f"{ref} 标题：{q.title}", self.floor_text(chat, ref, q.floor, names)]
        lines += [self.floor_text(chat, names[a], a, names) for a in q.answers]
        return "\n".join(lines)

    async def ask(self, report: dict, system: str, prompt: str, schema: dict) -> str:
        started = time.monotonic()

        def spent(answer: str, input_tokens: int, output_tokens: int) -> None:
            seconds = round(time.monotonic() - started, 1)
            call = {"output": answer, "input_tokens": input_tokens, "output_tokens": output_tokens}
            report["llm"].append(call | {"seconds": seconds})

        try:
            answer, input_tokens, output_tokens = await self.complete(system, prompt, schema)
        except Truncated as e:
            spent("", e.input_tokens, e.output_tokens)
            raise
        spent(answer, input_tokens, output_tokens)
        return answer

    # ---- A segment

    async def work(self, chat: Chat, segment: list[Message], report: dict) -> None:
        messages = [m for m in segment if not m.recalled]
        if not messages:
            return
        last = messages[-1].number
        follow = self.tuning.follow_messages
        chat.following = [q for q in chat.following if last - q.last <= follow]
        quoted = await self.follow_quotes(chat, messages)
        questions, floors = self.names(chat)
        names = {floor: ref for ref, floor in floors.items()}
        before = self.before(chat, messages[0], ORGANIZE_BEFORE)
        prompt = judge.organize_prompt(
            [self.question_text(chat, ref, q, names) for ref, q in questions.items()],
            [self.line(chat, m, names) for m in before],
            [self.line(chat, m, names) for m in messages],
            self.boards,
            self.tags,
        )
        answer = await self.ask(report, judge.ORGANIZE, prompt, judge.ORGANIZE_SCHEMA)
        try:
            raw = judge.extract_json(answer)
        except ValueError:
            log.warning("The LLM's answer for a segment of group %s is not JSON", chat.group)
            report["dropped"].append("answer is not JSON")
            return
        plan = judge.check(
            raw,
            {m.number: m for m in before + messages},
            questions,
            floors,
            {b["slug"] for b in self.boards},
            set(self.tags),
            chat.aliases,
        )
        report["dropped"] += plan.dropped
        touched = await self.carry_out(chat, plan, questions, floors, report)
        for q in touched | quoted:
            q.last = last

    # ---- The summary

    def remember(self, group: int, entries: list[dict]) -> None:
        """What a segment brought, for the next summary in the group, kept through restarts."""
        if self.notify is None:
            return
        named = self.summary.setdefault(group, [])
        # A segment tried again names again what it posted the first time.
        named += [e for e in entries if e not in named]
        if not named:
            del self.summary[group]
            return
        self.save_summary()

    def save_summary(self) -> None:
        write_file(self.summary_file, json.dumps(self.summary, ensure_ascii=False))

    async def send_summary(self) -> bool:
        """One message in each group that had threads archived since the last (design §8.8).
        Whether all went."""
        sent = True
        for group in list(self.summary):
            entries = self.summary[group]
            # Segments archived while it goes are for the next summary.
            count = len(entries)
            try:
                await self.notify(group, summary_message(entries, self.site, self.templates))
            except Exception:
                log.exception("Sending the archive summary to group %s failed", group)
                sent = False
                continue
            del entries[:count]
            if not entries:
                del self.summary[group]
            self.save_summary()
        return sent

    async def follow_quotes(self, chat: Chat, messages: list[Message]) -> set[Question]:
        """Questions the segment quotes archived messages of, followed again if they had lapsed."""
        found: set[Question] = set()
        for m in messages:
            if m.quoted is None:
                continue
            target = chat.by_id.get(m.quoted)
            if target is not None:
                if target.floor is not None:
                    found.add(target.floor.question)
                continue
            try:
                path = f"/bot/messages/group/{chat.group}/{m.quoted}"
                link = await self.forum.call("GET", path)
            except ForumError as e:
                if e.code != "message_not_linked":
                    raise
                continue
            if not self.theirs(await self.forum.call("GET", f"/threads/{link['thread_id']}")):
                found.add(await self.thread_question(chat, link["thread_id"]))
        chat.following += [q for q in found if q not in chat.following]
        return found

    def theirs(self, thread: dict) -> bool:
        return self.elsewhere[0] <= thread["created_at"] < self.elsewhere[1]

    async def thread_question(self, chat: Chat, thread: int) -> Question:
        """The question a thread asks: the one followed, or its opening and latest answers
        fetched from the forum to go by."""
        for q in chat.following:
            if q.opening and q.floor.thread == thread:
                return q
        detail = await self.forum.call("GET", f"/threads/{thread}")
        self.latest[thread] = max(self.latest.get(thread, 0), detail["last_post_at"])
        path = f"/threads/{thread}/posts"
        first = await self.forum.call("GET", path, params={"after": 0, "body": "markdown"})
        latest = await self.forum.call(
            "GET", path, params={"before": detail["floor_count"] + 1, "body": "markdown"}
        )
        q = Question(detail["title"], opening=True, last=0)

        def fetched(post: dict) -> Floor:
            return Floor(post["id"], thread, None, [], post["body"], post["edited_at"], q)

        q.floor = fetched(first["items"][0])
        q.answers = [fetched(p) for p in latest["items"] if p["floor"] > 1 and not p["deleted"]]
        return q

    async def carry_out(
        self,
        chat: Chat,
        plan: judge.Plan,
        questions: dict[str, Question],
        floors: dict[str, Floor],
        report: dict,
    ) -> set[Question]:
        """Posts what the segment brought; returns the questions it touched."""
        asked: dict[str, Question] = {}
        answered: dict[str, Floor] = {}
        touched: set[Question] = set()

        async def attempt[T](what: str, work: Coroutine[Any, Any, T]) -> T | None:
            try:
                result = await work
            except ForumError as e:
                if unavailable(e):
                    raise
                log.warning("Archiving %s in group %s failed: %s", what, chat.group, e)
                report["dropped"].append(f"{what}: forum {e.code}")
                return None
            if result:
                report["done"].append(what)
            return result

        # A question the same as another new one waits for that one.
        for item in sorted(plan.questions, key=lambda i: (i.same_as or "").startswith("N")):
            same = None
            if item.same_as is not None:
                same = questions.get(item.same_as) or asked.get(item.same_as)
                if same is None:
                    report["dropped"].append(f"new {item.ref}: {item.same_as} was not posted")
                    continue
                touched.add(same)
            q = await attempt(f"new {item.ref}", self.post_question(chat, item, same, report))
            if q is not None:
                asked[item.ref] = q
                touched.add(q)
        # In the order they were said, as the LLM may not list them.
        for item in sorted(plan.answers, key=lambda i: i.messages[0].time):
            question = questions.get(item.question) or asked.get(item.question)
            if question is None:
                report["dropped"].append(f"new {item.ref}: {item.question} was not posted")
                continue
            reply_to = None
            if item.reply_to is not None:
                reply_to = floors.get(item.reply_to) or answered.get(item.reply_to)
            # The LLM may name a floor of another thread, where a floor can't reply.
            if reply_to is not None and reply_to.thread != question.floor.thread:
                reply_to = None
            floor = await attempt(
                f"new {item.ref}", self.post_answer(chat, item, question, reply_to)
            )
            if floor is not None:
                answered[item.ref] = floor
                touched.add(question)
        for edit in plan.edits:
            floor = edit.floor
            messages = floor.messages + edit.messages
            work = self.rewrite(chat, floor, messages, edit.body, edit.title, report)
            if await attempt(f"edit {floor.post}", work):
                touched.add(floor.question)
        for item in plan.reactions:
            what = f"react #{item.message.number}"
            target = floors.get(item.post) or answered.get(item.post)
            if target is None and item.post in asked:
                target = asked[item.post].floor
            if target is None:
                report["dropped"].append(f"{what}: {item.post} was not posted")
            elif target.author == item.message.user:
                report["dropped"].append(f"{what}: their own floor")
            else:
                await attempt(what, self.react(chat, target, item))
        return touched

    # ---- Posting

    def source(self, chat: Chat, messages: list[Message]) -> dict:
        return {
            "chat_type": "group",
            "chat_id": chat.group,
            "message_ids": [m.id for m in messages],
        }

    def settle(
        self, q: Question, post: dict, author: int, messages: list[Message], body: str
    ) -> Floor:
        floor = Floor(post["id"], post["thread_id"], author, messages, body, post["edited_at"], q)
        self.latest[floor.thread] = max(self.latest.get(floor.thread, 0), post["created_at"])
        for m in messages:
            m.floor = floor
        return floor

    async def post_question(
        self, chat: Chat, item: judge.NewQuestion, same: Question | None, report: dict
    ) -> Question:
        author = item.messages[0].user
        how = "same_as"
        if same is None:
            same = await self.duplicate_of(chat, item, report)
            how = "search"
        older = await self.open_thread(same.floor.thread) if same is not None else None
        if same is not None and older is not None:
            # Asked before: a floor in that thread, which later answers reply to.
            q = Question(same.title, opening=False, last=0)
            q.floor = await self.post_floor(chat, q, same.floor.thread, item.messages, item.body)
            # Told as it was asked, by 猜你想问 (design §8.11), not again in the summary.
            report["asked_again"].append({"post": q.floor.post, "how": how})
        else:
            q = Question(item.title, opening=True, last=0)
            key = f"qq-{chat.group}-{item.messages[0].id}-thread"
            try:
                thread = await self.forum.call(
                    "POST",
                    "/threads",
                    body={
                        "board": item.board,
                        "kind": item.kind,
                        "title": item.title,
                        "tags": await self.make_tags(item, report),
                        "content": content(item.messages, item.body),
                        "source": self.source(chat, item.messages),
                        "created_at": int(item.messages[0].time),
                    },
                    act_as=author,
                    key=key,
                )
                posts = await self.forum.call(
                    "GET", f"/threads/{thread['id']}/posts", params={"after": 0}
                )
                opening = posts["items"][0]
                report["summary"].append(
                    {
                        "user": author,
                        "kind": item.kind,
                        "thread": thread["id"],
                        "title": item.title,
                    }
                )
            except ForumError as e:
                if e.code != "source_exists":
                    raise
                # Archived already, before a restart broke the segment off.
                opening = await self.forum.call("GET", f"/posts/{e.details['post_id']}")
            q.floor = self.settle(q, opening, author, item.messages, item.body)
        chat.following.append(q)
        if q.opening:
            # The thread.created event would add it too, but a question asked again in the next
            # segment must find it already.
            await self.index.put(q.floor.thread, item.title, item.body)
        return q

    async def open_thread(self, thread: int) -> dict | None:
        """A thread that takes new floors; None when it was deleted or locked since, and the
        question gets a thread of its own."""
        try:
            detail = await self.forum.call("GET", f"/threads/{thread}")
        except ForumError as e:
            if e.status != 404:
                raise
            return None
        self.latest[thread] = max(self.latest.get(thread, 0), detail["last_post_at"])
        return None if detail["locked"] else detail

    async def make_tags(self, item: judge.NewQuestion, report: dict) -> list[str]:
        """The item's tags, the new ones made on the forum first while the day's allow."""
        today = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        if self.tags_day != today:
            self.tags_day, self.tags_made = today, 0
        tags = list(item.tags)
        for name, kind in item.new_tags:
            if name in self.tags:
                continue
            if self.tags_made >= NEW_TAGS_PER_DAY:
                tags.remove(name)
                report["dropped"].append(f"tag {name}: {NEW_TAGS_PER_DAY} made today already")
                continue
            try:
                await self.forum.call("POST", "/tags", body={"name": name, "kind": kind})
                self.tags_made += 1
                report["done"].append(f"tag {name}")
            except ForumError as e:
                if e.code == "scope_missing":
                    # A token without moderate: the thread goes without the tag.
                    tags.remove(name)
                    report["dropped"].append(f"tag {name}: forum {e.code}")
                    continue
                if e.code != "tag_exists":
                    raise
            self.tags.append(name)
        return tags

    async def post_answer(
        self, chat: Chat, item: judge.NewAnswer, question: Question, reply_to: Floor | None
    ) -> Floor:
        # Answers to a question asked again in an older thread reply to where it was asked.
        if reply_to is None and not question.opening:
            reply_to = question.floor
        floor = await self.post_floor(
            chat, question, question.floor.thread, item.messages, item.body, reply_to
        )
        question.answers.append(floor)
        return floor

    async def post_floor(
        self,
        chat: Chat,
        q: Question,
        thread: int,
        messages: list[Message],
        body: str,
        reply_to: Floor | None = None,
    ) -> Floor:
        author = messages[0].user
        payload = {
            "content": content(messages, body),
            "source": self.source(chat, messages),
            "created_at": max(int(messages[0].time), self.latest.get(thread, 0)),
        }
        if reply_to is not None:
            payload["reply_to_id"] = reply_to.post
        try:
            post = await self.forum.call(
                "POST",
                f"/threads/{thread}/posts",
                body=payload,
                act_as=author,
                key=f"qq-{chat.group}-{messages[0].id}-post",
            )
        except ForumError as e:
            if e.code != "source_exists":
                raise
            post = await self.forum.call("GET", f"/posts/{e.details['post_id']}")
        return self.settle(q, post, author, messages, body)

    async def react(self, chat: Chat, floor: Floor, item: judge.Reaction) -> bool:
        """Their reaction to the floor, and their boost under it, as said in the group."""
        m = item.message
        said = {"source": self.source(chat, [m]), "created_at": int(m.time)}
        path = f"/posts/{floor.post}"
        if item.emoji is not None:
            await self.forum.call(
                "PUT", f"{path}/reaction", body={"emoji": item.emoji} | said, act_as=m.user
            )
        if item.boost is not None:
            await self.forum.call(
                "PUT", f"{path}/boost", body={"body": item.boost} | said, act_as=m.user
            )
        m.reacted = floor
        return True

    async def rewrite(
        self,
        chat: Chat,
        floor: Floor,
        messages: list[Message],
        body: str,
        title: str | None,
        report: dict,
    ) -> bool:
        """Organized anew from all its messages. A floor its writer changed on the forum is
        theirs from then on."""
        current = await self.forum.call("GET", f"/posts/{floor.post}")
        if current["edited_at"] != floor.edited_at:
            report["dropped"].append(f"edit {floor.post}: changed on the forum")
            return False
        q = floor.question
        if title is not None and title != q.title:
            # Before the floor: a new title is an edit of the opening post too.
            path = f"/threads/{floor.thread}"
            try:
                await self.forum.call("PATCH", path, body={"title": title}, act_as=floor.author)
                q.title = title
            except ForumError as e:
                # Who left keeps the title: only what is archived from the group may be written
                # for them, and a title carries no messages.
                if e.code != "member_frozen":
                    raise
        post = await self.forum.call(
            "PATCH",
            f"/posts/{floor.post}",
            body={
                "content": content(messages, body),
                "source": self.source(chat, messages),
            },
            act_as=floor.author,
        )
        for m in floor.messages:
            m.floor = None
        floor.messages, floor.body, floor.edited_at = messages, body, post["edited_at"]
        for m in messages:
            m.floor = floor
        if q.opening and floor is q.floor:
            await self.index.put(floor.thread, q.title, body)
        return True

    async def duplicate_of(
        self, chat: Chat, item: judge.NewQuestion, report: dict
    ) -> Question | None:
        """A thread on the forum that asks the same (design §8.5)."""
        found = []
        for thread in await self.index.candidates(item.title, item.body):
            try:
                t = await self.forum.call("GET", f"/threads/{thread}")
            except ForumError as e:
                if e.status != 404:
                    raise
                await self.index.remove(thread)
                continue
            # A floor asked before the thread's last would go above floors said after it, as
            # when the history is archived after the plugin, or by workers side by side.
            if t["last_post_at"] <= item.messages[0].time and not self.theirs(t):
                found.append(t)
        if not found:
            return None
        for t in found:
            t["created"] = datetime.fromtimestamp(t["created_at"]).strftime("%Y-%m-%d")
        prompt = judge.duplicate_prompt(item.title, item.body, found)
        try:
            answer = await self.ask(report, judge.DUPLICATE, prompt, judge.DUPLICATE_SCHEMA)
            same = judge.extract_json(answer).get("same")
        except (ValueError, AttributeError):
            log.warning("The LLM's answer on duplicates is not a JSON object; taken as none")
            return None
        names = {f"C{i}": t["id"] for i, t in enumerate(found, 1)}
        if same not in names:
            return None
        return await self.thread_question(chat, names[same])


def summary_message(entries: list[dict], site: str, templates: dict[str, str]) -> list[dict]:
    """The summary (design §8.8): each new thread's writer @ed with the thread. `templates`: the
    archive settings, with the summary's, each thread's and the line for the rest."""
    items = [
        fill(
            templates["summary_item"],
            n=i,
            at=[at(e["user"])],
            kind="（讨论）" if e["kind"] == "discussion" else "",
            title=e["title"],
            url=f"{site}/t/{e['thread']}",
            forum=site,
        )
        for i, e in enumerate(entries[:SUMMARY_ITEMS], 1)
    ]
    if len(entries) > SUMMARY_ITEMS:
        items.append(
            fill(templates["summary_more"], count=len(entries) - SUMMARY_ITEMS, forum=site)
        )
    return fill(templates["summary"], count=len(entries), items=joined(items, "\n"), forum=site)
