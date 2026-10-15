"""AstrBot entry: listens to NapCat's events and runs the plugin's background work."""

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .mousehole.archive import Archive, Message, Tuning, plain
from .mousehole.events import Events
from .mousehole.forum import Forum
from .mousehole.judge import OneJev
from .mousehole.llm import LLM, Embedder, Settings
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
        self.llm: LLM | None = None
        self.embedder: Embedder | None = None
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
        self.archive_groups = {int(g) for g in c["archive"]["groups"]}
        o, e, m = c["onejev"], c["embedding"], c["llm"]
        if c["archive"]["enabled"] and not all(
            (o["url"], m["base_url"], m["model"], e["base_url"], e["model"])
        ):
            # The rest of the plugin works on; only archiving waits for its settings.
            logger.error(
                "鼠洞插件：自动收录要填好 OneJev、大模型和嵌入模型的地址和模型，现在不收录"
            )
        elif c["archive"]["enabled"]:
            self.onejev = OneJev(o["url"], o["api_key"], o["model"])
            self.llm = LLM(Settings.of(m))
            self.embedder = Embedder(e["base_url"], e["api_key"], e["model"])
            index = Index(
                self.forum,
                self.embedder.embed,
                f"{e['base_url']} {e['model']}",
                data / "threads.npz",
                e["min_similarity"],
            )
            self.archive = Archive(
                self.forum,
                self.onejev,
                self.llm.complete,
                index,
                prefixes=self.context.get_config()["wake_prefix"],
                tuning=Tuning.of(c),
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
        # When AstrBot starts, plugins load before the platforms; installed or reloaded later,
        # the platform is already there.
        if self.qq.found():
            self.listen()

    async def start(self, index: Index | None) -> None:
        # Groups are known before any event or message is acted on.
        await self.sync.refresh_groups()
        self.tasks.append(asyncio.create_task(self.sync.run()))
        if self.events.types:
            self.tasks.append(asyncio.create_task(self.events.run()))
        if self.archive and index:
            await index.load()
            self.tasks.append(asyncio.create_task(self.archive.run()))

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        if self.forum and self.qq.found():
            self.listen()

    def listen(self) -> None:
        # AstrBot's session allowlist runs before any plugin and would drop the private messages
        # of members not on it, so these come straight from aiocqhttp.
        if self.config["login"]:
            self.qq.platform().bot.subscribe("message.private", self.on_private)

    async def on_private(self, raw: dict) -> None:
        text = plain(raw["message"])
        if not wants_login(text):
            return
        try:
            await self.qq.reply(raw, await login(self.forum, raw["user_id"], text))
        except Exception:
            logger.exception("Replying to a private login failed")

    async def terminate(self) -> None:
        if self.forum and self.qq.found():
            self.qq.platform().bot.unsubscribe("message.private", self.on_private)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for client in (self.forum, self.onejev, self.llm, self.embedder):
            if client:
                await client.close()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent):
        raw = event.message_obj.raw_message
        if self.forum is None or event.platform_meta.id != self.qq.platform().meta().id:
            return
        if raw["post_type"] == "notice":
            await self.on_notice(raw)
        elif raw["post_type"] == "message" and raw["message_type"] == "private":
            # Answered in on_private; here only kept from AstrBot's chat model.
            if self.config["login"] and wants_login(plain(raw["message"])):
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
                event.stop_event()
                await self.qq.reply(raw, "登录请私聊我")

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
