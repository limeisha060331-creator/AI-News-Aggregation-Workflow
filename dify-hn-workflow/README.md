@

配套 Dify 工作流的第一步：**定时触发 → HTTP 请求 → 代码节点 → 结构化列表**。

| 文件 | 说明 |
| --- | --- |
| `code_node_parse_hn.py` | 节点代码，直接粘进 Dify 代码节点即可 |
| `test_code_node_parse_hn.py` | 45 条单元测试，覆盖空值、去重、时间格式化 |

## 1. 定时触发

在 Dify 工作流的「开始」节点之前挂一个定时任务，每天 08:00 运行。

## 2. HTTP 请求节点

推荐用 Algolia 接口，**一次请求就返回完整列表**，代码节点无需联网：

```
GET https://hn.algolia.com/api/v1/search?tags=story&numericFilters=points%3E100&hitsPerPage=30
```

官方 Firebase 接口（`topstories.json`）只返回 id 列表，需要再逐个请求详情，
在代码节点里做不到（沙箱没有网络），所以要采官方接口就得配合迭代节点。

## 3. 代码节点

把 `code_node_parse_hn.py` 全文粘进代码节点，然后按下表声明变量。

输入变量：

| 变量名 | 类型 | 必填 | 默认值 | 映射 |
| --- | --- | --- | --- | --- |
| `hn_json` | String | 是 | — | HTTP 节点的 `body` |
| `tz_offset_hours` | Number | 否 | 8 | 北京时间填 8，UTC 填 0 |
| `time_format` | String | 否 | `%Y-%m-%d %H:%M` | strftime 格式 |
| `max_items` | Number | 否 | 0（不限） | 只想取前 N 条时填 N |

输出变量：

| 变量名 | 类型 | 说明 |
| --- | --- | --- |
| `stories` | Array[Object] | `title` / `url` / `time` / `source`，另附 `id`、`author`、`score`、`comments`、`text`、`time_iso`、`time_unix` |
| `count` | Number | 条数 |
| `skipped` | Number | 因缺标题或结构非法被丢弃的条数 |
| `deduplicated` | Number | 被去重合并掉的条数 |
| `error` | String | 解析异常原因，正常为空 |

## 4. 手动运行

点「运行」，输出面板会直接展开 `stories` 数组。示例结果（输入 5 条，含 1 条重复、1 条空标题）：

```json
{
  "stories": [
    {
      "title": "Show HN: I built a tiny workflow engine",
      "url": "https://example.com/posts/workflow?utm_source=hn&utm_medium=social",
      "time": "2026-10-07 07:40",
      "source": "example.com",
      "id": "41234567",
      "author": "alice",
      "score": 321,
      "comments": 87,
      "text": "",
      "time_iso": "2026-10-06T23:40:05Z",
      "time_unix": 1791330005
    },
    {
      "title": "Ask HN: How do you keep up with AI news?",
      "url": "https://news.ycombinator.com/item?id=41234568",
      "time": "2026-10-07 08:40",
      "source": "Hacker News",
      "id": "41234568",
      "author": "bob",
      "score": 56,
      "comments": null,
      "text": "每天信息太多，想知道大家用什么方式筛选。",
      "time_iso": "2026-10-07T00:40:05Z",
      "time_unix": 1791333605
    },
    {
      "title": "Rust 1.90 released",
      "url": "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
      "time": "",
      "source": "blog.rust-lang.org",
      "id": "",
      "author": "",
      "score": 410,
      "comments": null,
      "text": "",
      "time_iso": "",
      "time_unix": null
    }
  ],
  "count": 3,
  "skipped": 1,
  "deduplicated": 1,
  "error": ""
}
```

## 本地跑测试

```bash
cd dify-hn-workflow
D:\anaconda\python.exe -m unittest test_code_node_parse_hn -v
```

也可以直接运行模块看示例输出：

```bash
D:\anaconda\python.exe code_node_parse_hn.py
```

## 处理规则

**空值**

- 没有标题的条目直接丢弃（计入 `skipped`）。
- `url` 为 null 时用 `https://news.ycombinator.com/item?id={id}` 回填，来源记为 `Hacker News`（Ask HN 这类文本贴）。
- 既没标题也没链接和 id 的条目丢弃；`time` 解不出来时留空字符串而不是报错。

**去重**

- 按归一化 URL 去重：小写域名、去掉 `www.` / `m.` / `amp.` 前缀、去掉尾斜杠、剔除 `utm_*`、`fbclid`、`ref` 等跟踪参数。
- 保留先出现的那条（对 HN 榜单就是排名更高的），只从重复项里补缺失字段。
- 解析不了时间、分数、评论数时不抛异常，分别留空 / null。

**时间**

- 支持 Unix 秒、毫秒、微秒、纳秒，数字字符串，以及 ISO 8601（带 `Z`、带时区偏移、无时区、纯日期）。
- 无时区信息的 ISO 串按 UTC 处理。
- 默认输出北京时间 `2026-10-07 07:40`，同时给出机器可读的 `time_iso`（UTC）与 `time_unix`。

## 在既有工作流里的位置

仓库里的 `dify-ai-news-v1.yml` 已经接入了这个节点，主链路是：

```
start_node -> http_hn -> code_hn -> http_openai -> http_qbitai -> code_parse
           -> code_semantic -> llm_sum -> code_build -> end_node
```

- `code_hn` 就是本文档这个节点，输入 `http_hn.body`，输出 `stories` 等五个变量。
- `code_parse` 不再自己解析 HN，改成用 `map_hn()` 消费 `code_hn.stories`，
  再和两个 RSS 源合并、跨源去重、按历史已推送 URL 过滤，并输出北京日期 `run_date`。
- `code_semantic`（节点名「语义去重」）吃 `code_parse.items` 和开始节点传进来的
  `history_items`，用向量相似度拦「同一事件、不同链接」的重复，见下节。
- `llm_sum`（节点名「生成日报」）吃 `code_semantic.items_json` 和 `code_parse.run_date`，
  直接产出中文 Markdown 日报：今日头条 / 重要进展 / 值得关注 / 链接汇总，每条带 1-5 重要性评分。
  提示词只有一份来源：[`prompts/llm_daily_report_prompt.md`](../prompts/llm_daily_report_prompt.md)。
- `code_build`（节点名「组装日报」）不再解析 LLM 的 JSON，改用 LLM 写好的 Markdown：
  剥掉推理块和代码围栏、抽出正文里真正引用到的链接交给调度器做跨天去重；
  LLM 输出不可用时按来源权重保底渲染一份，宁可朴素也不推空消息。
  另外回一份 `pushed_items`（链接 + 标题 + 来源），调度脚本靠它把历史写进本地库。
- 工作流到 `end_node` 就结束了，**不管推送**。推送由 `scheduler/run_daily.py` 走 PushPlus 发微信：
  Dify 的 HTTP 节点只看 HTTP 状态码，飞书这类接口业务失败时依然返回 200，放在工作流里会「假成功」。

## HTTP 节点必须带浏览器 UA

三个抓取节点的 `headers` 都写死了浏览器 User-Agent，不是装饰：

```
User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
```

Dify 的 HTTP 节点默认发 `python-httpx/x.y`，量子位的 CDN 会直接回 **403 + 空 body**。
这种失败很难查：节点状态是 `succeeded`，`code_parse` 只是安静地少一个源
（`qbitai_raw: 0`、`missing_sources: ["量子位(0)"]`），日报照样推出去。
从容器里换个 UA 请求同一个地址就是 200，所以问题不在网络、不在 feed 地址。
改抓取地址后如果某个源长期 0 条，先看这条。

结束节点的 `outputs` 也要和调度脚本对齐：`markdown` / `count` / `pushed_urls` /
`pushed_items` / `stats` / `semantic_stats` 一个都不能少。节点跑成功但字段没导出时，
调度脚本只会拿到 `None`，日志里悄悄缺数——`validate_dsl.py` 现在会静态拦这一条。

## 语义去重（RAG）：现状与升级路径

`code_semantic` 的位置在 URL 去重之后、写日报之前。它解决的是 URL 去重抓不到的一类重复：
同一个事件、不同链接（HN 讨论页指向官方博客，中英文两家媒体报道同一件事）。

算法本身是标准的向量检索那套：编码成向量 -> 与历史候选比余弦 -> 取 top_k=5 ->
超阈值判重丢弃，落在疑似区间的打标记交 `llm_sum` 裁决。同源条目阈值更严，
避免同一家媒体的连载被误判成重复。

**编码器是可插拔的，这一点决定了它现在能干什么。** 默认用文件里的本地词法编码器，
跑 `python dify-hn-workflow/code_node_semantic_dedup.py` 可以复现标定数据：

| 场景 | 实测相似度 | 词法编码器能不能判出来 |
| --- | --- | --- |
| 同语言近重复（中-中、英-英改写） | 0.84 ~ 1.00 | 能 |
| 跨语言同事件（英文原文 vs 中文报道） | 0.07 ~ 0.30 | 不能 |
| 不同事件但同源（Rust 1.90 vs 1.91） | 0.67 | 不会误判，落在疑似区以下 |

也就是说：只靠本地词法编码器，跨语言那一类抓不到，会漏。要真正覆盖它，需要接一个
embedding 模型。节点已经留好了入口，不用改判重逻辑：

1. 在 `code_semantic` 前面加一个 HTTP 节点，调你们的 embedding 接口，把结果整理成
   `{"<url>": [浮点数, ...]}`；
2. 把这段 JSON 传给 `code_semantic` 的 `embeddings` 入参；
3. 命中的条目自动改用模型向量，没命中的继续用词法编码器兜底。

跨语言那一类的重复，目前由 `llm_sum` 的「同一件事只保留信息最完整的一条」规则兜底，
两者互补，所以在接模型之前也不会漏推、只是偶尔会重复占版面。

三处「单一来源」由脚本强制对齐，改完代码或提示词后依次跑：

```bash
D:\anaconda\python.exe tools\sync_hn_node.py        # code_hn 代码
D:\anaconda\python.exe tools\sync_semantic_node.py  # code_semantic 代码
D:\anaconda\python.exe tools\sync_llm_prompt.py     # llm_sum 提示词
D:\anaconda\python.exe tools\validate_dsl.py        # 校验有没有漂移
```

校验会检查 YAML 结构、边与变量引用（含 LLM 提示词里的 `{{#节点.变量#}}` 占位符）、主链路连通性、
代码节点 `main()` 的参数名是否和输入变量对得上，比对上面三处单一来源，并真实执行四个 Code 节点
（含 LLM 跑偏时的保底渲染）。它还会拿一批真实 URL 逐条比对 `code_parse` 与
`tools/news_store.py` 的归一化结果——两边算出来的去重 key 不一致，跨天去重会整个失效。

## 状态存哪：本地库，不用飞书多维表格

| 文件 | 作用 |
| --- | --- |
| `scheduler/state/news.db` | 权威存储：已推送条目的 URL、标题、来源、日期 |
| `scheduler/state/history.json` | 同上的人类可读镜像，方便直接打开翻 |
| `scheduler/logs/*.json` | 每次运行的完整结果，周报的数据源 |

调度脚本每次运行读库，把 `history_urls`（URL 列表）和 `history_items`（带标题）传给工作流；
**只有推送成功才写回**，推送失败时条目故意不落库，下次重抓一遍，避免漏掉一整天的资讯。

不用飞书多维表格的原因：Dify 的 HTTP 节点只看状态码，多维表格业务失败也返 200，状态会被写脏；
还要多管一套应用凭证和 QPS；最关键的是「写回」绑不到「真的推送成功」这个事件上。

```bash
python tools/news_store.py --stats        # 看窗口内按天按源的统计
python tools/news_store.py --prune        # 清理历史窗口外的记录
python tools/weekly_report.py --days 7    # 出周报：漏斗、源质量、阈值评估
```
