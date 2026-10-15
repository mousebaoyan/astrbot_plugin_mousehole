# 鼠洞论坛插件

[鼠洞](https://github.com/mousebaoyan/mousehole)是四非计算机保研鼠群的论坛。这个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件把 QQ 群和论坛连起来：群成员同步到论坛，私聊 bot 登录论坛，群里正经的提问和群友的回答由大模型整理后自动收录成帖子，开奖结果发到群里。

## 安装

1. 在鼠洞后台的 bot 页创建 token，bot 的 QQ 号填机器人自己的，范围选 `read`、`act`、`sync`、`auth`、`events`，要自动收录再加 `moderate`（建标签）；在后台关联要同步的群。
2. 把这个仓库放进 AstrBot 的 `data/plugins/`，在插件配置里填论坛地址和 token。成员同步、私聊登录、个性签名、开奖结果默认都开着，可以分别关掉。
3. 自动收录默认关着。先在插件配置里填好大模型（默认 ECNU 的 `ecnu-plus`）和嵌入模型（bge-m3）的地址和密钥，用[回放](docs/replay.md)调好，再打开"自动收录"。

## 文档

- [设计](docs/design.md)
- [回放实验](docs/replay.md)：用历史群消息调自动收录
- [AGENTS.md](AGENTS.md)：参与开发的规矩

许可证：MIT
