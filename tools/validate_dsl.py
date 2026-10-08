"""校验 dify-ai-news-v1.yml：YAML 结构、节点连线，以及三个 Code 节点的 Python 逻辑。

两处「单一来源」在这里强制对齐，避免改了一边忘了另一边：
  - code_hn 的内嵌代码 == dify-hn-workflow/code_node_parse_hn.py（去掉演示数据段）
  - llm_sum 的提示词   == prompts/llm_daily_report_prompt.md

用法：D:\\anaconda\\python.exe tools\\validate_dsl.py
"""

import json
import os
import re
import sys

import yaml

import sync_llm_prompt
import news_store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSL_PATH = os.path.join(BASE_DIR, "dify-ai-news-v1.yml")
HN_MODULE_PATH = os.path.join(
    BASE_DIR,
    "dify-hn-workflow",
    "code_node_parse_hn.py",
)
SEMANTIC_MODULE_PATH = os.path.join(
    BASE_DIR,
    "dify-hn-workflow",
    "code_node_semantic_dedup.py",
)
PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "llm_daily_report_prompt.md")

# dify-hn-workflow/code_node_parse_hn.py 里这一段之后是本地演示数据，不进入 DSL
DEMO_MARKER = "# ---------------------------------------------------------------- 手动运行示例数据"

FAKE_HN = {
    "hits": [
        {
            "title": "GPT-5 released",
            "url": "https://openai.com/index/gpt-5/?utm_source=hn",
            "objectID": "1",
            "points": 420,
            "created_at": "2026-10-06T10:00:00Z",
            "story_text": None,
        },
        {
            "title": "GPT-5 released again",
            "url": "https://openai.com/index/gpt-5/",
            "objectID": "2",
            "points": 90,
            "created_at": "2026-10-06T11:00:00Z",
        },
        {
            "title": "Show HN: my thing",
            "url": "https://example.com/thing",
            "objectID": "3",
            "points": 150,
            "created_at": "2026-10-06T12:00:00Z",
        },
    ]
}

FAKE_RSS_OPENAI = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Introducing GPT-5</title><link>https://openai.com/index/gpt-5/</link>
<description>&lt;p&gt;We are releasing GPT-5 today.&lt;/p&gt;</description>
<pubDate>Mon, 06 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>New safety approach</title><link>https://openai.com/index/safety/</link>
<description>Safety first</description>
<pubDate>Mon, 06 Oct 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

FAKE_RSS_QBITAI = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>国产大模型发布新版本</title><link>https://www.qbitai.com/2026/10/ai-model-release</link>
<description>摘要文本</description>
<pubDate>Mon, 06 Oct 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""

# 真实场景里 deepseek 会把推理过程一起塞进输出，这里复现这个前缀
FAKE_REPORT = (
    "<think>\n<!--dify-deepseek-reasoning-->先按重要性分档，再写摘要。\n</think>\n\n"
    "# AI 日报 · 2026-10-07\n\n"
    "> 共 2 条 · 来源：OpenAI Blog / HN\n\n"
    "## 今日头条\n\n"
    "### 1. OpenAI 正式发布 GPT-5 · 重要性 5/5\n"
    "OpenAI 今天发布 GPT-5，推理与工具调用能力明显提升，是当天分量最重的一条。\n"
    "来源：OpenAI Blog · [原文](https://openai.com/index/gpt-5/)\n\n"
    "## 重要进展\n\n"
    "今日无\n\n"
    "## 值得关注\n\n"
    "### 2. 有人开源了一个极简工作流引擎 · 重要性 2/5\n"
    "HN 上有人开源了一个小引擎，适合想看实现思路的人。\n"
    # 故意用转义右括号的写法：模型经常写出 .../thing\) 这种链接，末尾反斜杠不能进 URL
    "来源：HN · [原文](https://example.com/thing\\)\n\n"
    "## 链接汇总\n\n"
    "- [OpenAI 正式发布 GPT-5](https://openai.com/index/gpt-5/) — OpenAI Blog · 5/5\n"
    "- [有人开源了一个极简工作流引擎](https://example.com/thing) — HN · 2/5\n"
)

# 模型跑偏时的典型输出：没有分档标题，也没有可用链接
FAKE_JUNK = "<think>我要开始写了</think>\n不好意思，我无法完成这个任务。"

PROMPT_REF_RE = re.compile(r"\{\{#([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)#\}\}")


def load_dsl():
    with open(DSL_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def check_embedded_code(node_id: str, embedded: str, module_path: str, sync_script: str) -> int:
    """确认 DSL 内嵌的代码节点与磁盘上的模块文件完全一致（去掉演示数据段）。"""
    if not os.path.exists(module_path):
        print("!! 找不到 %s，跳过内嵌代码比对" % module_path)
        return 0

    with open(module_path, encoding="utf-8") as handle:
        source = handle.read()
    expected = source[: source.index(DEMO_MARKER)] if DEMO_MARKER in source else source

    if embedded.strip() != expected.strip():
        print("!! DSL 里 %s 的内嵌代码与 %s 不一致" % (node_id, os.path.basename(module_path)))
        print("   请跑 %s 重新同步（会去掉文件末尾的演示数据段）" % sync_script)
        return 1

    print("%s 内嵌代码与 %s 一致 -> OK" % (node_id, os.path.basename(module_path)))
    return 0


def check_normalize_url_alignment(parse_namespace: dict) -> int:
    """code_parse 节点和 tools/news_store.py 的 URL 归一化必须逐条一致。

    两边算出来的 key 不一样，跨天去重就会失效：工作流里进过库的 URL，
    调度脚本按另一个 key 查，永远查不到，第二天原样再推一遍。
    """
    samples = [
        "https://openai.com/index/gpt-5/?utm_source=hn&utm_medium=social",
        "https://www.example.com/p/",
        "example.com/p/",
        "http://Example.COM/Path/?fbclid=abc&keep=1",
        "https://example.com/a?utm_source=x&utm_campaign=y#frag",
        "blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
        "",
    ]
    node_normalize = parse_namespace.get("normalize_url")
    if not node_normalize:
        print("!! code_parse 节点里找不到 normalize_url，无法校验归一化一致性")
        return 1

    for sample in samples:
        node_value = node_normalize(sample)
        store_value = news_store.normalize_url(sample)
        if node_value != store_value:
            print("!! URL 归一化不一致：%r" % sample)
            print("   code_parse  -> %r" % node_value)
            print("   news_store  -> %r" % store_value)
            return 1
    print("code_parse 与 news_store 的 URL 归一化一致（%d 个样本）-> OK" % len(samples))
    return 0


def check_embedded_prompt() -> int:
    """确认 DSL 里 llm_sum 的提示词与 prompts/llm_daily_report_prompt.md 完全一致。"""
    if not os.path.exists(PROMPT_PATH):
        print("!! 找不到 %s，无法比对提示词" % PROMPT_PATH)
        return 1

    with open(DSL_PATH, encoding="utf-8") as handle:
        dsl = handle.read()
    id_at = dsl.find(sync_llm_prompt.NODE_ID_LINE)
    header_at = dsl.rfind(sync_llm_prompt.PROMPT_HEADER, 0, id_at)
    tail_at = dsl.find(sync_llm_prompt.PROMPT_TAIL, header_at)
    if id_at < 0 or header_at < 0 or tail_at < 0:
        print("!! 在 DSL 里定位不到 llm_sum 的 prompt_template")
        return 1

    if dsl[header_at:tail_at] != sync_llm_prompt.render_prompt_block():
        print("!! DSL 里 llm_sum 的提示词与 prompts/llm_daily_report_prompt.md 不一致")
        print("   请跑 D:\\anaconda\\python.exe tools\\sync_llm_prompt.py 重新同步")
        return 1

    print("llm_sum 提示词与 prompts/llm_daily_report_prompt.md 一致 -> OK")
    return 0


def check_code_node_signatures(nodes: dict) -> int:
    """Dify 用「输入变量名」当关键字参数调用代码节点的 main()，两边名字必须一致。

    这个坑本地直接调 main() 是测不出来的，只有真的跑一遍 Dify 才会炸，
    所以在这里静态比一遍签名。
    """
    import ast

    for node_id, node in nodes.items():
        data = node["data"]
        if data.get("type") != "code":
            continue
        declared = [variable.get("variable") for variable in data.get("variables") or []]
        if not declared:
            continue
        try:
            tree = ast.parse(data.get("code") or "")
        except SyntaxError as error:
            print("节点 %s 的代码有语法错误：%s" % (node_id, error))
            return 1
        main_fn = next((item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "main"), None)
        if main_fn is None:
            print("节点 %s 找不到 main() 函数" % node_id)
            return 1
        params = {arg.arg for arg in main_fn.args.args}
        missing = [name for name in declared if name not in params]
        if missing:
            print("节点 %s 的输入变量 %s 在 main() 里没有同名参数" % (node_id, missing))
            print("   Dify 会按变量名传关键字参数，名字对不上会直接 TypeError")
            return 1
        print("节点 %s 的 main() 参数与输入变量 %s 对得上 -> OK" % (node_id, declared))
    return 0


def main() -> int:
    doc = load_dsl()
    graph = doc["workflow"]["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]

    print("YAML 解析成功")
    print("kind=%s version=%s mode=%s name=%s" % (doc.get("kind"), doc.get("version"), doc["app"]["mode"], doc["app"]["name"]))
    print("节点数=%d 边数=%d" % (len(nodes), len(edges)))
    for node_id, node in nodes.items():
        print("  %-12s %s" % (node_id, node["data"]["type"]))

    for edge in edges:
        if edge["source"] not in nodes or edge["target"] not in nodes:
            print("边的首尾节点不存在: %s" % edge)
            return 1
    print("边的首尾节点都存在 -> OK")

    order = []
    current = "start_node"
    while current and len(order) <= len(nodes):
        order.append(current)
        downstream = [edge["target"] for edge in edges if edge["source"] == current]
        if len(downstream) > 1:
            print("节点 %s 有多个下游 %s，当前校验只支持单链路" % (current, downstream))
            return 1
        current = downstream[0] if downstream else None
    if order[-1] != "end_node" or len(order) != len(nodes):
        print("主链路不完整，实际链路: %s" % " -> ".join(order))
        print("未连上的节点: %s" % sorted(set(nodes) - set(order)))
        return 1
    print("主链路 %d 个节点全部连通 -> OK" % len(order))

    for node_id, node in nodes.items():
        for variable in node["data"].get("variables") or []:
            selector = variable.get("value_selector") or []
            if not selector:
                continue
            if selector[0] not in nodes:
                print("节点 %s 的输入 %s 引用了不存在的节点 %s" % (node_id, variable.get("variable"), selector[0]))
                return 1
            outputs = nodes[selector[0]]["data"].get("outputs")
            if isinstance(outputs, dict) and len(selector) > 1 and selector[1] not in outputs:
                print(
                    "节点 %s 的输入 %s 引用了 %s 上不存在的输出 %s"
                    % (node_id, variable.get("variable"), selector[0], selector[1])
                )
                return 1
    print("节点输入变量的引用都存在 -> OK")

    # LLM 节点的提示词用 {{#node.var#}} 引用上游输出，Dify 不把它列在 variables 里，单独扫一遍
    for node_id, node in nodes.items():
        if node["data"].get("type") != "llm":
            continue
        prompt_text = "".join(part.get("text") or "" for part in node["data"].get("prompt_template") or [])
        for ref_node, ref_var in PROMPT_REF_RE.findall(prompt_text):
            if ref_node not in nodes:
                print("节点 %s 的提示词引用了不存在的节点 %s" % (node_id, ref_node))
                return 1
            outputs = nodes[ref_node]["data"].get("outputs")
            if isinstance(outputs, dict) and ref_var not in outputs:
                print("节点 %s 的提示词引用了 %s 上不存在的输出 %s" % (node_id, ref_node, ref_var))
                return 1
    print("LLM 提示词里的变量引用都存在 -> OK")

    # 结束节点决定 API 能拿到什么：节点跑成功但字段没导出，下游只会看到 None，
    # 这种错不报异常、只在运行日志里悄悄缺数，所以在这里静态拦一道
    end_outputs = nodes["end_node"]["data"].get("outputs") or []
    for output in end_outputs:
        selector = output.get("value_selector") or []
        if len(selector) != 2 or selector[0] not in nodes:
            print("结束节点的输出 %s 引用了不存在的节点 %s" % (output.get("variable"), selector))
            return 1
        declared = nodes[selector[0]]["data"].get("outputs")
        if isinstance(declared, dict) and selector[1] not in declared:
            print("结束节点的输出 %s 引用了 %s 上不存在的输出 %s"
                  % (output.get("variable"), selector[0], selector[1]))
            return 1
    required_outputs = {"markdown", "count", "pushed_urls", "pushed_items", "stats", "semantic_stats"}
    exported = {output.get("variable") for output in end_outputs}
    missing_outputs = sorted(required_outputs - exported)
    if missing_outputs:
        print("!! 结束节点缺少调度脚本要用的输出：%s" % missing_outputs)
        print("   scheduler/run_daily.py 会读这些字段，缺了只能拿到 None")
        return 1
    print("结束节点的输出引用都存在，调度脚本要的 %d 个字段齐全 -> OK" % len(required_outputs))

    if check_embedded_code(
        "code_hn", nodes["code_hn"]["data"]["code"], HN_MODULE_PATH, "tools/sync_hn_node.py"
    ) != 0:
        return 1
    if check_embedded_code(
        "code_semantic", nodes["code_semantic"]["data"]["code"], SEMANTIC_MODULE_PATH,
        "tools/sync_semantic_node.py"
    ) != 0:
        return 1
    if check_embedded_prompt() != 0:
        return 1
    if check_code_node_signatures(nodes) != 0:
        return 1

    namespace_hn = {}
    exec(compile(nodes["code_hn"]["data"]["code"], "code_hn", "exec"), namespace_hn)
    hn_result = namespace_hn["main"](json.dumps(FAKE_HN))

    print()
    print("[code_hn] 输出键: %s" % list(hn_result))
    print(
        "[code_hn] count=%s skipped=%s deduplicated=%s"
        % (hn_result["count"], hn_result["skipped"], hn_result["deduplicated"])
    )
    for story in hn_result["stories"]:
        print("   %-16s %-22s %s" % (story["time"] or "(无时间)", story["source"], story["title"][:44]))
        assert story["title"] and story["url"] and story["source"], "title/url/source 不能为空"
    assert hn_result["error"] == "", "code_hn 解析出错: %s" % hn_result["error"]
    assert hn_result["count"] == 2, "FAKE_HN 应去重成 2 条，实际 %s" % hn_result["count"]
    assert hn_result["deduplicated"] == 1, "应记录 1 条重复，实际 %s" % hn_result["deduplicated"]
    assert hn_result["stories"][0]["time"].endswith("18:00"), "10:00Z 换算北京时间应为 18:00"

    namespace = {}
    exec(compile(nodes["code_parse"]["data"]["code"], "code_parse", "exec"), namespace)
    parse_result = namespace["main"](
        hn_items=hn_result["stories"],
        openai_body=FAKE_RSS_OPENAI,
        qbitai_body=FAKE_RSS_QBITAI,
        history_urls="https://example.com/thing",
    )

    print()
    print("[code_parse] 输出键: %s" % list(parse_result))
    print("[code_parse] stats: %s" % parse_result["stats"])
    assert isinstance(parse_result["items"], list), "items 必须是 list"
    assert isinstance(parse_result["items"][0], dict), "items 元素必须是 dict"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", parse_result["run_date"] or ""), (
        "run_date 应该是北京日期 YYYY-MM-DD，实际 %r" % parse_result["run_date"]
    )
    for item in parse_result["items"]:
        print("   index=%s origin=%-12s %s  %s" % (item["index"], item["origin"], item["title"][:38], item["url"]))

    if check_normalize_url_alignment(namespace) != 0:
        return 1

    # 语义去重节点：有历史时该拦的拦住，没历史时一条都不能少，异常时整体放行
    namespace_sem = {}
    exec(compile(nodes["code_semantic"]["data"]["code"], "code_semantic", "exec"), namespace_sem)
    semantic_main = namespace_sem["main"]

    blank = semantic_main(items=parse_result["items"], history_items="")
    blank_stats = json.loads(blank["stats"])
    print()
    print("[code_semantic] 无历史：in=%d kept=%d dropped=%d"
          % (blank_stats["semantic_in"], blank_stats["semantic_kept"], blank_stats["semantic_dropped"]))
    assert blank["error"] == "", "语义去重不该报错：%s" % blank["error"]
    assert blank_stats["semantic_dropped"] == 0, "没有历史时不应判重"
    assert len(blank["items"]) == len(parse_result["items"]), "没有历史时不应丢条目"

    # 历史里放一条和当批同链接的条目，语义去重必须把它拦下来
    target = parse_result["items"][0]
    history_items = json.dumps(
        [{"url": target["url"], "title": target["title"], "origin": target.get("origin") or ""}],
        ensure_ascii=False,
    )
    filtered = semantic_main(items=parse_result["items"], history_items=history_items)
    filtered_stats = json.loads(filtered["stats"])
    print("[code_semantic] 带历史：in=%d kept=%d dropped=%d suspect=%d encoder=%s"
          % (filtered_stats["semantic_in"], filtered_stats["semantic_kept"],
             filtered_stats["semantic_dropped"], filtered_stats["semantic_suspect"],
             filtered_stats["semantic_encoder"]))
    for row in filtered_stats["dropped_detail"]:
        print("   判重 %s  <- %s (%.3f, %s)"
              % (row["title"][:34], row["dup_of"], row["score"], row["dup_scope"]))
    assert filtered_stats["semantic_dropped"] == 1, (
        "历史里已有同一条，应该恰好拦下 1 条，实际 %d" % filtered_stats["semantic_dropped"]
    )
    assert len(filtered["items"]) == len(parse_result["items"]) - 1
    assert target["url"] not in [item.get("url") for item in filtered["items"]], "被拦下的条目还在列表里"

    # 传了真 embedding 就走模型向量，不再用词法编码器
    embeddings = json.dumps({target["url"]: [1.0, 0.0], "https://example.com/other": [0.0, 1.0]})
    model_run = semantic_main(items=parse_result["items"], history_items=history_items,
                              embeddings=embeddings)
    model_stats = json.loads(model_run["stats"])
    print("[code_semantic] 带 embedding：encoder=%s 模型向量=%d"
          % (model_stats["semantic_encoder"], model_stats["semantic_model_vectors"]))
    assert model_stats["semantic_encoder"] == "embedding+lexical", "传了 embedding 应该优先用它"
    assert model_stats["semantic_model_vectors"] == 1

    # 输入烂掉时必须整体放行，不能抛异常把工作流打断
    broken = semantic_main(items=[None, {"url": None, "title": None}, "not-a-dict"],
                           history_items="{不是 JSON")
    print("[code_semantic] 脏输入：error=%r kept=%d" % (broken["error"][:40], len(broken["items"])))
    assert broken["error"] == "", "脏输入应该被吸收，不该报错：%s" % broken["error"]

    # 单源刷屏回归：OpenAI Blog 灌 30 条，HN 只有 2 条，结果里必须仍然有 HN
    bulk = "".join(
        "<item><title>Bulk %d</title><link>https://bulk.example.com/post-%d</link>"
        "<description>d</description><pubDate>Mon, 06 Oct 2026 09:00:00 GMT</pubDate></item>" % (i, i)
        for i in range(30)
    )
    bulk_rss = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>' + bulk + "</channel></rss>"
    quota_result = namespace["main"](
        hn_items=hn_result["stories"],
        openai_body=bulk_rss,
        qbitai_body="",
        history_urls="",
    )
    quota_stats = json.loads(quota_result["stats"])
    quota_origins = {item["origin"] for item in quota_result["items"]}
    print()
    print("[配额] selected_by_source=%s" % json.dumps(quota_stats["selected_by_source"], ensure_ascii=False))
    assert "HN" in quota_origins, "HN 被高权重的 OpenAI Blog 挤掉了：%s" % quota_stats["selected_by_source"]
    assert quota_stats["selected"] <= 15, "选中数量超过 TOP_N：%s" % quota_stats["selected"]

    namespace2 = {}
    exec(compile(nodes["code_build"]["data"]["code"], "code_build", "exec"), namespace2)
    build_result = namespace2["main"](
        llm_markdown=FAKE_REPORT,
        items=parse_result["items"],
        run_date="2026-10-07",
    )

    print()
    print("[code_build] 输出键: %s  count=%s" % (list(build_result), build_result["count"]))
    print("[code_build] pushed_urls: %s" % build_result["pushed_urls"])
    print("--- markdown ---")
    print(build_result["markdown"][:600])
    assert "<think>" not in build_result["markdown"], "推理块没被剥离"
    assert "今日头条" in build_result["markdown"], "分档标题丢了"
    assert "重要性 5/5" in build_result["markdown"], "评分丢了"
    assert build_result["count"] == 2, "日报里有 2 条链接，count 应为 2，实际 %s" % build_result["count"]
    assert "https://openai.com/index/gpt-5" in build_result["pushed_urls"], "正文里的链接没被抽出来"
    assert "https://openai.com/index/gpt-5/" not in build_result["pushed_urls"], (
        "链接没归一到 code_parse 的形式，跨天去重会漏"
    )
    assert "https://example.com/thing" in build_result["pushed_urls"], (
        "模型转义右括号留下的反斜杠没被剥掉：%s" % build_result["pushed_urls"]
    )
    # pushed_items 是给调度脚本写本地库用的：语义去重依赖历史标题，只存 URL 就没法比
    assert len(build_result["pushed_items"]) == len(build_result["pushed_urls"]), (
        "pushed_items 与 pushed_urls 数量不一致"
    )
    pushed_titles = {row["url"]: row["title"] for row in build_result["pushed_items"]}
    print("[code_build] pushed_items: %s" % json.dumps(build_result["pushed_items"], ensure_ascii=False)[:200])
    assert pushed_titles.get("https://openai.com/index/gpt-5"), (
        "pushed_items 要回填标题，否则跨天语义去重没有文本可比"
    )
    assert all(row.get("origin") is not None for row in build_result["pushed_items"])
    assert "thing\\)" not in build_result["markdown"], "正文里的链接反斜杠没被清掉，点开会 404"
    # 模型跑偏时必须走保底渲染，不能把道歉或空消息推出去
    degraded = namespace2["main"](llm_markdown=FAKE_JUNK, items=parse_result["items"], run_date="2026-10-07")
    print()
    print("[code_build/保底] count=%s 链接数=%s" % (degraded["count"], len(degraded["pushed_urls"])))
    assert "保底渲染" in degraded["markdown"], "LLM 输出不可用时应该走保底渲染"
    assert degraded["markdown"].startswith("# AI 日报 · 2026-10-07"), "保底渲染的标题不对"
    assert degraded["pushed_urls"], "保底渲染也要带出条目链接，否则跨天去重会漏"

    # 模型把整篇日报套进 ```markdown 围栏里也要能还原
    fenced = namespace2["main"](
        llm_markdown="```markdown\n" + FAKE_REPORT + "\n```",
        items=parse_result["items"],
        run_date="2026-10-07",
    )
    assert fenced["count"] == 2, "代码围栏没被剥掉：count=%s" % fenced["count"]
    print("[code_build] 代码围栏剥离 -> OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
