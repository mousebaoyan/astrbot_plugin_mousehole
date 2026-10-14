"""Auto-archiving (design §8). Group messages gather into rounds; OneJev screens each round and
the LLM organizes what is worth keeping into threads and floors under their writers' names. The
questions stay followed, so later answers and additions find their place."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import judge
from .forum import Forum, ForumError
from .llm import BudgetExhausted
from .similar import Index

log = logging.getLogger("astrbot.mousehole")

# (system prompt, prompt, JSON schema) → (answer, input tokens, output tokens)
Complete = Callable[[str, str, dict], Awaitable[tuple[str, int, int]]]


@dataclass
class Tuning:
    """What replay tunes (docs/replay.md §1), which the plugin's settings may change."""

    # A round goes to the LLM when either OneJev answer reaches this. Low on purpose: a
    # question missed is worse than tokens spent.
    threshold: float = 0.3
    # A round closes at this many messages, or this long after its first.
    round_messages: int = 30
    round_minutes: float = 10
    # A question is followed until this many messages have passed its last related one.
    follow_messages: int = 200

    @classmethod
    def of(cls, config: dict) -> Tuning:
        """From the plugin's settings (_conf_schema.json)."""
        archive = config["archive"]
        return cls(
            threshold=config["onejev"]["threshold"],
            round_messages=archive["round_messages"],
            round_minutes=archive["round_minutes"],
            follow_messages=archive["follow_messages"],
        )


# Messages before a round that the models see too.
SCREEN_BEFORE = 10
ORGANIZE_BEFORE = 20
# Messages kept per group, for context, quotes and recalls.
KEPT = 1000
ONEJEV_RETRY = 60
TICK = 10
TAXONOMY_EVERY = 3600

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


@dataclass(eq=False)
class Message:
    id: int
    user: int
    time: float
    segments: list[dict]
    # Its number in the group, which the models refer to it by.
    number: int = 0
    floor: Floor | None = None
    recalled: bool = False
    # Where its pictures are stored on the forum, once copied there.
    uploads: list[str] | None = None

    @property
    def quoted(self) -> int | None:
        return next((int(s["data"]["id"]) for s in self.segments if s["type"] == "reply"), None)

    @classmethod
    def from_onebot(cls, event: dict) -> Message:
        return cls(event["message_id"], event["user_id"], event["time"], event["message"])


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
        self.pending: list[Message] = []
        self.started = 0.0
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
        if len(self.kept) > KEPT:
            self.by_id.pop(self.kept.popleft().id, None)


@dataclass
class Round:
    chat: Chat
    messages: list[Message]


@dataclass
class Recall:
    group: int
    message_id: int
    user: int
    message: Message | None


class Archive:
    def __init__(
        self,
        forum: Forum,
        onejev: judge.OneJev,
        complete: Complete,
        index: Index,
        *,
        prefixes: list[str],
        tuning: Tuning | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.forum = forum
        self.onejev = onejev
        self.complete = complete
        self.index = index
        self.tuning = tuning or Tuning()
        self.prefixes = [p for p in prefixes if p]
        self.sleep = sleep
        self.clock = clock
        self.chats: dict[int, Chat] = {}
        self.queue: asyncio.Queue[Round | Recall] = asyncio.Queue()
        self.boards: list[dict] = []
        self.tags: list[str] = []
        self.onejev_down = False

    # ---- Taking messages in

    def worth(self, m: Message, bot: int) -> bool:
        """Not the bot's own, not a command, and more than stickers and faces."""
        if m.user == bot or plain(m.segments).lstrip().startswith(tuple(self.prefixes)):
            return False
        return any(shows(s) for s in m.segments)

    def add(self, group: int, m: Message, bot: int) -> None:
        if not self.worth(m, bot):
            return
        chat = self.chats.setdefault(group, Chat(group))
        chat.keep(m)
        if not chat.pending:
            chat.started = m.time
        chat.pending.append(m)
        if len(chat.pending) >= self.tuning.round_messages:
            self.close(chat)

    def tick(self, now: float) -> None:
        for chat in self.chats.values():
            if chat.pending and now - chat.started >= self.tuning.round_minutes * 60:
                self.close(chat)

    def close(self, chat: Chat) -> None:
        self.queue.put_nowait(Round(chat, chat.pending))
        chat.pending = []

    def recall(self, group: int, message_id: int, user: int) -> None:
        """Someone took back their own message."""
        chat = self.chats.get(group)
        m = chat.by_id.get(message_id) if chat else None
        if m is not None:
            m.recalled = True
            if m in chat.pending:
                chat.pending.remove(m)
                return
        # After whatever round is being worked on, which may archive it yet.
        self.queue.put_nowait(Recall(group, message_id, user, m))

    async def refresh_taxonomy(self) -> None:
        boards = (await self.forum.call("GET", "/boards"))["items"]
        self.boards = [b for b in boards if b["post_role"] == "user"]
        self.tags = [t["name"] for t in (await self.forum.call("GET", "/tags"))["items"]]

    async def run(self) -> None:
        refreshed = -TAXONOMY_EVERY
        while True:
            if self.clock() - refreshed >= TAXONOMY_EVERY:
                try:
                    await self.refresh_taxonomy()
                    refreshed = self.clock()
                except Exception:
                    log.exception("Reading the boards and tags failed")
            try:
                job = await asyncio.wait_for(self.queue.get(), TICK)
            except TimeoutError:
                self.tick(self.clock())
                continue
            try:
                report = await self.handle(job)
                # What the LLM wrote stays out of the logs: it is the members' words.
                tokens = sum(c["input_tokens"] + c["output_tokens"] for c in report["llm"])
                log.info(
                    "Archive %s: scores %s, %d tokens, done %s, dropped %s",
                    report.get("numbers") or f"recall of {report['recall']}",
                    report.get("scores"),
                    tokens,
                    report["done"],
                    report["dropped"],
                )
            except BudgetExhausted as e:
                log.warning("Archiving skips a round: the LLM's daily tokens are used up (%s)", e)
            except Exception:
                log.exception("Archiving failed")
            self.tick(self.clock())

    async def handle(self, job: Round | Recall) -> dict[str, Any]:
        if isinstance(job, Recall):
            return await self.take_back(job)
        return await self.work(job)

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
        return " ".join(parts)

    def before(self, chat: Chat, first: Message, count: int) -> list[Message]:
        kept = list(chat.kept)
        i = kept.index(first)
        return [m for m in kept[max(0, i - count) : i] if not m.recalled]

    def names(self, chat: Chat) -> tuple[dict[str, Question], dict[str, Floor]]:
        """What Q1, Q2… and A1, A2… stand for in this round's prompts."""
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
        answer, input_tokens, output_tokens = await self.complete(system, prompt, schema)
        report["llm"].append(
            {
                "output": answer,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        return answer

    # ---- A round

    async def work(self, r: Round) -> dict[str, Any]:
        chat = r.chat
        messages = [m for m in r.messages if not m.recalled]
        report: dict[str, Any] = {
            "group": chat.group,
            "numbers": [r.messages[0].number, r.messages[-1].number],
            "times": [r.messages[0].time, r.messages[-1].time],
            "scores": None,
            "llm": [],
            "done": [],
            "dropped": [],
            # Questions posted as floors in threads that asked them before.
            "asked_again": [],
        }
        if not messages:
            return report
        last = messages[-1].number
        follow = self.tuning.follow_messages
        chat.following = [q for q in chat.following if last - q.last <= follow]
        quoted = await self.follow_quotes(chat, messages)
        questions, floors = self.names(chat)
        names = {floor: ref for ref, floor in floors.items()}
        if not quoted:
            scores = await self.screen(chat, messages, names, report)
            if scores is None or max(scores.values()) < self.tuning.threshold:
                return report
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
            log.warning("The LLM's answer for a round of group %s is not JSON", chat.group)
            report["dropped"].append("answer is not JSON")
            return report
        plan = judge.check(
            raw,
            {m.number: m for m in before + messages},
            questions,
            floors,
            {b["slug"] for b in self.boards},
            set(self.tags),
        )
        report["dropped"] += plan.dropped
        touched = await self.carry_out(chat, plan, questions, floors, report)
        for q in touched | quoted:
            q.last = last
        return report

    async def follow_quotes(self, chat: Chat, messages: list[Message]) -> set[Question]:
        """Questions the round quotes archived messages of, followed again if they had lapsed.
        Such a round skips OneJev: it is about something worth keeping already."""
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
            found.add(await self.thread_question(chat, link["thread_id"]))
        chat.following += [q for q in found if q not in chat.following]
        return found

    async def thread_question(self, chat: Chat, thread: int) -> Question:
        """The question a thread asks: the one followed, or its opening and latest answers
        fetched from the forum to go by."""
        for q in chat.following:
            if q.opening and q.floor.thread == thread:
                return q
        detail = await self.forum.call("GET", f"/threads/{thread}")
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

    async def screen(
        self, chat: Chat, messages: list[Message], names: dict[Floor, str], report: dict
    ) -> dict[str, float] | None:
        """OneJev's answers; while it can't be reached the round waits, rather than going to
        the LLM and costing tokens. None when it won't take the round."""
        before = [self.line(chat, m, names) for m in self.before(chat, messages[0], SCREEN_BEFORE)]
        lines = [self.line(chat, m, names) for m in messages]
        titles = [q.title for q in chat.following]
        started = time.monotonic()
        while True:
            try:
                scores = await self.onejev.screen(before, lines, titles)
                break
            except judge.OneJevRejected as e:
                log.warning("OneJev refused a round of group %s: %s", chat.group, e)
                report["dropped"].append(f"OneJev refused: {e}")
                return None
            except judge.OneJevDown as e:
                if not self.onejev_down:
                    log.warning("OneJev can't be reached, rounds wait: %s", e)
                    self.onejev_down = True
                await self.sleep(ONEJEV_RETRY)
        if self.onejev_down:
            log.info("OneJev is back")
            self.onejev_down = False
        report["scores"] = scores
        report["onejev_seconds"] = round(time.monotonic() - started, 1)
        return scores

    async def carry_out(
        self,
        chat: Chat,
        plan: judge.Plan,
        questions: dict[str, Question],
        floors: dict[str, Floor],
        report: dict,
    ) -> set[Question]:
        """Posts what the round brought; returns the questions it touched."""
        asked: dict[str, Question] = {}
        answered: dict[str, Floor] = {}
        touched: set[Question] = set()

        async def attempt[T](what: str, work: Coroutine[Any, Any, T]) -> T | None:
            try:
                result = await work
            except ForumError as e:
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
        for item in plan.answers:
            question = questions.get(item.question) or asked.get(item.question)
            if question is None:
                report["dropped"].append(f"new {item.ref}: {item.question} was not posted")
                continue
            reply_to = None
            if item.reply_to is not None:
                reply_to = floors.get(item.reply_to) or answered.get(item.reply_to)
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
        return touched

    # ---- Posting

    def source(self, chat: Chat, messages: list[Message]) -> dict:
        return {
            "chat_type": "group",
            "chat_id": chat.group,
            "message_ids": [m.id for m in messages],
        }

    async def content(self, author: int, messages: list[Message], body: str) -> dict:
        """The organized text, then the pictures in the messages. Each picture is copied to the
        forum once: QQ's links expire within hours, and a floor may be rewritten long after."""
        segments = [{"type": "text", "data": {"text": body}}]
        for m in messages:
            if m.uploads is None:
                m.uploads = []
                for s in m.segments:
                    if s["type"] != "image" or is_sticker(s) or not s["data"].get("url"):
                        continue
                    try:
                        picture = {"url": s["data"]["url"]}
                        stored = await self.forum.call(
                            "POST", "/uploads/fetch", body=picture, act_as=author
                        )
                    except ForumError as e:
                        if e.code != "fetch_failed":
                            raise
                        log.warning("A picture in message %s could not be copied", m.id)
                        continue
                    m.uploads.append(stored["url"])
            segments += [{"type": "image", "data": {"url": url}} for url in m.uploads]
        return {"onebot": segments}

    def settle(
        self, q: Question, post: dict, author: int, messages: list[Message], body: str
    ) -> Floor:
        floor = Floor(post["id"], post["thread_id"], author, messages, body, post["edited_at"], q)
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
        if same is not None:
            # Asked before: a floor in that thread, which later answers reply to.
            q = Question(same.title, opening=False, last=0)
            q.floor = await self.post_floor(chat, q, same.floor.thread, item.messages, item.body)
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
                        "kind": "question",
                        "title": item.title,
                        "tags": item.tags,
                        "content": await self.content(author, item.messages, item.body),
                        "source": self.source(chat, item.messages),
                    },
                    act_as=author,
                    key=key,
                )
                posts = await self.forum.call(
                    "GET", f"/threads/{thread['id']}/posts", params={"after": 0}
                )
                opening = posts["items"][0]
            except ForumError as e:
                if e.code != "source_exists":
                    raise
                # Archived already, before the plugin restarted.
                opening = await self.forum.call("GET", f"/posts/{e.details['post_id']}")
            q.floor = self.settle(q, opening, author, item.messages, item.body)
        chat.following.append(q)
        if q.opening:
            # The thread.created event would add it too, but a question asked again in the next
            # round must find it already.
            await self.index.put(q.floor.thread, item.title, item.body)
        return q

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
            "content": await self.content(author, messages, body),
            "source": self.source(chat, messages),
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
            await self.forum.call("PATCH", path, body={"title": title}, act_as=floor.author)
            q.title = title
        post = await self.forum.call(
            "PATCH",
            f"/posts/{floor.post}",
            body={
                "content": await self.content(floor.author, messages, body),
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
                found.append(await self.forum.call("GET", f"/threads/{thread}"))
            except ForumError as e:
                if e.status != 404:
                    raise
                self.index.remove(thread)
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

    # ---- Recalls

    async def take_back(self, job: Recall) -> dict[str, Any]:
        """A recalled message leaves its floor: the rest organized anew, or the floor deleted
        when nothing else of it is at hand (design §8.7)."""
        report: dict[str, Any] = {"recall": job.message_id, "llm": [], "done": [], "dropped": []}
        m = job.message
        if m is None:
            try:
                path = f"/bot/messages/group/{job.group}/{job.message_id}"
                link = await self.forum.call("GET", path)
            except ForumError as e:
                if e.code != "message_not_linked":
                    raise
                return report
            post = await self.forum.call("GET", f"/posts/{link['post_id']}")
            await self.delete(link["thread_id"], post["id"], post["floor"] == 1, job.user, report)
            return report
        floor = m.floor
        if floor is None:
            return report
        chat = self.chats[job.group]
        q = floor.question
        rest = [x for x in floor.messages if x is not m]
        if not rest:
            opening = q.opening and floor is q.floor
            await self.delete(floor.thread, floor.post, opening, job.user, report)
            m.floor = None
            if floor is q.floor:
                chat.following = [x for x in chat.following if x is not q]
            else:
                q.answers.remove(floor)
            return report
        lines = "\n".join(self.text(chat, x) for x in rest)
        kind = "提问" if floor is q.floor else "回答"
        prompt = f"这一层是{kind}。剩下的消息：\n{lines}"
        answer = await self.ask(report, judge.REORGANIZE, prompt, judge.REORGANIZE_SCHEMA)
        try:
            raw = judge.extract_json(answer)
            body = raw["body"].strip()
        except (ValueError, KeyError, AttributeError, TypeError):
            log.warning("The LLM's rewrite after a recall is unusable; floor %s kept", floor.post)
            report["dropped"].append("rewrite unusable")
            return report
        title = judge.title_of(raw.get("title")) if q.opening and floor is q.floor else None
        if await self.rewrite(chat, floor, rest, body, title, report):
            report["done"].append(f"rewrote {floor.post}")
        m.floor = None
        return report

    async def delete(self, thread: int, post: int, opening: bool, user: int, report: dict) -> None:
        path = f"/threads/{thread}" if opening else f"/posts/{post}"
        try:
            await self.forum.call("DELETE", path, act_as=user)
        except ForumError as e:
            if e.status != 403:
                raise
            # A thread with replies stays; so does what its writer may no longer touch.
            report["dropped"].append(f"delete {path}: {e.code}")
            return
        if opening:
            self.index.remove(thread)
        report["done"].append(f"deleted {path}")
