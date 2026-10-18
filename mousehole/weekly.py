"""The weekly poster in the groups (design §9): the forum's week as an image in each group's
album, then a message @-ing those on its lists who are in the group."""

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from .files import write_file
from .forum import Forum
from .qq import QQ
from .sync import Sync

log = logging.getLogger("astrbot.plugin.astrbot_plugin_mousehole")

# Sunday at 20 o'clock, by the machine's clock like the archive's hours.
WEEKDAY = 6
HOUR = 20
# A plugin back after a long stop doesn't post a stale week.
FRESH = 12 * 3600
MENTIONS = 20
ALBUM = "鼠洞周榜"
WIDTH = 1080
FONT = Path(__file__).parent / "poster" / "LXGWWenKaiScreen.woff2"


def last_due(now: float) -> float:
    """The latest Sunday 20:00 at or before `now`."""
    at = datetime.fromtimestamp(now)
    due = (at - timedelta(days=(at.weekday() - WEEKDAY) % 7)).replace(
        hour=HOUR, minute=0, second=0, microsecond=0
    )
    if due > at:
        due -= timedelta(days=7)
    return due.timestamp()


def mention_message(stats: dict[str, Any], members: set[int], site: str) -> list[dict]:
    """The lists of people one to a line: @ for those in this group, up to a limit, names
    for the rest."""

    def text(value: str) -> dict:
        return {"type": "text", "data": {"text": value}}

    mentioned = 0

    def person(user: dict) -> dict:
        nonlocal mentioned
        if user["qq"] in members and mentioned < MENTIONS:
            mentioned += 1
            return {"type": "at", "data": {"qq": str(user["qq"])}}
        return text(user["display_name"])

    segments = [text("【鼠洞本周周榜】")]
    people = stats["people"]
    lists = [
        ("👍 获赞最多", people["most_liked"], "赞"),
        ("✅ 被采纳最多", people["most_accepted"], "次"),
        ("💬 回复最多", people["most_replies"], "层"),
    ]
    for title, ranked, unit in lists:
        if ranked:
            segments.append(text(f"\n{title}"))
        for i, p in enumerate(ranked, 1):
            segments += [text(f"\n{i}. "), person(p["user"]), text(f" {p['count']} {unit}")]
    if stats["gold_badges"]:
        segments.append(text("\n🏅 本周金徽章"))
    for i, g in enumerate(stats["gold_badges"], 1):
        segments += [text(f"\n{i}. "), person(g["user"]), text(f"「{g['badge']['name']}」")]
    segments.append(text(f"\n{site}"))
    return segments


# The forum's colours: paper, ink, sea teal, apricot sun.
STYLE = """
@font-face { font-family: WenKai; src: url(data:font/woff2;base64,%s) format("woff2"); }
* { box-sizing: border-box; margin: 0; }
body { width: %dpx; background: #f3efe8; font-family: WenKai, sans-serif; color: #1f2a28; }
main { margin: 32px; padding: 48px 52px; background: #fefdfb; border-radius: 32px; }
header h1 { font-size: 64px; letter-spacing: 4px; }
header p { margin-top: 8px; font-size: 28px; color: #686d66; }
.overview { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 36px; }
.tile { background: #fff; border: 2px solid #ebe6dd; border-radius: 20px; padding: 18px 22px; }
.tile b { display: block; font-size: 48px; color: #2b7a6b; }
.tile span { font-size: 26px; color: #686d66; }
h2 { margin: 44px 0 14px; font-size: 34px; }
h2::before { content: "●"; color: #ef9f70; margin-right: 12px; font-size: 26px; }
ol { list-style: none; padding: 0; }
li { display: flex; align-items: center; gap: 18px; padding: 14px 0;
  border-top: 2px solid #ebe6dd; font-size: 30px; }
li .n { width: 44px; color: #a9532e; font-size: 30px; }
li .title { flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
li .board { font-size: 22px; color: #2b7a6b; background: #e6f1ee; padding: 2px 12px;
  border-radius: 999px; }
li .count { color: #2b7a6b; min-width: 90px; text-align: right; }
.avatar { width: 64px; height: 64px; border-radius: 50%%; background: #ebe6dd; object-fit: cover; }
footer { margin-top: 44px; font-size: 26px; color: #686d66; text-align: center; }
"""


def poster_html(stats: dict[str, Any], site: str, font: str, avatars: dict[str, str]) -> str:
    """The week as a page Chromium shoots; what members wrote is escaped. `avatars` are data
    URLs by the users' avatar_url, so the page needs no network."""
    start = datetime.fromtimestamp(stats["since"])
    end = datetime.fromtimestamp(stats["until"] - 1)
    o = stats["overview"]
    tiles = [
        ("新帖", o["threads"]),
        ("新回复", o["posts"]),
        ("收录", o["archived"]),
        ("发言的人", o["active"]),
        ("新登录", o["activated"]),
        ("发出徽章", o["badges"]),
    ]

    def avatar(user: dict) -> str:
        if user["avatar_url"]:
            return f'<img class="avatar" src="{avatars[user["avatar_url"]]}">'
        return '<span class="avatar"></span>'

    def threads(title: str, items: list[dict], unit: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f'<li><span class="n">{i}</span><span class="title">{escape(t["thread"]["title"])}</span>'
            f'<span class="board">{escape(t["board"]["name"])}</span>'
            f'<span class="count">{t["count"]} {unit}</span></li>'
            for i, t in enumerate(items, 1)
        )
        return f"<h2>{title}</h2><ol>{rows}</ol>"

    def people(title: str, items: list[dict], unit: str) -> str:
        if not items:
            return ""
        rows = "".join(
            f'<li><span class="n">{i}</span>{avatar(p["user"])}'
            f'<span class="title">{escape(p["user"]["display_name"])}</span>'
            f'<span class="count">{p["count"]} {unit}</span></li>'
            for i, p in enumerate(items, 1)
        )
        return f"<h2>{title}</h2><ol>{rows}</ol>"

    gold = "".join(
        f'<li>{avatar(g["user"])}<span class="title">{escape(g["user"]["display_name"])}</span>'
        f'<span class="board">{escape(g["badge"]["name"])}</span></li>'
        for g in stats["gold_badges"]
    )
    t, p = stats["threads"], stats["people"]
    body = "".join(
        [
            threads("获赞最多的帖子", t["most_liked"], "赞"),
            threads("回复最多的帖子", t["most_replied"], "层"),
            threads("读的人最多的帖子", t["most_read"], "人"),
            people("获赞最多的人", p["most_liked"], "赞"),
            people("被采纳最多的人", p["most_accepted"], "次"),
            people("回复最多的人", p["most_replies"], "层"),
            f"<h2>本周金徽章</h2><ol>{gold}</ol>" if gold else "",
        ]
    )
    overview = "".join(
        f'<div class="tile"><b>{n}</b><span>{label}</span></div>' for label, n in tiles
    )
    return (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{STYLE % (font, WIDTH)}</style></head><body><main>"
        f"<header><h1>鼠洞周榜</h1>"
        f"<p>{start.month} 月 {start.day} 日 – {end.month} 月 {end.day} 日</p></header>"
        f'<div class="overview">{overview}</div>{body}'
        f"<footer>更多讨论在鼠洞：{escape(site.removeprefix('https://'))}</footer>"
        f"</main></body></html>"
    )


async def shoot(page_html: str) -> bytes:
    """The page as a JPEG, in a Chromium of its own; Playwright is only needed here."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": WIDTH, "height": 800})
            await page.set_content(page_html)
            return await page.screenshot(type="jpeg", quality=90, full_page=True)
        finally:
            await browser.close()


class Weekly:
    def __init__(
        self,
        forum: Forum,
        qq: QQ,
        sync: Sync,
        state: Path,
        *,
        groups: list[int],
        site: str,
        render: Callable[[str], Awaitable[bytes]] = shoot,
        clock: Callable[[], float] = time.time,
    ):
        """`groups`: where the poster goes, empty for every synced group."""
        self.forum = forum
        self.qq = qq
        self.sync = sync
        self.state = state
        self.groups = groups
        self.site = site
        self.render = render
        self.clock = clock

    def load(self) -> dict[str, Any]:
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def save(self, week: float, sent: list[int]) -> None:
        write_file(self.state, json.dumps({"week": week, "sent": sent}))

    async def run(self) -> None:
        while True:
            try:
                await self.post()
            except Exception:
                log.exception("Posting the weekly poster failed")
            due = last_due(self.clock()) + 7 * 86400
            while (left := due - self.clock()) > 0:
                await asyncio.sleep(min(left, 60))

    async def post(self) -> None:
        """This week's poster to the groups not yet sent it, if it is still fresh."""
        week = last_due(self.clock())
        if self.clock() - week > FRESH:
            return
        state = self.load()
        sent: list[int] = state["sent"] if state.get("week") == week else []
        todo = [g for g in self.groups or self.sync.groups if g not in sent]
        if not todo:
            return
        stats = await self.forum.call("GET", "/bot/stats/weekly", params={"until": int(week)})
        if stats["overview"]["threads"] + stats["overview"]["posts"] == 0:
            self.save(week, sent + todo)
            return
        font = base64.b64encode(FONT.read_bytes()).decode()
        people = [p["user"] for ranked in stats["people"].values() for p in ranked]
        people += [g["user"] for g in stats["gold_badges"]]
        avatars = {}
        for url in {p["avatar_url"] for p in people if p["avatar_url"]}:
            res = await self.forum.http.get(self.site + url)
            res.raise_for_status()
            avatars[url] = "data:image/webp;base64," + base64.b64encode(res.content).decode()
        image = await self.render(poster_html(stats, self.site, font, avatars))
        for group in todo:
            # QQ itself tells the group about the upload, with the picture.
            await self.qq.upload_album(group, await self.qq.album(group, ALBUM), ALBUM, image)
            members = self.sync.roster.get(group, set())
            await self.qq.send_group(group, mention_message(stats, members, self.site))
            sent.append(group)
            self.save(week, sent)
