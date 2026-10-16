"""Replays an exported group chat through auto-archiving, offline (docs/replay.md).

uv run python -m tools.replay data/export.json --config data/config.json --out replay-out/a
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import aclosing
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from mousehole import judge
from mousehole.archive import BATCH_HOURS, SUMMARY_HOUR, Archive, Message, Tuning, until
from mousehole.llm import LLM, Embedder, Settings, read
from mousehole.similar import Index
from tools.fake_forum import FakeForum, Thread, bigrams

FREE_BOARD = {
    "slug": "free",
    "name": "未分类",
    "description": "所有帖子都选这个",
    "post_role": "user",
}
# ECNU's credits per million tokens of ecnu-plus, doubled from 8 to 18 o'clock.
CREDITS_IN, CREDITS_CACHED, CREDITS_OUT = 100, 20, 400
DEARER_HOURS = range(8, 18)
SITE = "https://bbs.mousebaoyan.cn"

CATEGORIZE = """给论坛上的这个帖子（提问或讨论）写一个类别和几个标签，用来设计论坛的版块和标签。
- category：不超过 6 个字，说明它属于哪类问题，比如“夏令营”“导师联系”“选校定位”。
- tags：最多 5 个，可以是话题、院校或方向。
只输出一个 JSON 对象：{"category": "……", "tags": ["……"]}"""
CATEGORIZE_SCHEMA = {
    "name": "category",
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["category", "tags"],
        "additionalProperties": False,
    },
}
# Titles sharing this much are listed as maybe the same question.
SIMILAR_TITLES = 0.3


# ---- The export


def convert(export: dict) -> tuple[int, int, list[tuple[Message, str, str]]]:
    """The group, the exporting account (taken for the bot), and each message with its sender's
    card and text as exported. Recalled messages lost their text, and system ones say nothing.
    Messages go by QQ's message IDs, which the forum keeps apart from those the plugin sees."""
    info = export["chatInfo"]
    ids = {m["id"] for m in export["messages"]}
    messages = []
    for m in export["messages"]:
        if m["system"] or m["recalled"]:
            continue
        segments = []
        for e in m["content"]["elements"]:
            kind, data = e["type"], e.get("data") or {}
            if kind == "text":
                segments.append({"type": "text", "data": {"text": data["text"]}})
            elif kind == "at":
                segments.append({"type": "at", "data": {"qq": data.get("uin") or "all"}})
            elif kind == "reply":
                quoted = data.get("referencedMessageId")
                if quoted in ids:
                    segments.append({"type": "reply", "data": {"id": quoted}})
            elif kind == "image":
                sticker = data.get("subType") == "sticker"
                # The picture itself is where the export keeps it, by its MD5.
                image = {"sub_type": 1 if sticker else 0, "md5": data.get("md5")}
                segments.append({"type": "image", "data": image})
            else:
                segments.append({"type": {"market_face": "mface"}.get(kind, kind), "data": {}})
        message = Message(int(m["id"]), int(m["sender"]["uin"]), m["timestamp"] / 1000, segments)
        messages.append((message, m["sender"]["name"], m["content"]["text"]))
    return int(info["peerUid"]), int(info["selfUin"]), messages


# ---- The models


def credits(input_tokens: int, output_tokens: int, hour: int, cached: int = 0) -> float:
    """What a call costs on ECNU at that hour of the day, `cached` of its input from the cache."""
    total = (input_tokens - cached) * CREDITS_IN + cached * CREDITS_CACHED
    return (total + output_tokens * CREDITS_OUT) / 1e6 * (2 if hour in DEARER_HOURS else 1)


class Cached(httpx.AsyncBaseTransport):
    """The LLM's answers kept by the whole request, shared by all runs: a run stopped halfway and
    started again, or a longer one over the same start, pays only for the calls not made before.
    `spent` is what the calls not found here cost."""

    def __init__(self, path: Path):
        self.path = path
        self.real = httpx.AsyncHTTPTransport()
        self.answers: dict[str, str] = {}
        self.hits = 0
        self.spent = 0.0
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                self.answers[entry["request"]] = entry["response"]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request.content.decode()
        if key in self.answers:
            self.hits += 1
        else:
            res = await self.real.handle_async_request(request)
            content = await res.aread()
            if res.status_code != 200:
                return httpx.Response(res.status_code, content=content)
            _, usage, finish = await read(httpx.Response(200, content=content))
            if usage:
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                hour = datetime.now().hour
                prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
                self.spent += credits(prompt, completion, hour, cached)
            if finish is None:
                # Broken off: asked again, not kept.
                return httpx.Response(200, content=content)
            self.answers[key] = content.decode()
            line = json.dumps({"request": key, "response": self.answers[key]}) + "\n"
            # One write, so runs going at once don't mix their lines.
            with self.path.open("ab", buffering=0) as f:
                f.write(line.encode())
        return httpx.Response(200, text=self.answers[key])


# ---- The run


def moment(value: str) -> float:
    """`YYYY-MM-DD` for the start of the day, or `YYYY-MM-DD HH:MM`."""
    return datetime.fromisoformat(value).timestamp()


def up_to(value: str) -> float:
    """`YYYY-MM-DD` up to the end of that day, or `YYYY-MM-DD HH:MM` up to then."""
    end = datetime.fromisoformat(value)
    return (end + timedelta(days=1) if len(value) == len("YYYY-MM-DD") else end).timestamp()


async def replay(args: argparse.Namespace) -> None:
    export = json.loads(Path(args.export).read_text(encoding="utf-8"))
    group, bot, messages = convert(export)
    start = moment(args.start) if args.start else float("-inf")
    end = up_to(args.until) if args.until else float("inf")
    messages = [m for m in messages if start <= m[0].time < end]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.boards:
        taxonomy = json.loads(Path(args.boards).read_text(encoding="utf-8"))
        boards = [b | {"post_role": "user"} for b in taxonomy["boards"]]
        tags = [t["name"] for t in taxonomy["tags"]]
    else:
        boards, tags = [FREE_BOARD], []
    now = 0.0
    fake = FakeForum(boards, tags, clock=lambda: now)
    forum = fake.client()
    # The plugin's own settings, so replay asks the models just as it will.
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    cache = Cached(out.parent / "llm-cache.jsonl")
    # Replay's days aren't today's: no daily limit.
    llm = LLM(Settings.of(config["llm"] | {"tokens_per_day": 0}), cache)
    e = config["embedding"]
    embedder = Embedder(e["base_url"], e["api_key"], e["model"])
    tuning = Tuning.of(config)

    # Each summary that would have gone: (when, group, segments).
    sent: list[tuple[float, int, list[dict]]] = []

    async def told(group: int, segments: list[dict]) -> None:
        sent.append((now, group, segments))

    index = Index(forum, embedder.embed, e["model"], None, e["min_similarity"])
    data = tempfile.TemporaryDirectory()
    archive = Archive(
        forum,
        llm.complete,
        index,
        data=Path(data.name),
        prefixes=["/"],
        site=SITE,
        notify=told,
        tuning=tuning,
        clock=lambda: now,
    )
    await archive.refresh_taxonomy()
    reports: list[dict] = []
    # Written as each segment ends: a run stopped halfway keeps what it has paid for.
    segments_file = out / "segments.jsonl"
    segments_file.write_text("", encoding="utf-8")

    shown = {m.id: (m, name, text) for m, name, text in messages}

    def enough() -> bool:
        return args.segments is not None and len(reports) >= args.segments

    def write() -> None:
        """What the segments so far came to: written after each, to read while a run goes
        on."""
        (out / "threads.md").write_text(threads_md(fake, shown, reports), encoding="utf-8")
        # Of the segments run: a batch's messages are all kept as it starts.
        kept = sum(r["numbers"][1] - r["numbers"][0] + 1 for r in reports)
        summary = summary_md(fake, shown, kept, reports, sent, tuning, cache)
        (out / "summary.md").write_text(summary, encoding="utf-8")

    async def batch() -> None:
        archive.start_batch()
        async with aclosing(archive.batch()) as segments:
            async for report in segments:
                reports.append(report)
                with segments_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(report, ensure_ascii=False) + "\n")
                write()
                print(
                    f"segment {len(reports)}: #{report['numbers'][0]}–#{report['numbers'][1]}"
                    f" done={len(report['done'])} dropped={len(report['dropped'])}"
                    f" spent={cache.spent:.1f} credits",
                    file=sys.stderr,
                )
                if enough():
                    return

    # Batches and summaries at the hours the messages came, as the plugin would have.
    if messages:
        first = messages[0][0].time
        next_batch = first + until(BATCH_HOURS, first)
        next_summary = first + until((SUMMARY_HOUR,), first)
    for m, _, _ in messages:
        if enough():
            break
        while min(next_batch, next_summary) <= m.time and not enough():
            if next_batch < next_summary:
                now = next_batch
                await batch()
                next_batch += until(BATCH_HOURS, next_batch)
            else:
                now = next_summary
                await archive.send_summary()
                next_summary += until((SUMMARY_HOUR,), next_summary)
        now = m.time
        await archive.add(group, m, bot)
    if not enough():
        await batch()
    # What the batches so far would have told, though the morning hadn't come.
    await archive.send_summary()
    data.cleanup()

    write()
    if not args.boards:
        categories = await categories_md(fake, llm)
        (out / "categories.md").write_text(categories, encoding="utf-8")
    print(
        f"done: {len(fake.threads)} threads, {len(reports)} segments, {len(sent)} summaries,"
        f" {cache.hits} calls cached, {cache.spent:.1f} credits spent → {out}",
        file=sys.stderr,
    )


# ---- What it writes


def clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def threads_md(fake: FakeForum, shown: dict, reports: list[dict]) -> str:
    again = {a["post"]: a["how"] for r in reports for a in r.get("asked_again", [])}
    names = {m.user: name for m, name, _ in shown.values()}
    told = defaultdict(list)
    for r in reports:
        for x in r["summary"]:
            told[x["thread"]].append(f"@{names[x['user']]}")
    lines = ["# 本来会发的帖子", ""]
    for n, t in enumerate(sorted(fake.threads.values(), key=lambda t: t.created_at), 1):
        state = "（已删除）" if t.deleted else ""
        kind = "讨论" if t.kind == "discussion" else "问答"
        lines += [
            f"## {n}. {t.title}{state}",
            f"{kind}，版块 {t.board}，标签 {'、'.join(t.tags) or '无'}",
            f"汇总里：{'、'.join(told[t.id]) or '无'}",
            "",
        ]
        for p in t.posts:
            m0 = shown.get(p.sources[0]) if p.sources else None
            who = m0[1] if m0 else "?"
            notes = []
            if p.id in again:
                notes.append(f"并进来的提问（{again[p.id]}）")
            if p.reply_to:
                notes.append(f"回复 {fake.posts[p.reply_to].floor} 楼")
            if p.deleted:
                notes.append("已删除")
            note = f"（{'，'.join(notes)}）" if notes else ""
            lines += [f"### {p.floor} 楼 · {who}{note}", "", p.body, ""]
            if p.reactions:
                said = "、".join(f"{names.get(u, '?')} {e}" for u, e in p.reactions.items())
                lines += [f"回应：{said}", ""]
            for u, boost in p.boosts.items():
                lines += [f"短评 · {names.get(u, '?')}：{boost}", ""]
            for i, old in enumerate(p.revisions, 1):
                lines.append(f"> 第 {i} 版：{old}")
            if p.revisions:
                lines.append("")
            lines.append("原消息：")
            for source in p.sources:
                m, name, text = shown[source]
                lines.append(f"- {clock(m.time)} {name}：{text}")
            lines.append("")
    return "\n".join(lines)


def reason(text: str) -> str:
    """A dropped item's reason without its particulars, to count alike ones together."""
    return re.sub(r"\d+", "n", re.sub(r"\b[NQAC]\d+\b", "X", text))


def summary_md(
    fake: FakeForum,
    shown: dict,
    kept: int,
    reports: list[dict],
    sent: list[tuple[float, int, list[dict]]],
    tuning: Tuning,
    cache: Cached,
) -> str:
    # The stretch the segments cover, so a run of a few segments isn't taken for a day.
    span = (reports[-1]["times"][1] - reports[0]["times"][0]) / 86400 if reports else 0
    days = max(span, 1 / 24)
    organized = [r for r in reports if r["llm"]]
    calls = [c for r in reports for c in r["llm"]]
    posts = [p for t in fake.threads.values() for p in t.posts]
    again = Counter(a["how"] for r in reports for a in r.get("asked_again", []))
    dropped = Counter(reason(d) for r in reports for d in r["dropped"])
    named = sum(len(r["summary"]) for r in reports)
    # At the night price the batches run at, with nothing cached: replay's segments go one after
    # another as the plugin's do, but a run may take its answers from llm-cache.jsonl.
    cost = round(sum(credits(c["input_tokens"], c["output_tokens"], 0) for c in calls), 1)

    rows = [
        ("有效消息（去掉表情、命令和 bot 的）", kept),
        ("段", len(reports)),
        ("交给大模型的段", len(organized)),
        ("大模型调用", len(calls)),
        ("输入 token", sum(c["input_tokens"] for c in calls)),
        ("输出 token", sum(c["output_tokens"] for c in calls)),
        ("大模型耗时（秒）", round(sum(c["seconds"] for c in calls))),
        ("ECNU 积分（夜间价，不算缓存）", cost),
        ("新帖", len(fake.threads)),
        ("回复", sum(p.floor > 1 for p in posts) - sum(again.values())),
        ("更新", sum(len(p.revisions) for p in posts)),
        ("same_as 并进旧帖", again["same_as"]),
        ("查重并进旧帖", again["search"]),
        ("讨论帖", sum(t.kind == "discussion" for t in fake.threads.values())),
        ("汇总（条）", len(sent)),
        ("汇总里写到的帖子", named),
    ]
    lines = [
        f"# 回放结果（{days:.2f} 天的消息）",
        "",
        "| 项 | 总数 | 每天 |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {name} | {value} | {round(value / days, 1)} |" for name, value in rows]
    lines += ["", "## 丢掉的项", "", "| 原因 | 次数 |", "| --- | --- |"]
    lines += [f"| {r} | {n} |" for r, n in dropped.most_common()]
    lines += ["", "## 疑似重复的帖子", ""] + suspects(fake, shown, tuning.follow_messages)
    names = {m.user: name for m, name, _ in shown.values()}
    for t, _, segments in sent:
        text = "".join(
            s["data"]["text"] if s["type"] == "text" else f"@{names[int(s['data']['qq'])]}"
            for s in segments
        )
        lines += ["", f"## {clock(t)} 的汇总", "", "```text", text, "```"]
    lines += [
        "",
        f"这次跑实际花了 {cache.spent:.1f} 积分（按跑的时候的价格），"
        f"{cache.hits} 次调用是之前跑过的，从 llm-cache.jsonl 里拿，没花钱。",
    ]
    return "\n".join(lines) + "\n"


def suspects(fake: FakeForum, shown: dict, follow: int) -> list[str]:
    """Threads with titles much alike, and threads their asker opened while a question of theirs
    was still followed."""
    threads = sorted(
        (t for t in fake.threads.values() if not t.deleted), key=lambda t: t.created_at
    )
    found = []
    for i, t in enumerate(threads):
        for older in threads[:i]:
            a, b = bigrams(t.title), bigrams(older.title)
            if a and b and len(a & b) / len(a | b) >= SIMILAR_TITLES:
                found.append(f"- 标题相近：「{older.title}」和「{t.title}」")
            if older.author == t.author and following(older, t, shown, follow):
                found.append(f"- 同一个人还在跟进时又开帖：「{older.title}」和「{t.title}」")
    return found or ["（没有）"]


def following(older: Thread, newer: Thread, shown: dict, follow: int) -> bool:
    last = max(shown[s][0].number for p in older.posts for s in p.sources)
    first = min(shown[s][0].number for s in newer.posts[0].sources)
    return 0 < first - last <= follow


async def categories_md(fake: FakeForum, llm: LLM) -> str:
    categories: dict[str, list[str]] = defaultdict(list)
    tags: Counter[str] = Counter()
    used_in = used_out = 0
    for t in fake.threads.values():
        prompt = f"标题：{t.title}\n{t.posts[0].body}"
        answer, input_tokens, output_tokens = await llm.complete(
            CATEGORIZE, prompt, CATEGORIZE_SCHEMA
        )
        used_in += input_tokens
        used_out += output_tokens
        try:
            raw = judge.extract_json(answer)
            categories[str(raw["category"])].append(t.title)
            tags.update(str(tag) for tag in raw.get("tags") or [])
        except (ValueError, KeyError, TypeError):
            categories["（没分出来）"].append(t.title)
    lines = ["# 类别", "", "| 类别 | 问题数 | 例子 |", "| --- | --- | --- |"]
    for name, titles in sorted(categories.items(), key=lambda c: -len(c[1])):
        lines.append(f"| {name} | {len(titles)} | {'；'.join(titles[:5])} |")
    lines += ["", "# 标签", "", "| 标签 | 次数 |", "| --- | --- |"]
    lines += [f"| {tag} | {n} |" for tag, n in tags.most_common()]
    # Not in summary.md, which is written before these calls.
    lines += [
        "",
        f"分类调用大模型 {len(fake.threads)} 次，输入 {used_in} token，输出 {used_out} token。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay an exported group chat (docs/replay.md)")
    parser.add_argument("export", help="QQChatExporter 导出的 JSON")
    parser.add_argument(
        "--config", required=True, help="插件的配置文件（AstrBot 的 data/config 下）"
    )
    parser.add_argument("--out", required=True, help="结果放在哪个目录")
    parser.add_argument("--boards", help="版块和标签的 JSON；不给就是自由分类")
    parser.add_argument(
        "--from", dest="start", help="从这天开始（YYYY-MM-DD），或者从这一刻（YYYY-MM-DD HH:MM）"
    )
    parser.add_argument(
        "--until", help="到这天为止，含这天（YYYY-MM-DD），或者到这一刻（YYYY-MM-DD HH:MM）"
    )
    parser.add_argument("--segments", type=int, help="跑满这么多段就停，先小范围试时用")
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
