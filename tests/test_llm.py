import json

import httpx
import pytest

from mousehole.judge import DUPLICATE_SCHEMA
from mousehole.llm import BUSY_WAIT, LLM, BudgetExhausted, Settings

ECNU = Settings("https://chat.ecnu.edu.cn/open/api/v1", "key", "ecnu-plus")


def answering(*statuses):
    """A fake API answering with these statuses in turn, then 200; keeps what it was sent."""
    sent, left = [], list(statuses)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if left:
            return httpx.Response(left.pop(0), text="busy")
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "想想"}}]},
            {"choices": [{"delta": {"content": '{"same"'}}]},
            {"choices": [{"delta": {"content": ": null}"}}]},
            {"choices": [], "usage": {"prompt_tokens": 600, "completion_tokens": 400}},
        ]
        lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
        return httpx.Response(200, text="".join(lines))

    return httpx.MockTransport(handle), sent


@pytest.mark.parametrize(
    ("change", "expected", "absent"),
    [
        (
            {},
            {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
                "response_format": {"type": "json_schema", "json_schema": DUPLICATE_SCHEMA},
            },
            ["temperature"],
        ),
        (
            {"thinking": "disabled", "structured_output": "json_object"},
            {
                "thinking": {"type": "disabled"},
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            ["reasoning_effort"],
        ),
        (
            {"thinking": "omit", "structured_output": "off"},
            {"temperature": 0.2},
            ["thinking", "response_format"],
        ),
    ],
)
async def test_requests_carry_the_chosen_options(change, expected, absent):
    transport, sent = answering()
    llm = LLM(Settings(**vars(ECNU) | change), transport)
    assert await llm.complete("系统", "提示", DUPLICATE_SCHEMA) == ('{"same": null}', 600, 400)
    (body,) = sent
    assert body.items() >= expected.items()
    assert not set(absent) & set(body)
    assert (body["model"], body["max_tokens"], body["stream"]) == ("ecnu-plus", 16384, True)


async def test_too_many_requests_at_once_wait_their_turn():
    transport, sent = answering(429, 429)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    await LLM(ECNU, transport, sleep).complete("系统", "提示")
    assert (len(sent), slept) == (3, [BUSY_WAIT, BUSY_WAIT])


async def test_the_daily_limit_stops_calls_once_reached():
    transport, sent = answering()
    llm = LLM(Settings(**vars(ECNU) | {"tokens_per_day": 1500}), transport)
    await llm.complete("系统", "提示")
    await llm.complete("系统", "提示")
    with pytest.raises(BudgetExhausted):
        await llm.complete("系统", "提示")
    assert len(sent) == 2
