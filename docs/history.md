# 离线历史收录

历史收录（[设计](design.md)第 8.10 节）可以交给有自己大模型 API 的人跑：他那边不连论坛，收到一份论坛的快照，收出来的发帖、回复、更新都记下来；跑完把数据目录发回来，维护者导入论坛。

| 谁 | 做什么 | 见 |
| --- | --- | --- |
| 维护者 | 打包：群聊的导出、收到哪了、论坛的快照 | 第 7 节 |
| 跑的人 | 配好模型，试跑几种模型和思考程度，选定了正式跑，随时看进度和花费 | 第 2–5 节 |
| 跑的人 | 把数据目录发回来，跑没跑完都行 | 第 6 节 |
| 维护者 | 导入论坛，合并重复的帖子；没跑完的接着跑 | 第 7 节 |

## 1. 包里有什么

| 路径 | 内容 |
| --- | --- |
| `mousehole-plugin/` | 插件的代码 |
| `handoff/export/*.json` | 群聊的导出，只用来收录，不要外传 |
| `handoff/runs.json`、`done.txt`、`run-*/` | 收到哪了：各段的范围、收过的批次、收了一半的批次 |
| `handoff/forum/snapshot.json` | 打包时论坛上的帖子和楼层、版块、标签，查重和接着跟进旧帖用 |

跑的时候 `handoff/` 里还会有：

| 文件 | 内容 |
| --- | --- |
| `forum/journal.jsonl` | 离线发出的每一个写操作（发帖、回复、更新、表情回应、短评、新标签），一行一个，导入的就是它 |
| `segments.jsonl` | 每段的结果：消息范围、模型的原始输出、token、采纳和丢掉的项 |
| `usage.jsonl` | 每次调用大模型的 token（输入、缓存、输出、思考）和花费 |
| `threads.npz` | 帖子的向量索引，可以随时删，下次从快照重建 |

## 2. 准备

- 装 [uv](https://docs.astral.sh/uv/)，在 `mousehole-plugin/` 里 `uv sync`。下面的命令都在这个目录里跑，路径按实际的写。
- 大模型的 API key。
- 嵌入模型要用 bge-m3：查重的门槛是按它调的，换了模型相似度对不上。不用自己跑，[硅基流动](https://www.siliconflow.cn/pricing)的 `BAAI/bge-m3` 免费，注册拿一个 key 就行；也可以本机装 Ollama，`ollama pull bge-m3`，地址填 `http://127.0.0.1:11434/v1`、模型填 `bge-m3`。

配置写在 `config.json`，放在 `handoff/` 外面，有 key，不要发回来：

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-……",
    "model": "gpt-6-luna",
    "structured_output": "json_schema",
    "thinking": "openai",
    "reasoning_effort": "low",
    "service_tier": "flex",
    "max_tokens": 65536,
    "timeout_seconds": 900
  },
  "prices": {
    "currency": "$",
    "input": 0.05,
    "cached_input": 0.005,
    "cache_write": 0.0625,
    "output": 0.25
  },
  "embedding": {
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": "sk-……",
    "model": "BAAI/bge-m3",
    "min_similarity": 0.5
  },
  "archive": { "segment_messages": 50, "follow_messages": 200 }
}
```

| 项 | 说明 |
| --- | --- |
| `llm.thinking` | `openai`：OpenAI 的推理模型，思考多少只看 `reasoning_effort`，输出上限按 `max_completion_tokens` 传。别家的 OpenAI 兼容接口见设计第 4 节 |
| `llm.reasoning_effort` | `none`、`minimal`、`low`、`medium`、`high`、`xhigh`，各模型支持的不一样，看 OpenAI 的模型页 |
| `llm.service_tier` | `flex` 按 Batch 的价格算，一半的钱，慢一些；它忙时返回 `429`，不扣钱，工具会等着再问。空的不传，按标准价 |
| `llm.max_tokens` | 每次最多输出多少 token，含思考。想到上限的一段切成两半重收，`status` 里记作 `split` |
| `llm.timeout_seconds` | 多久没收到新内容就断开再问。OpenAI 的模型思考时什么都不传回来，要设长 |
| `prices` | 每百万 token 的价格，只用来算花费，从 [OpenAI 的价格页](https://developers.openai.com/api/docs/pricing)抄这个模型的（用 flex 就抄 Flex 那一列）。`cache_write` 是写进缓存的输入，GPT-5.6 以后的模型按输入价的 1.25 倍算，之前的模型没有，填和 `input` 一样就行 |
| `archive` | 一段多少条消息、跟进多久，不用改 |

## 3. 试跑

先拿几十段比较几种模型和思考程度。每试一种复制一份数据目录，不要在 `handoff/` 本身上试：

```bash
cp -r ../handoff ../try-luna-low
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../try-luna-low --workers 1 --segments 30
uv run python -m tools.history status ../handoff/export/legacy.json --data ../try-luna-low
uv run python -m tools.history show ../handoff/export/legacy.json --data ../try-luna-low --out ../try-luna-low.md
```

- `--workers 1` 时从同一份 `handoff/` 复制出来的，收的是同样的那几段，几种模型可以直接比。想试 6 月的，加上 `--from "2026-06-21 04:00"`。
- `show` 把这次发出的帖子排成 Markdown：新帖整个列出，论坛上已有的帖子只列新加的楼层，每一层下面是它收进的原消息。作者是 QQ 号。对照[回放实验](replay.md)第 5.2 节的表看：收错、漏收、记错人、挂错、改了意思、拆错、补充没进去、标题、版块和标签。"改了意思"和"记错人"最要紧。
- `status` 写着这个模型花了多少、每千条消息多少钱、照这样收完剩下的要多少，以及丢掉的项各几个；`split` 多说明想得太长，降低思考程度或者换模型。
- 定下来以后把试跑的目录删掉。

## 4. 正式跑

```bash
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../handoff --workers 10 --from "2026-06-01 04:00"
uv run python -m tools.history run ../handoff/export/legacy.json --config ../config.json --data ../handoff --workers 10
```

先带 `--from` 收完 6 到 8 月，再不带它收 3 到 5 月。`--workers` 是同时收几批，按 API 的限流定，10 个就够。

每收完一段打印一行：

```text
run 3 group 100200300 06-21 09:12: 4 done, 0 dropped; 1,812 of 57,740 messages, 9.5 h to go; $0.42 (1.2M in (0.4M cached), 2.9M out), about $13.40 for all
```

依次是：哪一段、这一段的时间，发了几项、丢了几项；这次跑收了多少条、一共要收多少条、照这样还要多久；这次花了多少钱、用了多少 token，照这样收完要多少钱。

- 随时可以 Ctrl-C 停下，同样的命令再跑就接着收。中间换模型、改思考程度也行，`status` 按模型分开记。
- 一直出现 `Waiting 10 minutes`：大模型或嵌入模型的接口不通，修好了会自己接着收；后面带着 `insufficient_quota` 的是余额用完了。
- 不时拿 OpenAI 后台的用量和 `status` 对一下。

## 5. 看进度

```bash
uv run python -m tools.history status ../handoff/export/legacy.json --data ../handoff
```

列出一共收了多少条、每个月收了多少，哪些天收了一半、哪些天还没收；每个模型调用了几次、多少 token、花了多少；离线发出了多少新帖、回复、更新和表情回应。跑着的时候也能看。

## 6. 发回来

停下以后，把 `handoff/` 整个打包发给维护者，附上 `status` 的输出。`threads.npz` 可以不带，`config.json` 不要带。没跑完也一样发：维护者先导入收完的部分，剩下的接着跑。

## 7. 维护者

### 打包

```bash
uv run python -m tools.handoff pack data/legacy/legacy.json --config data/history-prod.json --data data/history-legacy --out ../handoff
git archive HEAD --prefix mousehole-plugin/ -o ../mousehole-plugin.tar
```

- 导出：原样复制。
- 收到哪了：`runs.json`、`done.txt`，各段的 `archive.json` 和收了一半的批次。
- 快照：论坛上的所有帖子和楼层（Markdown 原文）、版块、标签；还有剩下的消息引用了、已经收进论坛的那些消息在哪一层，离线时照样能接着跟进那些帖子。用 bot 的 token 只读，要几分钟。
- 已经是离线收录的目录不再打包，要转交就直接发它。

### 导入

```bash
uv run python -m tools.handoff import data/legacy/legacy.json --config data/history-prod.json --data 发回来的/handoff
```

- 先给论坛还不认识的退群的人建账号，用导出里的名字（设计第 8.10 节）。
- `journal.jsonl` 按顺序发到论坛：离线时的帖子和楼层 ID（从 10^12 起）换成论坛上的；幂等键、来源和群里发出的时间照原样带。
- 每导一条记一行 `forum/imported.jsonl`，中途停了再跑接着导，导过的不会再发。
- 论坛不收的跳过，跟着它的一起跳过（比如帖子没发成，里面的回复），最后按原因列出来：成员关了群聊收录（`proxy_disabled`）、退群的人改标题（`member_frozen`）、帖子删了等。回复的那一层没了的，不带引用照发；帖子被合并了的，发到合并后留下的帖子里。
- 导入以后用 `tools/duplicates.py` 找重复的帖子合并（设计第 8.10 节）。

### 接着跑

发回来的目录就是现在的进度。剩下的在同一个目录里接着离线跑（换成自己的配置），跑完再导入，已经导入的不会再发。不能拿它连论坛在线跑：它跟进的帖子有离线发的，论坛上没有。

### 和在线收录的不同

- 论坛是打包时的快照，只有这次离线收的会往里加：这期间别人在论坛上发的帖子查重时找不到，论坛上的合并和删除到导入时才知道。
- 字面查重用标题和各层正文的两字重合度（`tools/fake_forum.py`），代替论坛的 `/search/similar`。
- 导出里有图片的不能离线收：图片传不进快照，导入时也就没有。
