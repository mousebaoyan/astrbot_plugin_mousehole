"""An in-memory forum answering the API auto-archiving uses, for unit tests, replay
(docs/replay.md §3) and the history archived offline (docs/history.md). It follows the forum's
rules that matter here: one floor per QQ message, idempotency keys, `proxy_disabled`, and titles
editing the opening post."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from mousehole.files import append_line
from mousehole.forum import Forum

# The largest integer the forum's JSON keeps exact.
MAX_ID = 2**53 - 1
# What is made offline gets IDs from here on, past any the forum has: an import tells them from
# the forum's own by it.
OFFLINE_IDS = 10**12


@dataclass
class Post:
    id: int
    thread: int
    floor: int
    author: int
    body: str
    reply_to: int | None
    created_at: int
    sources: list[int] = field(default_factory=list)
    edited_at: int | None = None
    deleted: bool = False
    accepted: bool = False
    # Earlier bodies, oldest first.
    revisions: list[str] = field(default_factory=list)
    # By whom: their emoji, their boost.
    reactions: dict[int, str] = field(default_factory=dict)
    boosts: dict[int, str] = field(default_factory=dict)


@dataclass
class Thread:
    id: int
    board: str
    kind: str
    title: str
    tags: list[str]
    author: int
    created_at: int
    posts: list[Post] = field(default_factory=list)
    deleted: bool = False
    locked: bool = False


def bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


class FakeForum:
    def __init__(
        self,
        boards: list[dict] | None = None,
        tags: list[str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.boards = boards or [
            {"slug": "qa", "name": "保研问答", "description": "", "post_role": "user"}
        ]
        self.tags = tags or []
        self.clock = clock
        self.threads: dict[int, Thread] = {}
        self.posts: dict[int, Post] = {}
        # (group, message id) → post id
        self.links: dict[tuple[int, int], int] = {}
        # Idempotency key → the request it was first used on, and the answer when it succeeded.
        self.keys: dict[str, tuple[tuple[str, str], httpx.Response]] = {}
        self.proxy_disabled: set[int] = set()
        # Who left: frozen, they are only archived; strangers have no account yet.
        self.frozen: set[int] = set()
        self.strangers: set[int] = set()
        # The member records pushed, by group.
        self.synced: dict[int, list[dict]] = {}
        # Whether the token may make tags.
        self.moderate = True
        self.uploads = 0
        self.ids = 0
        # Each thread's bigrams, as /search/similar compares them, made again once it changes.
        self.words: dict[int, set[str]] = {}

    @classmethod
    def of_snapshot(cls, snapshot: dict) -> "FakeForum":
        """The forum as tools/handoff.py took it, for the history archived offline."""
        fake = cls(snapshot["boards"], snapshot["tags"])
        for t in snapshot["threads"]:
            thread = Thread(
                t["id"], t["board"], t["kind"], t["title"], t["tags"], 0, t["created_at"]
            )
            thread.locked = t["locked"]
            for p in t["posts"]:
                post = Post(
                    p["id"], t["id"], p["floor"], 0, p["body"], p["reply_to"], p["created_at"]
                )
                post.edited_at, post.deleted = p["edited_at"], p["deleted"]
                thread.posts.append(post)
                fake.posts[post.id] = post
            fake.threads[thread.id] = thread
        for group, message, post in snapshot["links"]:
            fake.links[group, message] = post
        fake.ids = OFFLINE_IDS
        return fake

    def client(self) -> Forum:
        return Forum("http://forum.test", "token", httpx.MockTransport(self.handle))

    def journaled(self, path: Path) -> Forum:
        """A client that writes down each change it makes, as it is made, one request a line.
        Made again on the same snapshot, in order, the changes bring the fake back to where it
        was, IDs and all; the import makes them on the forum (tools/handoff.py)."""
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                res = self.handle(request_of(entry))
                made = self.made(entry["method"], entry["path"], res)
                if res.status_code >= 400 or made != entry["made"]:
                    raise SystemExit(f"{path} does not go with the snapshot: {line[:200]}")

        def handle(request: httpx.Request) -> httpx.Response:
            # Asked again with a key: answered as the first time, nothing made.
            fresh = request.headers.get("Idempotency-Key") not in self.keys
            res = self.handle(request)
            if request.method != "GET" and res.status_code < 400 and fresh:
                entry = entry_of(request)
                entry["made"] = self.made(entry["method"], entry["path"], res)
                append_line(path, json.dumps(entry, ensure_ascii=False))
            return res

        return Forum("http://forum.offline", "", httpx.MockTransport(handle))

    def made(self, method: str, path: str, res: httpx.Response) -> dict:
        """The thread and the floor a write made."""
        match method, path.strip("/").split("/"):
            case "POST", ["threads"]:
                thread = res.json()["id"]
                return {"thread": thread, "post": self.threads[thread].posts[0].id}
            case "POST", ["threads", _, "posts"]:
                return {"post": res.json()["id"]}
        return {}

    def next_id(self) -> int:
        self.ids += 1
        return self.ids

    def now(self) -> int:
        return int(self.clock())

    # ---- What the API returns

    def post_json(self, p: Post) -> dict:
        return {
            "id": p.id,
            "thread_id": p.thread,
            "floor": p.floor,
            "author": {"qq": p.author},
            "body": "" if p.deleted else p.body,
            "reply_to": {"id": p.reply_to} if p.reply_to else None,
            "deleted": p.deleted,
            "edited_at": p.edited_at,
            "created_at": p.created_at,
        }

    def thread_json(self, t: Thread) -> dict:
        board = next(b for b in self.boards if b["slug"] == t.board)
        opening = t.posts[0].body
        return {
            "id": t.id,
            "kind": t.kind,
            "title": t.title,
            # Users' forum IDs are their QQ numbers here.
            "author": {"id": t.author},
            "board": {"slug": board["slug"], "name": board["name"]},
            "tags": t.tags,
            "reply_count": sum(not p.deleted for p in t.posts[1:]),
            "like_count": len(t.posts[0].reactions),
            "floor_count": len(t.posts),
            "last_post_at": max(p.created_at for p in t.posts),
            "created_at": t.created_at,
            "locked": t.locked,
            "excerpt": re.sub(r"!\[\]\([^)]*\)", "", opening)[:100],
        }

    def source_json(self, t: Thread) -> dict:
        board = next(b for b in self.boards if b["slug"] == t.board)
        posts = [
            {
                "floor": p.floor,
                "op": p.author == t.author,
                "accepted": p.accepted,
                "likes": sum(emoji == "heart" for emoji in p.reactions.values()),
                "text": re.sub(r"!\[\]\([^)]*\)", "[图片]", p.body),
            }
            for p in t.posts
            if not p.deleted
        ]
        return {
            "thread": {
                "id": t.id,
                "kind": t.kind,
                "title": t.title,
                "board": board["name"],
                "tags": t.tags,
                "positioning": False,
            },
            "replies": len(posts) - 1,
            "posts": posts,
        }

    # ---- Requests

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        path = request.url.path.removeprefix("/api/v1")
        if key in self.keys:
            first, res = self.keys[key]
            return res if first == (request.method, path) else error(422, "idempotency_key_reused")
        json_body = request.headers.get("Content-Type") == "application/json"
        body = json.loads(request.content) if json_body else None
        act_as = request.headers.get("X-Act-As")
        user = int(act_as.removeprefix("qq:")) if act_as else 0
        if user in self.strangers:
            return error(404, "unknown_member")
        if request.method != "GET" and user in self.proxy_disabled:
            return error(403, "proxy_disabled")
        if request.method != "GET" and user in self.frozen and "source" not in (body or {}):
            return error(403, "member_frozen")
        res = self.route(request.method, path, dict(request.url.params), body, user)
        if key is not None and res.status_code < 400:
            self.keys[key] = ((request.method, path), res)
        return res

    def route(self, method: str, path: str, params: dict, body: Any, user: int) -> httpx.Response:
        parts = path.strip("/").split("/")
        match method, parts:
            case "GET", ["boards"]:
                return ok({"items": self.boards})
            case "GET", ["tags"]:
                return ok({"items": [{"name": t, "kind": "topic"} for t in self.tags]})
            case "POST", ["tags"]:
                if not self.moderate:
                    return error(403, "scope_missing")
                if body["name"] in self.tags:
                    return error(409, "tag_exists")
                self.tags.append(body["name"])
                return httpx.Response(201, json={"name": body["name"], "kind": body["kind"]})
            case "GET", ["threads"]:
                live = [t for t in self.threads.values() if not t.deleted]
                return ok({"items": [self.thread_json(t) for t in live], "next_cursor": None})
            case "POST", ["threads"]:
                return self.create_thread(body, user)
            case "GET", ["threads", thread]:
                t = self.threads.get(int(thread))
                if not t or t.deleted:
                    return error(404, "thread_not_found")
                return ok(self.thread_json(t))
            case "GET", ["bot", "summaries", thread, "source"]:
                t = self.threads.get(int(thread))
                if not t or t.deleted:
                    return error(404, "thread_not_found")
                return ok(self.source_json(t))
            case "PATCH", ["threads", thread]:
                t = self.threads[int(thread)]
                opening = t.posts[0]
                opening.revisions.append(f"标题：{t.title}")
                t.title = body["title"]
                self.words.pop(t.id, None)
                opening.edited_at = self.now()
                return ok(self.thread_json(t))
            case "DELETE", ["threads", thread]:
                t = self.threads[int(thread)]
                if any(p.author != t.author and not p.deleted for p in t.posts[1:]):
                    return error(403, "forbidden")
                t.deleted = True
                self.unlink(t.posts)
                return httpx.Response(204)
            case "GET", ["threads", thread, "posts"]:
                posts = self.threads[int(thread)].posts
                if "before" in params:
                    found = [p for p in posts if p.floor < int(params["before"])][-20:]
                else:
                    found = [p for p in posts if p.floor > int(params.get("after", 0))][:20]
                return ok({"items": [self.post_json(p) for p in found]})
            case "POST", ["threads", thread, "posts"]:
                return self.create_post(self.threads[int(thread)], body, user)
            case "GET", ["posts", post]:
                return ok(self.post_json(self.posts[int(post)]))
            case "PATCH", ["posts", post]:
                return self.edit_post(self.posts[int(post)], body)
            case "DELETE", ["posts", post]:
                p = self.posts[int(post)]
                p.deleted = True
                self.unlink([p])
                self.words.pop(p.thread, None)
                return httpx.Response(204)
            case "PUT", ["posts", post, "reaction"]:
                p = self.posts[int(post)]
                if p.author == user:
                    return error(403, "forbidden")
                p.reactions[user] = body["emoji"]
                return httpx.Response(204)
            case "PUT", ["posts", post, "boost"]:
                p = self.posts[int(post)]
                p.boosts[user] = body["body"]
                return ok({"body": body["body"], "created_at": body.get("created_at")})
            case "GET", ["users", user] if int(user.removeprefix("qq:")) in self.strangers:
                return error(404, "not_found")
            case "GET", ["users", user]:
                return ok({"id": int(user.removeprefix("qq:"))})
            case "GET", ["bot", "messages", "group", group, message]:
                if int(message) > MAX_ID:
                    return error(400, "invalid_request")
                post_id = self.links.get((int(group), int(message)))
                if post_id is None:
                    return error(404, "message_not_linked")
                return ok({"thread_id": self.posts[post_id].thread, "post_id": post_id})
            case "GET", ["search", "similar"]:
                return ok({"items": self.similar(params["text"])})
            case "PATCH", ["bot", "groups", group, "members"]:
                self.synced.setdefault(int(group), []).extend(body["members"])
                left = {m["user_id"] for m in body["members"] if m.get("in_group") is False}
                self.frozen |= left
                self.strangers -= left
                return ok({"joined": 0, "left": len(left)})
            case "POST", ["uploads"]:
                self.uploads += 1
                return httpx.Response(201, json={"url": f"/uploads/00/{self.uploads:064x}.webp"})
            case "POST", ["uploads", "fetch"]:
                self.uploads += 1
                return httpx.Response(201, json={"url": f"/uploads/00/{self.uploads:064x}.webp"})
        raise AssertionError(f"fake forum: no route for {method} {path}")

    def similar(self, text: str) -> list[dict]:
        wanted = bigrams(text)
        scored = []
        for t in self.threads.values():
            if t.deleted:
                continue
            if t.id not in self.words:
                self.words[t.id] = bigrams(
                    t.title + "".join(p.body for p in t.posts if not p.deleted)
                )
            words = self.words[t.id]
            shared = len(wanted & words)
            if shared:
                scored.append((shared / (shared + 10), t))
        scored.sort(key=lambda s: -s[0])
        return [{"thread": {"id": t.id}, "score": score} for score, t in scored[:5]]

    # ---- Writing

    def render(self, content: dict) -> str:
        if "markdown" in content:
            return content["markdown"]
        parts = []
        for s in content["onebot"]:
            if s["type"] == "text":
                parts.append(s["data"]["text"])
            elif s["type"] == "image":
                parts.append(f"![]({s['data']['url']})")
        return "".join(parts)

    def taken(self, source: dict, post: int | None) -> httpx.Response | None:
        """409 when a message belongs to another floor already; 400, as the forum's JSON
        answers, for IDs past the integers it keeps exact."""
        if any(m > MAX_ID for m in source["message_ids"]):
            return error(400, "invalid_request")
        for message in source["message_ids"]:
            owner = self.links.get((source["chat_id"], message))
            if owner is not None and owner != post:
                details = {"thread_id": self.posts[owner].thread, "post_id": owner}
                return error(409, "source_exists", details)
        return None

    def link(self, source: dict, p: Post) -> None:
        self.unlink([p])
        for message in source["message_ids"]:
            self.links[source["chat_id"], message] = p.id
        p.sources = list(source["message_ids"])

    def unlink(self, posts: list[Post]) -> None:
        ids = {p.id for p in posts}
        self.links = {k: v for k, v in self.links.items() if v not in ids}

    def create_thread(self, body: dict, user: int) -> httpx.Response:
        if conflict := self.taken(body["source"], None):
            return conflict
        if len(body["title"]) < 4:
            return error(400, "invalid_request")
        if any(tag not in self.tags for tag in body["tags"]):
            return error(400, "invalid_request")
        t = Thread(
            self.next_id(),
            body["board"],
            body["kind"],
            body["title"],
            body["tags"],
            user,
            body.get("created_at", self.now()),
        )
        self.threads[t.id] = t
        opening = Post(
            self.next_id(), t.id, 1, user, self.render(body["content"]), None, t.created_at
        )
        t.posts.append(opening)
        self.posts[opening.id] = opening
        self.link(body["source"], opening)
        return httpx.Response(201, json=self.thread_json(t))

    def create_post(self, t: Thread, body: dict, user: int) -> httpx.Response:
        if conflict := self.taken(body["source"], None):
            return conflict
        reply_to = body.get("reply_to_id")
        if reply_to is not None and (
            self.posts[reply_to].thread != t.id or self.posts[reply_to].deleted
        ):
            return error(400, "invalid_request")
        p = Post(
            self.next_id(),
            t.id,
            len(t.posts) + 1,
            user,
            self.render(body["content"]),
            reply_to,
            body.get("created_at", self.now()),
        )
        t.posts.append(p)
        self.posts[p.id] = p
        self.link(body["source"], p)
        self.words.pop(t.id, None)
        return httpx.Response(201, json=self.post_json(p))

    def edit_post(self, p: Post, body: dict) -> httpx.Response:
        if conflict := self.taken(body["source"], p.id):
            return conflict
        p.revisions.append(p.body)
        p.body = self.render(body["content"])
        p.edited_at = self.now()
        self.link(body["source"], p)
        self.words.pop(p.thread, None)
        return ok(self.post_json(p))


def entry_of(request: httpx.Request) -> dict:
    """A write as the journal keeps it."""
    act_as = request.headers.get("X-Act-As")
    json_body = request.headers.get("Content-Type") == "application/json"
    return {
        "method": request.method,
        "path": request.url.path.removeprefix("/api/v1"),
        "act_as": int(act_as.removeprefix("qq:")) if act_as else None,
        "key": request.headers.get("Idempotency-Key"),
        "body": json.loads(request.content) if json_body else None,
    }


def request_of(entry: dict) -> httpx.Request:
    headers = {}
    if entry["act_as"] is not None:
        headers["X-Act-As"] = f"qq:{entry['act_as']}"
    if entry["key"] is not None:
        headers["Idempotency-Key"] = entry["key"]
    content = None
    if entry["body"] is not None:
        headers["Content-Type"] = "application/json"
        content = json.dumps(entry["body"], ensure_ascii=False).encode()
    url = f"http://forum.offline/api/v1{entry['path']}"
    return httpx.Request(entry["method"], url, headers=headers, content=content)


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


def error(status: int, code: str, details: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}} | (details or {}))
