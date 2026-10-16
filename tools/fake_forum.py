"""An in-memory forum answering the API auto-archiving uses, for unit tests and replay
(docs/replay.md §3). It follows the forum's rules that matter here: one floor per QQ message,
idempotency keys, `proxy_disabled`, and titles editing the opening post."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from mousehole.forum import Forum


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
        self.keys: dict[str, httpx.Response] = {}
        self.proxy_disabled: set[int] = set()
        # Whether the token may make tags.
        self.moderate = True
        self.uploads = 0
        self.ids = 0

    def client(self) -> Forum:
        return Forum("http://forum.test", "token", httpx.MockTransport(self.handle))

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
            "floor_count": len(t.posts),
            "created_at": t.created_at,
            "locked": t.locked,
            "excerpt": re.sub(r"!\[\]\([^)]*\)", "", opening)[:100],
        }

    # ---- Requests

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        if key in self.keys:
            return self.keys[key]
        path = request.url.path.removeprefix("/api/v1")
        json_body = request.headers.get("Content-Type") == "application/json"
        body = json.loads(request.content) if json_body else None
        act_as = request.headers.get("X-Act-As")
        user = int(act_as.removeprefix("qq:")) if act_as else 0
        if request.method != "GET" and user in self.proxy_disabled:
            return error(403, "proxy_disabled")
        res = self.route(request.method, path, dict(request.url.params), body, user)
        if key is not None and res.status_code < 500:
            self.keys[key] = res
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
                return ok({"items": [{"id": t.id} for t in live], "next_cursor": None})
            case "POST", ["threads"]:
                return self.create_thread(body, user)
            case "GET", ["threads", thread]:
                t = self.threads.get(int(thread))
                return ok(self.thread_json(t)) if t and not t.deleted else error(404, "not_found")
            case "PATCH", ["threads", thread]:
                t = self.threads[int(thread)]
                opening = t.posts[0]
                opening.revisions.append(f"标题：{t.title}")
                t.title = body["title"]
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
            case "GET", ["users", user]:
                return ok({"id": int(user.removeprefix("qq:"))})
            case "GET", ["bot", "messages", "group", group, message]:
                post_id = self.links.get((int(group), int(message)))
                if post_id is None:
                    return error(404, "message_not_linked")
                return ok({"thread_id": self.posts[post_id].thread, "post_id": post_id})
            case "GET", ["search", "similar"]:
                return ok({"items": self.similar(params["text"])})
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
            words = bigrams(t.title + "".join(p.body for p in t.posts if not p.deleted))
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
        """409 when a message belongs to another floor already."""
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
        p = Post(
            self.next_id(),
            t.id,
            len(t.posts) + 1,
            user,
            self.render(body["content"]),
            body.get("reply_to_id"),
            body.get("created_at", self.now()),
        )
        t.posts.append(p)
        self.posts[p.id] = p
        self.link(body["source"], p)
        return httpx.Response(201, json=self.post_json(p))

    def edit_post(self, p: Post, body: dict) -> httpx.Response:
        if conflict := self.taken(body["source"], p.id):
            return conflict
        p.revisions.append(p.body)
        p.body = self.render(body["content"])
        p.edited_at = self.now()
        self.link(body["source"], p)
        return ok(self.post_json(p))


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


def error(status: int, code: str, details: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}} | (details or {}))
