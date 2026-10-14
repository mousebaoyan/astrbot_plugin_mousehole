"""AstrBot entry: listens to NapCat's events and runs the plugin's background work."""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.provider.provider import EmbeddingProvider

from .mousehole.archive import Archive, Message, plain
from .mousehole.events import Events
from .mousehole.forum import Forum
from .mousehole.judge import OneJev
from .mousehole.login import login, wants_login
from .mousehole.qq import QQ
from .mousehole.similar import Index
from .mousehole.sync import Sync

NAME = "astrbot_plugin_mousehole"


class Mousehole(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.forum: Forum | None = None
        self.onejev: OneJev | None = None
        self.archive: Archive | None = None
        self.tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        c = self.config
        if not c["token"]:
            logger.warning("鼠洞插件：没有填论坛的 token，插件不工作")
            return
        data = StarTools.get_data_dir(NAME)
        self.forum = Forum(c["forum_url"], c["token"])
        self.qq = QQ(self.context.platform_manager, c["platform_id"])
        self.sync = Sync(self.forum, self.qq, members=c["sync_members"])
        index = None
        archive = c["archive"]
        self.archive_groups = {int(g) for g in archive["groups"]}
        embedder = self.context.get_provider_by_id(archive["embedding_provider"])
        if archive["enabled"] and not (
            archive["onejev_url"] and isinstance(embedder, EmbeddingProvider)
        ):
            # The rest of the plugin works on; only archiving waits for its settings.
            logger.error(
                "鼠洞插件：自动收录要配好 OneJev 的地址和 AstrBot 里的嵌入模型，现在不收录"
            )
        elif archive["enabled"]:
            model = f"{embedder.meta().id}:{embedder.meta().model}"
            index = Index(self.forum, embedder.get_embeddings, model, data / "threads.npz")
            self.onejev = OneJev(archive["onejev_url"])
            self.archive = Archive(
                self.forum,
                self.onejev,
                self.complete,
                index,
                prefixes=self.context.get_config()["wake_prefix"],
            )
        lottery = c["lottery"]
        self.events = Events(
            self.forum,
            self.qq,
            self.sync,
            data / "state.json",
            lottery=[int(g) for g in lottery["groups"]] if lottery["enabled"] else None,
            bio=c["push_bio"],
            index=index,
        )
        self.tasks = [asyncio.create_task(self.start(index))]

    async def start(self, index: Index | None) -> None:
        # Groups are known before any event or message is acted on.
        await self.sync.refresh_groups()
        self.tasks.append(asyncio.create_task(self.sync.run()))
        if self.events.types:
            self.tasks.append(asyncio.create_task(self.events.run()))
        if self.archive and index:
            await index.load()
            self.tasks.append(asyncio.create_task(self.archive.run()))

    async def terminate(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.forum:
            await self.forum.close()
        if self.onejev:
            await self.onejev.close()

    async def complete(self, system: str, prompt: str) -> tuple[str, int, int]:
        provider = self.config["archive"]["llm_provider"]
        if not provider:
            provider = (await self.context.get_using_provider_async()).meta().id
        res = await self.context.llm_generate(
            chat_provider_id=provider, prompt=prompt, system_prompt=system
        )
        usage = res.usage
        return res.completion_text, usage.input if usage else 0, usage.output if usage else 0

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        raw = event.message_obj.raw_message
        if self.forum is None or event.platform_meta.id != self.qq.platform().meta().id:
            return
        if raw["post_type"] == "notice":
            await self.on_notice(raw)
        elif raw["post_type"] == "message" and raw["message_type"] == "private":
            text = plain(raw["message"])
            if self.config["login"] and wants_login(text):
                await event.send(event.plain_result(await login(self.forum, raw["user_id"], text)))
                event.stop_event()
        elif raw["post_type"] == "message" and raw["group_id"] in self.sync.groups:
            group = raw["group_id"]
            self.sync.saw(group, raw["user_id"], raw["time"])
            if self.archives(group):
                self.archive.add(group, Message.from_onebot(raw), raw["self_id"])
            at_bot = any(
                s["type"] == "at" and str(s["data"]["qq"]) == str(raw["self_id"])
                for s in raw["message"]
            )
            if self.config["login"] and at_bot and plain(raw["message"]).strip().startswith("登录"):
                await event.send(event.plain_result("登录请私聊我"))
                event.stop_event()

    async def on_notice(self, raw: dict) -> None:
        if raw["notice_type"] != "group_recall":
            await self.sync.notice(raw)
            return
        # Admins taking back others' messages are not followed (design §8.7).
        if self.archives(raw["group_id"]) and raw["operator_id"] == raw["user_id"]:
            self.archive.recall(raw["group_id"], raw["message_id"], raw["user_id"])

    def archives(self, group: int) -> bool:
        if self.archive is None or group not in self.sync.groups:
            return False
        return not self.archive_groups or group in self.archive_groups
