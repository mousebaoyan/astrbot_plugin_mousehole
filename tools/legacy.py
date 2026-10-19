"""The group's history from before the exports the bot made (design §8.10), as a member's
QQChatExporter gave it: a txt up to 2026-07-31, its writers by the names they had when it was
exported and no QQ numbers; and an html for each day after, with them. Turned into an export
like QQChatExporter's JSON, which tools.history takes. Its pictures weren't kept.

uv run python -m tools.legacy TXT HTML... --later EXPORT.json... --config <配置> --out legacy.json
"""

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from mousehole.forum import Forum, ForumError

# Far above the seq of any message the group had, so the IDs these get (tools.replay's
# HISTORY_IDS plus seq) can't meet those of the exports the bot made.
LEGACY_SEQ = 2**39
# How far back a quote is looked for.
QUOTED_WITHIN = 7 * 86400
# Before joining the group as the forum knows it, by more than this, is someone else's.
JOINED_SLACK = 86400
LOST = "[图片已丢失]"
# The shortest name an @ is taken for.
MENTION_LEAST = 3

LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*?): (.*)$")
# A quote of a face has brackets in brackets.
REPLY = re.compile(r"^\[回复：((?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*)\]")
# What the txt writes for what isn't text, a card's or a face's name in brackets as well.
PLACEHOLDER = re.compile(
    r"\[(表情|图片|语音|视频|文件|合并转发|卡片消息)：(?:\[[^\]]*\]|[^\]])*\]"
    r"|\[(动画表情|红包/钱包)\]|\[/[^\[\]]{1,20}\]|\[CQ:at,qq=(\d+)\]"
)
KINDS = {
    "语音": "record",
    "视频": "video",
    "文件": "file",
    "合并转发": "forward",
    "红包/钱包": "json",
}
CHUNK = re.compile(r'__QCE_CHUNK__\(\{id:"c\d+",messages:\[')
SENDER = re.compile(r'nk=(\d+)[^"]*" alt="([^"]*)"')


def text(t: str) -> dict:
    return {"type": "text", "data": {"text": t}}


def bare(t: str) -> str:
    return "".join(t.split())


# ---- The txt


def read_txt(path: Path) -> list[dict]:
    """Its messages: time, writer's name and text, the lines after the first going with it."""
    found: list[dict] = []
    body = False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not body:
            body = line.startswith("=====")
            continue
        if m := LINE.match(line):
            time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            found.append({"time": time, "name": m.group(2), "text": m.group(3)})
        elif found:
            found[-1]["text"] += "\n" + line
    # A few lines are there twice.
    once = {(r["time"], r["name"], r["text"]): r for r in found}
    return list(once.values())


# ---- The html


class Content(HTMLParser):
    """A message's segments, from its html."""

    SEGMENTS = {
        "native-face-wrap": {"type": "face", "data": {}},
        "sticker-wrap": {"type": "image", "data": {"subType": "sticker"}},
        "image-content": text(LOST),
        "json-card": {"type": "json", "data": {}},
        "forward-card": {"type": "forward", "data": {}},
        "video-bubble": {"type": "video", "data": {}},
        "message-file": {"type": "file", "data": {}},
    }
    VOID = ("img", "br", "source", "input", "hr", "wbr", "meta")

    def __init__(self) -> None:
        super().__init__()
        self.segments: list[dict] = []
        self.reply: str | None = None
        # What each open tag holds: text, an @'s name, what is taken whole, or none of these.
        self.open: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        inside = self.open[-1] if self.open else None
        if tag in self.VOID:
            if tag == "br" and inside == "text":
                self.segments[-1]["data"]["text"] += "\n"
            return
        kind = (dict(attrs).get("class") or "").split(" ")[0]
        if inside == "whole":
            pass
        elif kind == "reply-content":
            self.reply = (dict(attrs).get("data-reply-to") or "").removeprefix("msg-")
            inside = "whole"
        elif kind in self.SEGMENTS:
            self.segments.append(json.loads(json.dumps(self.SEGMENTS[kind])))
            inside = "whole"
        elif kind == "text-content":
            self.segments.append(text(""))
            inside = "text"
        elif kind == "at-mention":
            self.segments.append({"type": "at", "name": ""})
            inside = "at"
        self.open.append(inside)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.VOID and self.open:
            self.open.pop()

    def handle_data(self, data: str) -> None:
        inside = self.open[-1] if self.open else None
        if inside == "text":
            self.segments[-1]["data"]["text"] += data
        elif inside == "at":
            self.segments[-1]["name"] += data


def read_html(paths: list[Path]) -> tuple[list[dict], dict[str, set[str]]]:
    """Their messages, and the names their writers had as each was exported. An html in
    another form has none."""
    messages: dict[str, dict] = {}
    names: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        s = path.read_text(encoding="utf-8")
        for chunk in CHUNK.finditer(s):
            batch, _ = json.JSONDecoder().raw_decode(s, chunk.end() - 1)
            for x in batch:
                h = x["html"]
                sender = SENDER.search(h)
                record = {"id": x["id"].removeprefix("msg-"), "time": x["ts"] / 1000}
                if "system-message" in h or sender is None:
                    messages[record["id"]] = record | {"system": True}
                    continue
                names[unescape(sender.group(2))].add(sender.group(1))
                content = Content()
                content.feed(h)
                messages[record["id"]] = record | {
                    "system": False,
                    "qq": sender.group(1),
                    "name": x["name"],
                    "segments": content.segments,
                    "reply": content.reply,
                }
    return sorted(messages.values(), key=lambda r: r["time"]), names


# ---- Who said it


class Writers:
    """Who each name stands for: a QQ number when sure, or a number below zero for each name
    that isn't, the same for the writer and those who @ them."""

    def __init__(self, sure: dict[str, int], claims: dict[str, set[str]], writing: set[str]):
        self.sure = sure
        self.claims = claims
        self.others: dict[str, int] = {}
        # Longest first, as a name runs on into the text after an @; not one like "23", which
        # starts many others.
        names = set(claims) | writing
        self.names = sorted(
            (n for n in names if len(n) >= MENTION_LEAST and not n.isdigit()), key=len, reverse=True
        )

    def of(self, name: str) -> int:
        if name in self.sure:
            return self.sure[name]
        return self.others.setdefault(name, -len(self.others) - 1)

    def mentioned(self, name: str) -> int:
        """Who an @ is to: as with writers, or anyone the name was ever only one person's."""
        claims = self.claims.get(name, set())
        return (
            int(next(iter(claims))) if name not in self.sure and len(claims) == 1 else self.of(name)
        )

    def name_at(self, t: str) -> str | None:
        return next((n for n in self.names if t.startswith(n)), None)


def sure_of(
    txt: list[dict],
    shown: dict[str, set[str]],
    claims: dict[str, set[str]],
    joined: dict[int, int | None],
) -> dict[str, int]:
    """The txt's names known for sure: shown in the html as one person's name at its export, and
    nowhere as anyone else's; and that person had joined the group by the first they said."""
    first: dict[str, float] = {}
    for r in txt:
        first.setdefault(r["name"], r["time"])
    sure = {}
    for name, at in first.items():
        if name not in shown or len(claims[name]) != 1:
            continue
        qq = int(next(iter(claims[name])))
        if joined.get(qq) is None or at >= joined[qq] - JOINED_SLACK:
            sure[name] = qq
    return sure


# ---- Segments


def segments_of(said: str, writers: Writers) -> list[dict]:
    """The txt's text in segments, its @s to whom they name."""
    found: list[dict] = []
    rest = said
    while rest:
        m = PLACEHOLDER.search(rest)
        head, rest = (rest, "") if m is None else (rest[: m.start()], rest[m.end() :])
        found += mentions(head, writers)
        if m is None:
            break
        kind = m.group(1) or m.group(2)
        if m.group(3):
            found.append({"type": "at", "data": {"uin": m.group(3)}})
        elif kind == "表情" or m.group(0).startswith("[/"):
            found.append({"type": "face", "data": {}})
        elif kind == "动画表情":
            found.append({"type": "image", "data": {"subType": "sticker"}})
        elif kind == "图片":
            found.append(text(LOST))
        elif kind == "卡片消息":
            card = "forward" if m.group(0).startswith("[卡片消息：[聊天记录]") else "json"
            found.append({"type": card, "data": {}})
        else:
            found.append({"type": KINDS[kind], "data": {}})
    return found


def mentions(t: str, writers: Writers) -> list[dict]:
    found = []
    parts = t.split("@")
    found.append(text(parts[0]))
    for part in parts[1:]:
        name = writers.name_at(part)
        if name is None:
            found.append(text("@" + part))
        else:
            found.append({"type": "at", "data": {"uin": str(writers.mentioned(name))}})
            found.append(text(part[len(name) :]))
    return [s for s in found if s["type"] != "text" or s["data"]["text"]]


def without_lost(segments: list[dict]) -> list[dict]:
    """Its pictures were lost: a message of nothing else says nothing."""

    def says(s: dict) -> bool:
        if s["type"] == "text":
            return bare(s["data"]["text"]) not in ("", LOST)
        return s["type"] not in ("face", "reply") and s["data"].get("subType") != "sticker"

    return segments if any(says(s) for s in segments) else [s for s in segments if s != text(LOST)]


# ---- The export


def convert_legacy(
    txt: list[dict],
    html: list[dict],
    shown: dict[str, set[str]],
    later: list[dict],
    joined: dict[int, int | None],
    group: int,
    bot: int,
) -> tuple[dict, Counter]:
    """An export as QQChatExporter's JSON, and what came of it."""
    tally: Counter = Counter()
    claims: dict[str, set[str]] = defaultdict(set)
    for name, qqs in shown.items():
        claims[name] |= qqs
    for r in html:
        if not r["system"]:
            claims[r["name"]].add(r["qq"])
    for export in later:
        for m in export["messages"]:
            sender = m.get("sender") or {}
            if not m["system"] and sender.get("uin"):
                for key in ("name", "groupCard", "nickname"):
                    if sender.get(key):
                        claims[sender[key]].add(sender["uin"])
    start = html[0]["time"] if html else float("inf")
    tally["txt after the html began"] = sum(r["time"] >= start for r in txt)
    txt = [r for r in txt if r["time"] < start]
    writers = Writers(sure_of(txt, shown, claims, joined), claims, {r["name"] for r in txt})

    records = []
    by_text: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(txt):
        record = {"id": f"txt-{i}", "time": r["time"], "system": r["text"].startswith("[系统")}
        if record["system"]:
            records.append(record)
            continue
        user = writers.of(r["name"])
        said = r["text"]
        segments = []
        if m := REPLY.match(said):
            said = said[m.end() :]
            quoted = bare(m.group(1))
            name = writers.name_at(said[1:]) if said.startswith("@") else None
            to = writers.mentioned(name) if name else None
            candidates = [
                j for j in by_text.get(quoted, []) if r["time"] - records[j]["time"] < QUOTED_WITHIN
            ]
            if to is not None and any(records[j]["user"] == to for j in candidates):
                candidates = [j for j in candidates if records[j]["user"] == to]
            if candidates:
                segments.append(
                    {
                        "type": "reply",
                        "data": {"referencedMessageId": records[candidates[-1]]["id"]},
                    }
                )
                tally["replies found"] += 1
            else:
                # Not to be found: what it quoted shows what it answers.
                quote = m.group(1).replace("[图片]", "图片")
                segments.append(text(f"（回复：{quote}）"))
                tally["replies not found"] += 1
        segments += segments_of(said, writers)
        records.append(
            record | {"user": user, "name": r["name"], "segments": without_lost(segments)}
        )
        by_text[bare(said)].append(len(records) - 1)
    for r in html:
        if r["system"]:
            records.append(r)
            continue
        segments = []
        if r["reply"]:
            segments.append({"type": "reply", "data": {"referencedMessageId": r["reply"]}})
        for s in r["segments"]:
            if s["type"] == "at":
                name = s.get("name", "").removeprefix("@").strip()
                s = {"type": "at", "data": {"uin": str(writers.mentioned(name))}}
            segments.append(s)
        records.append(r | {"user": int(r["qq"]), "segments": without_lost(segments)})

    messages = []
    for seq, r in enumerate(records, LEGACY_SEQ):
        message = {
            "id": r["id"],
            "seq": str(seq),
            "timestamp": int(r["time"] * 1000),
            "system": r["system"],
            "recalled": False,
        }
        if r["system"]:
            tally["system"] += 1
            messages.append(message | {"sender": {}, "content": {"elements": [], "text": ""}})
            continue
        tally["known" if r["user"] > 0 else "not known"] += 1
        tally["pictures lost"] += sum(s == text(LOST) for s in r["segments"])
        said = "".join(s["data"]["text"] for s in r["segments"] if s["type"] == "text")
        sender = {"uin": str(r["user"]), "name": r["name"], "nickname": "", "groupCard": r["name"]}
        messages.append(
            message | {"sender": sender, "content": {"elements": r["segments"], "text": said}}
        )
    tally["people known"] = len(
        {m["sender"]["uin"] for m in messages if m["sender"] and int(m["sender"]["uin"]) > 0}
    )
    tally["people not known"] = len(writers.others)
    info = {"peerUid": str(group), "selfUin": str(bot)}
    return {"chatInfo": info, "messages": messages}, tally


async def joined_of(forum: Forum, qqs: set[int]) -> dict[int, int | None]:
    """When each joined the group as the forum knows it; None for one it doesn't."""
    found: dict[int, int | None] = {}
    for qq in qqs:
        try:
            found[qq] = (await forum.call("GET", f"/users/qq:{qq}"))["joined_at"]
        except ForumError as e:
            if e.status != 404:
                raise
            found[qq] = None
    return found


async def main_async(args: argparse.Namespace) -> None:
    out = Path(args.out)
    if out.exists():
        # Its messages' IDs, by their order, are what the forum knows them by once archived.
        raise SystemExit(f"{out} is there already: archive from it, or write a new one elsewhere")
    paths = [Path(p) for p in args.exports]
    txt = read_txt(next(p for p in paths if p.suffix == ".txt"))
    html, shown = read_html(sorted(p for p in paths if p.suffix == ".html"))
    later = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.later]
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    forum = Forum(config["forum_url"], config["token"])
    try:
        joined = await joined_of(forum, {int(q) for qqs in shown.values() for q in qqs})
    finally:
        await forum.close()
    info = later[0]["chatInfo"]
    export, tally = convert_legacy(
        txt, html, shown, later, joined, int(info["peerUid"]), int(info["selfUin"])
    )
    out.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    for key, n in sorted(tally.items()):
        print(f"{key}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Turn a member's old exports into one for tools.history"
    )
    parser.add_argument("exports", nargs="+", help="txt 和每天的 html")
    parser.add_argument("--later", nargs="+", required=True, help="bot 后来导出的 JSON，对名字用")
    parser.add_argument("--config", required=True, help="插件的配置，查成员入群时间用")
    parser.add_argument("--out", required=True, help="转成的导出写到哪")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
