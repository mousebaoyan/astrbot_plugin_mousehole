# 离线历史收录

历史收录（[设计](design.md)第 8.10 节）可以交给有自己大模型 API 的人跑：他那边不连论坛，收到一份论坛的快照，收出来的发帖、回复、更新都记下来；跑完把数据目录发回来，维护者导入论坛。

| 谁 | 做什么 | 见 |
| --- | --- | --- |
| 维护者 | 打包：代码、群聊的导出、收到哪了、论坛的快照、配置的模板 | 第 8 节 |
| 跑的人 | 配好模型，试跑几种模型和思考程度，选定了正式跑，随时看进度和花费 | 第 2–6 节 |
| 跑的人 | 把数据目录发回来，跑没跑完都行 | 第 7 节 |
| 维护者 | 导入论坛，合并重复的帖子；没跑完的接着跑 | 第 8 节 |

## 1. 包里有什么

| 路径 | 内容 |
| --- | --- |
| `README.md` | 先看的说明，指到这份文档 |
| `config.json` | 配置，填上 key 就能用（第 2 节） |
| `mousehole-plugin/` | 代码，命令都在这里面跑 |
| `handoff/export/*.json` | 群聊的导出，只用来收录，不要外传 |
| `handoff/runs.json`、`done.txt`、`run-*/` | 收到哪了：各段的范围、收过的批次、收了一半的批次 |
| `handoff/forum/snapshot.json` | 打包时论坛上的帖子和楼层、版块、标签，查重和接着跟进旧帖用 |

跑的时候 `handoff/` 里还会有：

| 文件 | 内容 |
| --- | --- |
| `forum/journal.jsonl` | 离线发出的每一个写操作（发帖、回复、更新、表情回应、短评、新标签），一行一个，导入的就是它 |
| `segments.jsonl` | 每段的结果：消息范围、模型的原始输出、token、采纳和丢掉的项 |
| `usage.jsonl` | 每次调用大模型的 token（输入、缓存、输出、思考）和花费 |

## 2. 准备

1. 装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。macOS 和 Linux 在终端里跑 `curl -LsSf https://astral.sh/uv/install.sh | sh`；Windows 在 PowerShell 里跑 `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`。装完重开一个终端。
2. 在 `mousehole-plugin/` 里打开终端（Windows 在资源管理器里进到这个文件夹，地址栏输入 `powershell` 回车），跑 `uv sync`。下面所有的命令都在这里跑，Windows 的 PowerShell 也照抄。
3. 打开包最外层的 `config.json`（模板是 [history-config.json](history-config.json)），把 `api_key` 换成你的 key。用 OpenAI 官方的，其他都不用改；用中转的见下面。

| 项 | 说明 |
| --- | --- |
| `llm.base_url` | 接口的地址，到 `/v1` 为止 |
| `llm.model` | 模型名，先用模板里的 `gpt-6-luna`，试跑时再比别的 |
| `llm.thinking` | `openai`：OpenAI 的推理模型，思考多少只看 `reasoning_effort`。别家的 OpenAI 兼容接口见设计第 4 节 |
| `llm.reasoning_effort` | 思考程度：`none`、`minimal`、`low`、`medium`、`high`、`xhigh`，各模型支持的不一样，看 OpenAI 的模型页 |
| `llm.service_tier` | `flex` 是半价，慢一些；它忙时返回 `429`，不扣钱，工具会等着再问。留空（`""`）按标准价 |
| `llm.structured_output` | `json_schema` 让模型严格按格式答。接口不支持的改成 `json_object` |
| `llm.max_tokens` | 每次最多输出多少 token，含思考。想到上限的一段切成两半重收，`status` 里记作 `split` |
| `llm.timeout_seconds` | 多久没收到新内容就断开再问。推理模型思考时什么都不传回来，要设长 |
| `prices` | 每百万 token 的价格，只用来算花费，从 [OpenAI 的价格页](https://developers.openai.com/api/docs/pricing)抄这个模型的，用 flex 就抄 Flex 那一列；`currency` 是货币符号。`cache_write` 是写进缓存的输入，GPT-5.6 以后的模型按输入价的 1.25 倍算，之前的模型填和 `input` 一样 |
| `archive` | 一段多少条消息、跟进多久，不用改 |

- **用中转**（OpenAI 兼容的第三方接口）：`base_url` 填中转给的地址，`model` 填它那边的名字，`prices` 按中转的价格填；它不支持 `flex` 的，`service_tier` 留空。中转不一定返回 token 数，试跑完看 `status` 里有没有，没有的话花费只能去中转的后台看。
- **要代理才连得上的**：先在终端里设好 `HTTPS_PROXY`，比如 macOS 和 Linux 上 `export HTTPS_PROXY=http://127.0.0.1:7890`，PowerShell 里 `$env:HTTPS_PROXY="http://127.0.0.1:7890"`，端口按你的代理软件填。
- 不用嵌入模型：查重只按字面找，同一个问题换了说法的会另开新帖，导入后由维护者合并。

## 3. 试跑

先拿 30 段（约 1,500 条消息）比较几种模型和思考程度，用模板的模型每种大约一两块钱。每试一种复制一份 `handoff/`，在副本上试，不要在 `handoff/` 本身上试。比如先试模板的配置：

```bash
cp -r ../handoff ../try-luna-low
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../try-luna-low --workers 1 --segments 30
uv run python -m tools.history status ../handoff/export/legacy.json --data ../try-luna-low
uv run python -m tools.history show ../handoff/export/legacy.json --data ../try-luna-low --out ../try-luna-low.md
```

再复制一份 `../try-luna-medium`，把 `config.json` 里的 `reasoning_effort` 改成 `medium`，同样跑这三条（`try-luna-low` 换成 `try-luna-medium`）。还想比别的模型，就再改 `model` 和 `prices` 试一份。

- 从同一份 `handoff/` 复制出来、`--workers 1` 跑的，收的是同样的那几段（3 月下旬），几份可以直接比。想看 6 月的，三条命令的第二条加上 `--from "2026-06-21 04:00"`。
- `show` 把这次发出的帖子排成 Markdown：新帖整个列出，论坛上已有的帖子只列新加的楼层，每一层下面是它收进的原消息，作者是 QQ 号。对照[回放实验](replay.md)第 5.2 节的表看：收错、漏收、记错人、挂错、改了意思、拆错、补充没进去、标题、版块和标签。
- `status` 写着花了多少、每千条消息多少钱、照这样收完剩下的要多少，以及丢掉的项各几个；`split` 多说明想得太长，降低思考程度或者换模型。
- 怎么选：有"改了意思"或"记错人"的不选，这两种最要紧；剩下的效果差不多就选便宜的。
- 每段都 `failed`、跑的时候一直报错的，是配置不对，见第 6 节。失败的段会跳过、当作收过了，所以改好配置以后重新复制一份再试，也不要拿没试过的配置直接在 `handoff/` 上跑。
- 定下来以后把 `config.json` 改成选定的那种，把试跑的目录删掉。

## 4. 正式跑

```bash
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../handoff --workers 10 --from "2026-06-01 04:00"
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../handoff --workers 10
```

先跑第一条，收 6 到 8 月剩下的，它自己停了再跑第二条，收 3 到 5 月。`--workers` 是同时收几批，10 个就够；API 限流得厉害就调小。

每收完一段打印一行：

```text
run 3 group 100200300 06-21 09:12: 4 done, 0 dropped; 1,812 of 57,740 messages, 9.5 h to go; $0.42 (1.2M in (0.4M cached), 2.9M out), about $13.40 for all
```

依次是：哪一段、这一段的时间，发了几项、丢了几项；这次跑收了多少条、一共要收多少条、照这样还要多久；这次花了多少钱、用了多少 token，照这样收完要多少钱。

- 随时可以 Ctrl-C 停下，同样的命令再跑就接着收。电脑睡眠、断网了也一样，重跑同一条命令就行。跑的时候别让电脑睡眠。
- 同一个目录一次只跑一条 `run`；`status` 和 `show` 跑着的时候也能用。
- 中间换模型、改思考程度也行，`status` 按模型分开记；换之前先在副本上试几段。
- 一段接一段地报错、`done` 一直是 0 的，马上 Ctrl-C，照第 6 节改配置，不然后面的都会被跳过。
- 不时拿 API 后台的用量和 `status` 对一下。

## 5. 看进度

```bash
uv run python -m tools.history status ../handoff/export/legacy.json --data ../handoff
```

列出一共收了多少条、每个月收了多少，哪些天收了一半、哪些天还没收；每个模型调用了几次、多少 token、花了多少；离线发出了多少新帖、回复、更新和表情回应。

## 6. 出了问题

报错里有接口返回的原文，按它找原因：

| 看到的 | 原因 | 怎么办 |
| --- | --- | --- |
| `Waiting 10 minutes` 后面是 `HTTP 401` | key 不对 | 改 `api_key`，Ctrl-C 后重跑 |
| `Waiting 10 minutes` 后面是 `HTTP 429` 和 `insufficient_quota` | 余额用完了 | 充值，它会自己接着收 |
| `Waiting 10 minutes` 后面是 `HTTP 429`，没有 `insufficient_quota` | 限流，或者 flex 一直忙 | 等着就行；一直这样就调小 `--workers`，或者 `service_tier` 留空 |
| `Waiting 10 minutes` 后面是 `ConnectError`、`ConnectTimeout` | 连不上接口 | 查 `base_url`、网络和代理（第 2 节） |
| `HTTP 404`，或者 `model_not_found` | 模型名不对 | 改 `model` |
| `HTTP 400`，写着 `service_tier` | 不支持 flex | `service_tier` 留空 |
| `HTTP 400`，写着 `reasoning_effort` | 这个模型没有这一档 | 换一档 |
| `HTTP 400`，写着 `response_format` 或 `json_schema` | 不支持严格格式 | `structured_output` 改成 `json_object` |
| `HTTP 400`，写着 `max_completion_tokens` | 输出上限超过了这个模型的 | 调小 `max_tokens` |
| `status` 里 token 都是 0 | 接口不返回用量 | 花费去 API 后台看 |

别的报错，把终端里的报错和 `status` 的输出发给给你包的人。

## 7. 发回来

停下以后，把 `handoff/` 整个压缩发回来，附上 `status` 的输出。`config.json` 不要发，里面有你的 key。没跑完也一样发：维护者先导入收完的部分，剩下的接着跑。

## 8. 维护者

### 打包

```bash
uv run python -m tools.handoff pack data/legacy/legacy.json --config data/history-prod.json --data data/history-legacy --out ../mousehole-history
```

打出 `../mousehole-history/` 和它的 zip，发 zip 就行：

- 代码：`HEAD` 的，所以先提交。最外层放上 `README.md` 和 [history-config.json](history-config.json) 复制的 `config.json`。
- 导出：原样复制。
- 收到哪了：`runs.json`、`done.txt`，各段的 `archive.json` 和收了一半的批次。
- 快照：论坛上的所有帖子和楼层（Markdown 原文）、版块、标签；还有剩下的消息引用了、已经收进论坛的那些消息在哪一层，离线时照样能接着跟进那些帖子。用 bot 的 token 只读，要十分钟左右。
- 已经是离线收录的目录不再打包，要转交就直接发它。

### 导入

```bash
uv run python -m tools.handoff import data/legacy/legacy.json --config data/history-prod.json --data 发回来的/handoff
```

- 先给论坛还不认识的退群的人建账号，用导出里的名字（设计第 8.10 节）。
- `journal.jsonl` 按顺序发到论坛：离线时的帖子和楼层 ID（从 10^12 起）换成论坛上的；幂等键、来源和群里发出的时间照原样带。
- 每导一条记一行 `forum/imported.jsonl`，中途停了再跑接着导，导过的不会再发。
- 论坛不收的跳过，跟着它的一起跳过（比如帖子没发成，里面的回复），最后按原因列出来：成员关了群聊收录（`proxy_disabled`）、退群的人改标题（`member_frozen`）、帖子删了等。回复的那一层没了的，不带引用照发；帖子被合并了的，发到合并后留下的帖子里。
- 导入以后用 `tools/duplicates.py` 找重复的帖子合并（设计第 8.10 节）。离线没配嵌入模型时只按字面查重：以前收录时靠检索并进旧帖的提问，只按字面能找回四成，剩下的都另开了新帖，要靠这一步合并。

### 接着跑

发回来的目录就是现在的进度。剩下的在同一个目录里接着离线跑，换成自己的配置，可以加上 `embedding`（和插件的一样，bge-m3、门槛 0.5），按意思查重；跑完再导入，已经导入的不会再发。不能拿它连论坛在线跑：它跟进的帖子有离线发的，论坛上没有。

### 和在线收录的不同

- 论坛是打包时的快照，只有这次离线收的会往里加：这期间别人在论坛上发的帖子查重时找不到，论坛上的合并和删除到导入时才知道。
- 字面查重用标题和各层正文的两字重合度（`tools/fake_forum.py`），代替论坛的 `/search/similar`。
- 配了嵌入模型时，帖子的向量索引存在 `threads.npz`，可以随时删，下次从快照重建。
- 导出里有图片的不能离线收：图片传不进快照，导入时也就没有。
