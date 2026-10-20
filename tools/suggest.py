"""Scores 猜你想问 on the labeled questions in data/suggest (docs/replay.md §8): the plugin's own
code, prompts and settings over the questions of some days, against the threads as they stood
when each was asked.

uv run python -m tools.suggest score --config <插件的配置> 10-14 10-19
uv run python -m tools.suggest label 100200300:7690000000000000002 17=Y 23=N
"""

import argparse
import asyncio
import json
from datetime import datetime
from functools import partial
from pathlib import Path

from mousehole import judge, suggest
from mousehole.llm import LLM, Reranker, Settings
from tools.replay import CREDITS_RERANK, Cached, credits

DATA = Path(__file__).parents[1] / "data" / "suggest"
# How a thread told is labeled: answers the question, near enough, or not.
GOOD, OK, BAD = "Y", "P", "N"


def key(q: dict) -> str:
    return f"{q['group']}:{q['id']}"


def found(q: dict, threads: dict) -> list[dict]:
    """The candidates of the question as threads_for has them, with the floors there were then."""
    out = []
    for t, similarity in q["near"][: suggest.NEAREST]:
        thread = threads[str(t)]
        posts = [p for p in thread["posts"] if p["at"] < q["time"]]
        replies = sum(p["floor"] > 1 for p in posts)
        if similarity < suggest.SIMILARITY or not replies:
            continue
        source = {"thread": thread["thread"], "replies": replies, "posts": posts}
        out.append(
            {
                "id": t,
                "title": thread["thread"]["title"],
                "created": datetime.fromtimestamp(thread["created"]).strftime("%Y-%m-%d"),
                "source": source,
                "posts": judge.floors_within(posts, judge.SUGGEST_FLOOR_CHARS, judge.SUGGEST_CHARS),
            }
        )
    return out


async def score(args: argparse.Namespace) -> None:
    questions = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    threads = json.loads((DATA / "threads.json").read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    # Configs saved before the reranker was a setting lack it: the defaults fill in.
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    m = {k: v["default"] for k, v in schema["llm"]["items"].items()} | config["llm"]
    cache = Cached(DATA / "cache.jsonl")
    llm = LLM(Settings.of(m | {"tokens_per_day": 0}), cache)
    reranker = Reranker(m["base_url"], m["api_key"], m["rerank_model"], cache)
    # As main.py gives it to Suggest.
    complete = partial(llm.complete, think=False)
    asked = [q for q in questions if args.start <= q["day"] <= args.end]
    days = len({q["day"] for q in asked})
    tokens, told, reranked = [0, 0], [], 0
    for q in asked:
        candidates = found(q, threads)
        if not candidates:
            continue
        picked, input_tokens, output_tokens = await suggest.pick(
            complete, reranker.rerank, q["lines"], q["text"], candidates
        )
        reranked += 1
        tokens[0] += input_tokens
        tokens[1] += output_tokens
        if picked:
            told.append((q, [t["id"] for t in picked]))
    await llm.close()
    await reranker.close()

    labels = [[q["labels"].get(str(t), "?") for t in ts] for q, ts in told]
    good = sum(GOOD in ls for ls in labels)
    ok = sum(GOOD not in ls and OK in ls for ls in labels)
    unlabeled = sum(not {GOOD, OK} & set(ls) and "?" in ls for ls in labels)
    answered = sum(GOOD in q["labels"].values() for q in asked)
    spent = credits(*tokens, hour=0) + reranked * CREDITS_RERANK
    print(
        f"{args.start}..{args.end}: told {len(told)}: good {good}, ok {ok}, "
        f"bad {len(told) - good - ok - unlabeled}, unlabeled {unlabeled} "
        f"(not asking {sum(not q['asking'] for q, _ in told)}); "
        f"answered {good} of the {answered} a thread answers"
    )
    print(
        f"a day: {reranked / days:.0f} reranked, {sum(tokens) / days:.0f} tokens "
        f"({tokens[1] / days:.0f} out), {spent / days:.1f} credits off-peak; "
        f"{cache.spent:.1f} credits spent now"
    )
    for q, ts in told:
        todo = [t for t in ts if str(t) not in q["labels"]]
        if todo:
            print(f"\n{key(q)}\n  " + "\n  ".join(q["lines"][-3:]))
        for t in todo:
            thread = threads[str(t)]
            floors = [p for p in thread["posts"] if p["at"] < q["time"]][:6]
            lines = [f"#{p['floor']} {p['text'][:60]}" for p in floors]
            print(f"  [{t}] {thread['thread']['title']}\n    " + "\n    ".join(lines))


def label(args: argparse.Namespace) -> None:
    path = DATA / "questions.json"
    questions = json.loads(path.read_text(encoding="utf-8"))
    [q] = [q for q in questions if key(q) == args.question]
    for pair in args.pairs:
        t, value = pair.split("=")
        if value not in (GOOD, OK, BAD):
            raise SystemExit(f"{pair}: 标 Y、P 或 N")
        q["labels"][t] = value
    path.write_text(json.dumps(questions, ensure_ascii=False, indent=0), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score 猜你想问 (docs/replay.md §8)")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("score", help="评分")
    run.add_argument("--config", required=True, help="插件的配置文件（AstrBot 的 data/config 下）")
    run.add_argument("start", help="从这天（MM-DD）")
    run.add_argument("end", help="到这天，含这天")
    mark = commands.add_parser("label", help="标注推出去的帖子")
    mark.add_argument("question", help="群号:消息 ID")
    mark.add_argument("pairs", nargs="+", help="帖子=Y、P 或 N")
    args = parser.parse_args()
    if args.command == "score":
        asyncio.run(score(args))
    else:
        label(args)


if __name__ == "__main__":
    main()
