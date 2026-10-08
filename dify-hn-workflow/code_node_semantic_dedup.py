# -*- coding: utf-8 -*-
"""
Dify 代码节点：语义去重（Day 3-4 的 RAG 环节）。

数据流
    code_parse（URL 去重）-> 本节点（语义去重）-> llm_sum（写日报）

做什么
    1. 把条目的「标题 + 摘要片段」编码成向量
    2. 与「历史已推送条目」和「本批已保留条目」逐条比余弦相似度，取 top_k 候选
    3. 相似度 >= dup_threshold      -> 判重，丢弃
       gray_low <= 相似度 < 阈值    -> 疑似重复：保留但打 semantic 标记，交给 llm_sum 裁决
       相似度 < gray_low            -> 保留

编码器是可插拔的，这是本节点最重要的设计
    默认用本地词法编码器：纯标准库、纯算术无随机成分（不受 PYTHONHASHSEED 影响）、
    不依赖外部服务，因此历史条目只需要传标题，每次重新编码即可，不用存向量。
    它的实测边界很明确（跑本文件即可复现标定数据）：
        同一语言近重复（中-中、英-英改写）：0.84~1.00，能判出来
        跨语言同事件（英文原文 vs 中文报道）：0.07~0.30，判不出来
        不同事件但同源（Rust 1.90 vs 1.91）：0.67，会误判到「疑似」区
    所以跨语言那一类必须靠真 embedding 模型：上游加一个 HTTP 节点调 embedding
    接口，把结果通过 embeddings 入参传进来，本节点就优先用它，词法编码器退化成
    兜底。判重逻辑（top_k / 双阈值 / 同源加严）完全不用改。
    没接 embedding 时，跨语言重复由 llm_sum 的「同一件事只保留信息最完整的一条」
    规则兜底，两者互补。

输入
    items          : list[dict]  code_parse 的输出，元素含 title / url / origin / raw
    history_items  : str         历史已推送条目，JSON 数组或 JSON Lines，
                                 元素含 url / title / raw（raw 可缺省）
    embeddings     : str        可选。{"url": [float, ...]}，由上游 HTTP 节点调用
                                 真 embedding 接口得到；命中的条目用真向量，其余
                                 继续用本地词法编码器。留空则全部走词法编码器
    top_k          : int         每条最多比几个历史候选，默认 5
    dup_threshold  : float       判重阈值，默认 0.90
    gray_low       : float       疑似重复下界，默认 0.80

输出（对应 Dify 节点的输出变量）
    items       : list[dict]  保留的条目，疑似重复的条目带 semantic 字段
    items_json  : str         同 items 的 JSON
    stats       : str         JSON，含各阶段计数与判重明细，供日志和 Day 5 周报使用
    error       : str         异常原因，正常为空字符串
"""

import json
import math
import re

# --------------------------------------------------------------------------- 常量

# 标题在向量里的权重（重复计入几次），标题比摘要片段更能代表一条资讯
TITLE_REPEAT = 2
# 摘要片段最多取多少字进向量，太长会稀释标题
RAW_LIMIT = 200

DEFAULT_TOP_K = 5
# 实测（见文件末尾的标定用例）：真正重复的条目普遍落在 0.90 以上，
# 不同事件但同源的条目多数在 0.80 以下，所以 0.90 判重、0.80 起疑。
DUP_THRESHOLD = 0.90
GRAY_LOW = 0.80
# 同源条目要求更高：同一家媒体的连载、同系列产品发布，用词天然接近
SAME_SOURCE_MARGIN = 0.03
# 文本太短时相似度不稳定，直接不参与判重
MIN_TEXT_CHARS = 6

_LATIN_RE = re.compile(r"[a-z0-9]+(?:[.\-_/][a-z0-9]+)*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WHITESPACE_RE = re.compile(r"\s+")

# 只放高频虚词，不放「发布 / 推出 / 正式」这类在资讯标题里有实义的词
_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "at", "by",
    "with", "from", "is", "are", "was", "were", "be", "it", "its", "this",
    "that", "as", "new", "how", "why", "what", "your", "you", "we",
    "的", "了", "是", "在", "和", "与", "对", "就", "都", "而", "及",
}


# ----------------------------------------------------------------------- 基础工具


def _clean(value):
    """折叠空白并去掉首尾空格；非字符串一律当空串。"""
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def tokenize(text):
    """切词：英文按单词、中文按「单字 + 双字」，都丢掉虚词。

    中文不用分词库（节点里装不了），用单字加双字组合近似，对「同一事件换种说法」
    这种改写足够敏感。
    """
    text = _clean(text).lower()
    if not text:
        return []

    tokens = []
    for word in _LATIN_RE.findall(text):
        if len(word) >= 2 and word not in _STOPWORDS:
            tokens.append(word)

    for run in _CJK_RE.findall(text):
        for char in run:
            if char not in _STOPWORDS:
                tokens.append(char)
        for index in range(len(run) - 1):
            bigram = run[index:index + 2]
            if bigram not in _STOPWORDS:
                tokens.append(bigram)
    return tokens


def embed(title, raw=""):
    """把标题和摘要片段编码成 L2 归一化的稀疏向量 {token: weight}。

    权重用 1 + log(tf)，抑制高频词的重复计数；返回稀疏字典而不是定长数组，
    所以余弦相似度是精确值，没有哈希碰撞带来的噪声。
    """
    tokens = tokenize(title) * TITLE_REPEAT + tokenize(raw)
    if not tokens:
        return {}

    counts = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1

    vector = {}
    squares = 0.0
    for token, count in counts.items():
        weight = 1.0 + math.log(count)
        vector[token] = weight
        squares += weight * weight

    norm = math.sqrt(squares)
    if norm <= 0:
        return {}
    return {token: weight / norm for token, weight in vector.items()}


def cosine(left, right):
    """两个稀疏向量的余弦相似度，输入已归一化，所以等价于点积。"""
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(token, 0.0) for token, weight in left.items())


def item_vector(item):
    """从条目里取出标题和摘要片段，编码成向量。"""
    title = _clean(item.get("title"))
    raw = _clean(item.get("raw"))[:RAW_LIMIT]
    if len(title) + len(raw) < MIN_TEXT_CHARS:
        return {}
    return embed(title, raw)


def parse_history(history_items):
    """解析历史条目：JSON 数组和 JSON Lines 都接受，坏行直接跳过。"""
    text = history_items if isinstance(history_items, str) else ""
    text = text.strip()
    if not text:
        return []

    rows = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except ValueError:
        pass

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def unit_vector(values):
    """把真 embedding 模型给的一串浮点数转成单位长度的稀疏向量。

    统一用 {token: weight} 的稀疏字典表示向量，词法向量和模型向量走同一套余弦
    计算，判重逻辑就不需要为两种编码器分叉。零值不存，数学上和稠密向量等价。
    """
    if not isinstance(values, (list, tuple)) or not values:
        return {}
    squares = 0.0
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}
        squares += float(value) * float(value)
    if squares <= 0:
        return {}
    norm = math.sqrt(squares)
    return {("d%d" % index): float(value) / norm
            for index, value in enumerate(values) if value}


def parse_embeddings(embeddings):
    """解析上游传入的向量表：{"url": [float, ...]} 或 {"url": {"0": float, ...}}。"""
    text = embeddings if isinstance(embeddings, str) else ""
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    table = {}
    for url, values in parsed.items():
        if isinstance(values, dict):
            values = [values[key] for key in sorted(values, key=lambda item: int(item))]
        vector = unit_vector(values)
        if vector and isinstance(url, str):
            table[url.strip()] = vector
    return table


# ------------------------------------------------------------------------- 主逻辑


def vector_for(row, table):
    """取条目向量：优先用上游传来的真 embedding，没有就退回本地词法编码器。"""
    url = _clean(row.get("url"))
    if url and url in table:
        return table[url]
    return item_vector(row)


def dedup(items, history_items="", embeddings="", top_k=DEFAULT_TOP_K,
          dup_threshold=DUP_THRESHOLD, gray_low=GRAY_LOW):
    """对条目做语义去重，返回 (保留的条目, 统计字典)。"""
    rows = [item for item in (items or []) if isinstance(item, dict)]
    table = parse_embeddings(embeddings)

    history = []
    for row in parse_history(history_items):
        vector = vector_for(row, table)
        if vector:
            history.append({
                "url": _clean(row.get("url")),
                "vector": vector,
                "origin": _clean(row.get("origin")),
            })

    kept = []
    kept_vectors = []
    dropped = []
    suspects = []
    max_history_score = 0.0
    model_vectors = 0

    for item in rows:
        url = _clean(item.get("url"))
        origin = _clean(item.get("origin"))
        vector = vector_for(item, table)
        if url and url in table:
            model_vectors += 1

        # 编码不出向量（标题和摘要都空）就不判重，宁可漏判也不误杀
        if not vector:
            kept.append(dict(item))
            kept_vectors.append({})
            continue

        candidates = []
        for entry in history:
            score = cosine(vector, entry["vector"])
            max_history_score = max(max_history_score, score)
            candidates.append((score, entry["url"], entry["origin"], "history"))
        for index, entry in enumerate(kept_vectors):
            if not entry:
                continue
            candidates.append((cosine(vector, entry), _clean(kept[index].get("url")),
                               _clean(kept[index].get("origin")), "batch"))

        if not candidates:
            kept.append(dict(item))
            kept_vectors.append(vector)
            continue

        candidates.sort(key=lambda row: row[0], reverse=True)
        top_score, top_url, top_origin, top_scope = candidates[0]

        # 同源条目用更高的阈值：同一家媒体的连载、同系列产品发布，用词天然接近
        same_source = bool(origin) and origin == top_origin
        threshold = dup_threshold + (SAME_SOURCE_MARGIN if same_source else 0.0)

        # URL 完全相同必然是重复（正常已被 code_parse 拦掉，这里是兜底）
        if url and top_url and url == top_url:
            top_score, threshold = 1.0, dup_threshold

        if top_score >= threshold:
            dropped.append({
                "url": url,
                "title": _clean(item.get("title"))[:80],
                "origin": origin,
                "dup_of": top_url,
                "dup_scope": top_scope,
                "score": round(top_score, 4),
            })
            continue

        annotated = dict(item)
        if top_score >= gray_low and top_url:
            annotated["semantic"] = {
                "score": round(top_score, 4),
                "similar_to": top_url,
                "similar_scope": top_scope,
                "action": "suspect",
            }
            suspects.append({
                "url": url,
                "title": _clean(item.get("title"))[:80],
                "similar_to": top_url,
                "score": round(top_score, 4),
            })

        kept.append(annotated)
        kept_vectors.append(vector)

    stats = {
        "semantic_in": len(rows),
        "semantic_kept": len(kept),
        "semantic_dropped": len(dropped),
        "semantic_suspect": len(suspects),
        "semantic_history": len(history),
        "semantic_model_vectors": model_vectors,
        "semantic_encoder": "embedding+lexical" if table else "lexical",
        "dup_threshold": dup_threshold,
        "gray_low": gray_low,
        "top_k": int(top_k),
        "max_history_score": round(max_history_score, 4),
        "dropped_detail": dropped[:20],
        "suspect_detail": suspects[:20],
    }
    return kept, stats


def main(items, history_items="", embeddings="", top_k=DEFAULT_TOP_K,
         dup_threshold=DUP_THRESHOLD, gray_low=GRAY_LOW):
    """Dify 代码节点入口：去重失败时放行全部条目，绝不因为异常推空日报。"""
    rows = items if isinstance(items, list) else []
    try:
        kept, stats = dedup(
            rows,
            history_items=history_items,
            embeddings=embeddings,
            top_k=top_k,
            dup_threshold=dup_threshold,
            gray_low=gray_low,
        )
        error = ""
    except Exception as exc:  # noqa: BLE001 - 节点里任何异常都不能让工作流挂掉
        kept = [dict(item) for item in rows if isinstance(item, dict)]
        stats = {
            "semantic_in": len(rows),
            "semantic_kept": len(kept),
            "semantic_dropped": 0,
            "semantic_suspect": 0,
            "semantic_history": 0,
            "semantic_model_vectors": 0,
            "semantic_encoder": "lexical",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
        error = stats["error"]

    return {
        "items": kept,
        "items_json": json.dumps(kept, ensure_ascii=False),
        "stats": json.dumps(stats, ensure_ascii=False),
        "error": error,
    }


# ---------------------------------------------------------------- 手动运行示例数据

# 阈值标定用例：(A标题, A摘要, B标题, B摘要, 是否同一事件)
# 跑 `python code_node_semantic_dedup.py` 打印每对的相似度，用来给 dup_threshold /
# gray_low 找落点。将来换成真 embedding 模型，这批用例同样可以复用。
CALIBRATION_PAIRS = [
    ("OpenAI releases GPT-5 to all users", "GPT-5 is now available in ChatGPT.",
     "OpenAI 正式发布 GPT-5", "GPT-5 今天起在 ChatGPT 全量开放。", True),
    ("Anthropic raises $4B at $180B valuation", "",
     "Anthropic 完成 40 亿美元融资，估值 1800 亿", "", True),
    ("Google DeepMind open-sources a new protein model", "Faster structure prediction.",
     "DeepMind 开源新一代蛋白质结构模型", "推理速度比上一代快 3 倍。", True),
    ("Meta open-sources Llama 4", "",
     "Llama 4 权重已开源", "", True),
    ("机器之心：国产大模型发布新版本", "参数规模 700 亿。",
     "国产大模型发布新版本，参数 700 亿", "", True),
    ("Show HN: I built a tiny workflow engine", "A weekend project, single binary.",
     "Show HN: my workflow engine, now with retries", "Added retry support.", False),
    ("OpenAI releases GPT-5 to all users", "",
     "OpenAI 扩大 ChatGPT 广告投放", "", False),
    ("Rust 1.90 released", "", "Rust 1.91 released", "", False),
    ("Anthropic raises $4B at $180B valuation", "",
     "Anthropic 发布 Claude 新版本", "", False),
    ("Google DeepMind open-sources a new protein model", "",
     "Google 发布新一代 TPU", "", False),
]

SAMPLE = {
    "items": [
        {
            "index": 0,
            "title": "OpenAI releases GPT-5 to all users",
            "url": "https://openai.com/index/gpt-5",
            "origin": "OpenAI Blog",
            "score": 0,
            "published": "2026-10-07",
            "raw": "GPT-5 is now available to all ChatGPT users, with better reasoning.",
        },
        {
            "index": 1,
            "title": "OpenAI 正式发布 GPT-5 模型",
            "url": "https://www.qbitai.com/2026/10/gpt-5",
            "origin": "量子位",
            "score": 0,
            "published": "2026-10-07",
            "raw": "GPT-5 今天起在 ChatGPT 全量开放，推理能力提升明显。",
        },
        {
            "index": 2,
            "title": "Rust 1.90 released",
            "url": "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
            "origin": "Hacker News",
            "score": 410,
            "published": "2026-10-05",
            "raw": "",
        },
    ],
    "history": [
        {"url": "https://openai.com/index/gpt-5", "title": "OpenAI releases GPT-5 to all users",
         "origin": "OpenAI Blog", "raw": "GPT-5 is now available to all ChatGPT users."},
        {"url": "https://example.com/old-rust", "title": "Rust 1.89 released",
         "origin": "Hacker News", "raw": ""},
    ],
}


# 演示「接了真 embedding 之后」的效果：用一个 3 维主题向量冒充模型输出。
# 跨语言的同一事件被映射到同一个方向，这是词法编码器做不到、而真实模型能做到的部分。
SAMPLE_EMBEDDINGS = {
    "https://openai.com/index/gpt-5": [1.0, 0.0, 0.0],
    "https://www.qbitai.com/2026/10/gpt-5": [0.98, 0.02, 0.0],
    "https://example.com/old-rust": [0.0, 1.0, 0.0],
    "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html": [0.0, 0.0, 1.0],
}


def _has_cjk(text):
    return bool(_CJK_RE.search(text or ""))


def _report_calibration():
    """打印标定用例的相似度，并按当前阈值判断是否命中。

    这个函数就是「定阈值」的工具：改 CALIBRATION_PAIRS 换成真实抓到的条目，
    看同一事件组和不同事件组能不能分开。分不开就说明编码器不够用，该接真模型。
    """
    print("== 阈值标定（当前 dup_threshold=%.2f, gray_low=%.2f）==" % (DUP_THRESHOLD, GRAY_LOW))
    same_lang, cross_lang, other_scores = [], [], []
    for title_a, raw_a, title_b, raw_b, same_event in CALIBRATION_PAIRS:
        score = cosine(embed(title_a, raw_a), embed(title_b, raw_b))
        cross = _has_cjk(title_a) != _has_cjk(title_b)
        if not same_event:
            other_scores.append(score)
        elif cross:
            cross_lang.append(score)
        else:
            same_lang.append(score)
        verdict = "判重" if score >= DUP_THRESHOLD else ("疑似" if score >= GRAY_LOW else "保留")
        mark = "OK" if (score >= DUP_THRESHOLD) == same_event else "!!"
        print("  %s %.3f %-4s 同一事件=%-5s 跨语言=%-5s %s"
              % (mark, score, verdict, same_event, cross, title_a[:30]))
    print()
    if same_lang:
        print("同语言同一事件：min=%.3f max=%.3f" % (min(same_lang), max(same_lang)))
    if cross_lang:
        print("跨语言同一事件：min=%.3f max=%.3f  <- 词法编码器抓不到，需真 embedding"
              % (min(cross_lang), max(cross_lang)))
    if other_scores:
        print("不同事件：      min=%.3f max=%.3f" % (min(other_scores), max(other_scores)))
    print("结论：同语言可以靠 dup_threshold 分开；跨语言不分语言、词汇全换，词法向量无法覆盖，")
    print("      这是接真 embedding 模型（embeddings 入参）的主要理由。")


def _report_sample():
    """同一批条目跑两遍：只用词法编码器 vs 接了 embedding，看差别。"""
    history = json.dumps(SAMPLE["history"], ensure_ascii=False)
    print()
    print("== 示例：code_parse 的 3 条 + 2 条历史 ==")
    for label, kwargs in (("只用词法编码器", {}),
                          ("接真 embedding", {"embeddings": json.dumps(SAMPLE_EMBEDDINGS)})):
        result = main(SAMPLE["items"], history, **kwargs)
        stats = json.loads(result["stats"])
        print("[%s] encoder=%s 模型向量=%d 保留=%d 判重=%d 疑似=%d"
              % (label, stats["semantic_encoder"], stats["semantic_model_vectors"],
                 stats["semantic_kept"], stats["semantic_dropped"], stats["semantic_suspect"]))
        for row in stats["dropped_detail"]:
            print("     判重：%s <- %s (%.3f, %s)" % (row["title"][:34], row["dup_of"], row["score"], row["dup_scope"]))
        print("     保留：" + " | ".join(item["title"][:28] for item in result["items"]))


if __name__ == "__main__":
    _report_calibration()
    _report_sample()
