"""NapCat through AstrBot's aiocqhttp adapter (design §2)."""

import asyncio
import random
import time
from typing import Any

# Seconds between the messages the bot sends of its own accord.
GAP = (3, 6)
# For the coordinator the maintainer's other QQ plugins share, when it is there.
GUARD_CONFIG = {
    "recovery_min_seconds": 60,
    "recovery_max_seconds": 180,
    "failure_threshold": 3,
    "failure_cooldown_seconds": 600,
}


class QQ:
    def __init__(self, platform_manager: Any, platform_id: str):
        self.manager = platform_manager
        self.platform_id = platform_id
        self.sending = asyncio.Lock()
        self.next_at = 0.0

    def platform(self) -> Any:
        found = [p for p in self.manager.get_insts() if p.meta().name == "aiocqhttp"]
        if self.platform_id:
            found = [p for p in found if p.meta().id == self.platform_id]
        if len(found) != 1:
            raise RuntimeError(f"expected one aiocqhttp platform, found {len(found)}")
        return found[0]

    async def call(self, action: str, **params: Any) -> Any:
        return await self.platform().bot.call_action(action, **params)

    async def online(self) -> bool:
        return (await self.call("get_status"))["online"]

    async def send_group(self, group: int, message: list[dict[str, Any]]) -> None:
        async def send() -> None:
            await self.call("send_group_msg", group_id=group, message=message)

        guard = getattr(self.manager, "_qq_automation_guard_v1", None)
        if guard is not None:
            await guard.run(
                account=self.platform().meta().id,
                online=self.online,
                action=send,
                config=GUARD_CONFIG,
                gap=GAP,
            )
            return
        async with self.sending:
            await asyncio.sleep(self.next_at - time.monotonic())
            try:
                await send()
            finally:
                self.next_at = time.monotonic() + random.uniform(*GAP)
