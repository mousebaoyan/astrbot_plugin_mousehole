# 鼠洞论坛插件

[鼠洞](https://github.com/mousebaoyan/mousehole)是四非计算机保研鼠群的论坛。这个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件把 QQ 群和论坛连起来：群成员同步到论坛，私聊 bot 登录论坛，群里正经的提问和群友的回答由大模型整理后自动收录成帖子，开奖结果发到群里。

## 安装

1. 在鼠洞后台的 bot 页创建 token，bot 的 QQ 号填机器人自己的，范围选 `read`、`act`、`sync`、`auth`、`events`；在后台关联要同步的群。
2. 把这个仓库放进 AstrBot 的 `data/plugins/`，在插件配置里填论坛地址和 token。
3. 要自动收录，再配好 [OneJev](https://github.com/OmniJev/OneJev)、AstrBot 里的大模型和嵌入模型（bge-m3），填进插件配置。

## 文档

- [设计](docs/design.md)
- [回放实验](docs/replay.md)：用历史群消息调自动收录
- [AGENTS.md](AGENTS.md)：参与开发的规矩

许可证：MIT
