import json

import httpx
import pytest

from mousehole.judge import DUPLICATE_SCHEMA
from mousehole.llm import (
    BUSY_WAIT,
    LLM,
    RETRY_WAITS,
    BudgetExhausted,
    LLMError,
    Settings,
    Unavailable,
)

ECNU = Settings("https://chat.ecnu.edu.cn/open/api/v1", "key", "ecnu-plus")


def answering(*troubles):
    """A fake API answering with these in turn, then with a whole answer; keeps what it was
    sent. A trouble is a status, "dropped" for a connection lost, "broken" for a stream that
    stops before the answer, "length" for an answer that reached max_tokens."""
    sent, left = [], list(troubles)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        trouble = left.pop(0) if left else None
        if isinstance(trouble, int):
            return httpx.Response(trouble, text="busy")
        if trouble == "dropped":
            raise httpx.RemoteProtocolError("peer closed connection")
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "想想"}}]},
            {"choices": [{"delta": {"content": '{"same"'}}]},
        ]
        if trouble != "broken":
            finish = "length" if trouble == "length" else "stop"
            chunks.append({"choices": [{"delta": {"content": ": null}"}, "finish_reason": finish}]})
            chunks.append(
                {"choices": [], "usage": {"prompt_tokens": 600, "completion_tokens": 400}}
            )
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
    assert (body["model"], body["max_tokens"], body["stream"]) == ("ecnu-plus", 32768, True)


async def test_too_many_requests_at_once_wait_their_turn():
    transport, sent = answering(429, 429)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    await LLM(ECNU, transport, sleep).complete("系统", "提示")
    assert (len(sent), slept) == (3, [BUSY_WAIT, BUSY_WAIT])


async def test_lost_connections_broken_answers_and_server_errors_are_asked_again():
    transport, sent = answering("dropped", "broken", 502)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    assert await LLM(ECNU, transport, sleep).complete("系统", "提示") == (
        '{"same": null}',
        600,
        400,
    )
    assert (len(sent), slept) == (4, list(RETRY_WAITS))


async def test_asking_again_gives_up_in_the_end_and_bad_answers_are_not_asked_again():
    # Unavailable stops the batch to try again later; LLMError loses the segment.
    async def sleep(seconds):
        pass

    transport, sent = answering(*["dropped"] * (len(RETRY_WAITS) + 1))
    with pytest.raises(Unavailable):
        await LLM(ECNU, transport, sleep).complete("系统", "提示")
    transport, sent = answering(401)
    with pytest.raises(Unavailable):
        await LLM(ECNU, transport, sleep).complete("系统", "提示")
    for trouble in ("length", 422):
        transport, sent = answering(trouble)
        with pytest.raises(LLMError):
            await LLM(ECNU, transport, sleep).complete("系统", "提示")
        assert len(sent) == 1


async def test_the_daily_limit_stops_calls_once_reached():
    transport, sent = answering()
    llm = LLM(Settings(**vars(ECNU) | {"tokens_per_day": 1500}), transport)
    await llm.complete("系统", "提示")
    await llm.complete("系统", "提示")
    with pytest.raises(BudgetExhausted):
        await llm.complete("系统", "提示")
    assert len(sent) == 2
