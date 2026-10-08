# -*- coding: utf-8 -*-
"""
Dify 代码节点：解析 Hacker News API 返回的 JSON。

数据流
    HTTP 请求节点（拉 HN 数据）-> 本节点（清洗 / 去重 / 格式化）-> 结构化列表

输入
    hn_json          : str | dict | list  HTTP 节点响应体，兼容 Algolia 与 Firebase 两种形状
    tz_offset_hours  : float  输出时间的时区偏移，默认 8（北京时间 UTC+8）
    time_format      : str    strftime 格式，默认 "%Y-%m-%d %H:%M"
    max_items        : int    最多保留多少条，0 表示不限制

输出（对应 Dify 节点的输出变量）
    stories       : list[dict]  每条必含 title / url / time / source，
                                另附 id / author / score / comments / text / time_iso / time_unix
    count         : int  列表长度
    skipped       : int  因缺标题或结构非法被丢弃的条数
    deduplicated  : int  被去重合并掉的条数
    error         : str  解析失败原因，正常为空字符串
"""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit

# --------------------------------------------------------------------------- 常量

HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
HN_FALLBACK_SOURCE = "Hacker News"
DEFAULT_TZ_OFFSET_HOURS = 8.0
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M"
MAX_COLLECT_DEPTH = 4

# 条目列表可能挂在这些字段下（按优先级）
_LIST_KEYS = ("hits", "stories", "items", "results", "data", "list", "articles")

_TITLE_KEYS = ("title", "story_title", "name", "headline")
_URL_KEYS = ("url", "story_url", "link", "permalink", "href")
_ID_KEYS = ("id", "objectID", "object_id", "story_id")
_TIME_KEYS = (
    "time",
    "created_at_i",
    "timestamp",
    "published_at",
    "published",
    "date",
    "created_at",
)
_AUTHOR_KEYS = ("by", "author", "user", "username")
_SCORE_KEYS = ("score", "points", "votes")
_COMMENT_KEYS = ("descendants", "num_comments", "comments_count", "comments")
_SOURCE_KEYS = ("source", "site", "publisher", "domain")
_TEXT_KEYS = ("story_text", "text", "description", "summary", "content")

# 正文截断长度，供下游 LLM 摘要有足够上下文又不超过提示词预算
TEXT_LIMIT = 400

# 去重时应当忽略的跟踪参数
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid",
    "ref", "ref_src", "spm",
}

_HOST_ALIASES = {
    "x.com": "twitter.com",
    "mobile.twitter.com": "twitter.com",
    "m.twitter.com": "twitter.com",
    "old.reddit.com": "reddit.com",
    "np.reddit.com": "reddit.com",
    "www.reddit.com": "reddit.com",
}
_HOST_PREFIXES = ("www.", "m.", "mobile.", "amp.", "web.")

_WHITESPACE_RE = re.compile(r"\s+")
_NUMERIC_RE = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")


# ----------------------------------------------------------------------- 基础工具


def _as_text(value):
    """把任意 JSON 标量安全转成字符串；容器 / 布尔 / None 一律视为空。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _clean(value):
    """折叠空白并去掉首尾空格，用于净化 title 之类的文本。"""
    return _WHITESPACE_RE.sub(" ", _as_text(value)).strip()


def _first_present(item, keys):
    """按候选字段名顺序取第一个非空值。"""
    if not isinstance(item, dict):
        return ""
    for key in keys:
        text = _clean(item.get(key))
        if text:
            return text
    return ""


def _to_int(value, default=None):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _as_text(value).strip()
    try:
        return int(float(text))
    except ValueError:
        return default


def _to_float(value, default=0.0):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = _as_text(value).strip()
    try:
        return float(text)
    except ValueError:
        return default


def _clip(text, limit=TEXT_LIMIT):
    """把正文截断到指定长度。"""
    return text[:limit] if text else ""


# ----------------------------------------------------------------------- 时间处理


def _normalize_epoch(number):
    """把秒 / 毫秒 / 微秒 / 纳秒时间戳统一成秒。"""
    magnitude = abs(number)
    if magnitude >= 1e17:      # 纳秒
        return number / 1e9
    if magnitude >= 1e14:      # 微秒
        return number / 1e6
    if magnitude >= 1e11:      # 毫秒
        return number / 1e3
    return number              # 秒


def _parse_iso_datetime(text):
    candidate = text.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:          # 无时区信息时按 UTC 处理
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def to_unix_seconds(value):
    """epoch 秒 / 毫秒、数字字符串、ISO8601 字符串统一成 Unix 秒；无法识别返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _normalize_epoch(float(value))

    text = _as_text(value).strip()
    if not text:
        return None
    if _NUMERIC_RE.match(text):
        return _normalize_epoch(float(text))
    return _parse_iso_datetime(text)


def format_time(value, tz_offset_hours=DEFAULT_TZ_OFFSET_HOURS, time_format=DEFAULT_TIME_FORMAT):
    """把任意时间表示格式化成字符串；无法识别时返回空字符串。"""
    seconds = to_unix_seconds(value)
    if seconds is None:
        return ""
    tzinfo = timezone(timedelta(hours=_to_float(tz_offset_hours)))
    try:
        return datetime.fromtimestamp(seconds, tzinfo).strftime(time_format)
    except (OSError, OverflowError, ValueError):
        return ""


def format_time_utc(value):
    """输出 UTC 的 ISO8601（秒级精度，带 Z），便于机器读取。"""
    seconds = to_unix_seconds(value)
    if seconds is None:
        return ""
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- URL 处理


def _normalize_host(host):
    """小写化主机名，去掉 www / m / amp 之类前缀并做常见别名归一。"""
    host = (host or "").lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix):]
            break
    return _HOST_ALIASES.get(host, host)


def host_of(url):
    """从 URL 中取出用于展示的域名。"""
    text = _clean(url)
    if not text:
        return ""
    if "//" not in text.split("?", 1)[0]:
        text = "https://" + text
    return _normalize_host(urlsplit(text).hostname)


def normalize_url(url):
    """归一化 URL 用于去重：小写主机、补全裸域名、去掉跟踪参数与尾斜杠。"""
    text = _clean(url)
    if not text:
        return ""
    if "//" not in text.split("?", 1)[0]:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower().rstrip("/")

    host = _normalize_host(parts.hostname)
    if not host:
        return text.lower().rstrip("/")

    netloc = host
    try:
        if parts.port:
            netloc = "%s:%d" % (host, parts.port)
    except ValueError:
        pass

    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS
        )
    )
    normalized = netloc + path
    if query:
        normalized += "?" + query
    return normalized


def extract_source(raw_item, url, synthesized=False):
    """
    来源判定优先级：
    1. 数据自带的 source / site / domain 字段
    2. 链接是本地合成的 HN 讨论页（文本贴、Ask HN）-> 记为 Hacker News
    3. 链接域名
    """
    explicit = _first_present(raw_item, _SOURCE_KEYS)
    if explicit:
        return explicit
    if synthesized:
        return HN_FALLBACK_SOURCE
    return host_of(url) or HN_FALLBACK_SOURCE


# -------------------------------------------------------------------- 条目构造


def build_story(raw_item, tz_offset_hours=DEFAULT_TZ_OFFSET_HOURS, time_format=DEFAULT_TIME_FORMAT):
    """把单条原始数据规范化；缺标题或无法生成链接时返回 None（丢弃）。"""
    if not isinstance(raw_item, dict):
        return None

    title = _first_present(raw_item, _TITLE_KEYS)
    if not title:
        return None

    item_id = _first_present(raw_item, _ID_KEYS)
    url = _first_present(raw_item, _URL_KEYS)
    synthesized = not url
    if synthesized:
        if not item_id:
            return None                     # 既没链接也没 id，无法生成可点击条目
        url = HN_ITEM_URL.format(id=item_id)

    raw_time = _first_present(raw_item, _TIME_KEYS)
    seconds = to_unix_seconds(raw_time)

    return {
        "title": title,
        "url": url,
        "time": format_time(raw_time, tz_offset_hours, time_format),
        "source": extract_source(raw_item, url, synthesized),
        "id": item_id,
        "author": _first_present(raw_item, _AUTHOR_KEYS),
        "score": _to_int(_first_present(raw_item, _SCORE_KEYS)),
        "comments": _to_int(_first_present(raw_item, _COMMENT_KEYS)),
        "text": _clip(_first_present(raw_item, _TEXT_KEYS)),
        "time_iso": format_time_utc(raw_time),
        "time_unix": int(seconds) if seconds is not None else None,
    }


def dedupe_key(story):
    """同一条新闻可能有多种链接形态，统一用归一化 URL 做键。"""
    normalized = normalize_url(story["url"])
    if normalized:
        return "url:" + normalized
    return "title:" + _clean(story["title"]).casefold()


def _merge_missing(target, extra):
    """重复条目保留先出现的，只补齐 target 中缺失的字段。"""
    # build_story 保证 url 非空，这里同样按“先到先得”补齐其余字段
    for field in ("url", "time", "time_iso", "time_unix", "source", "id", "author", "score", "comments"):
        if target.get(field) in (None, "") and extra.get(field) not in (None, ""):
            target[field] = extra[field]


# ----------------------------------------------------------------- 入口 / 主流程


def _loads(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def collect_items(payload, depth=0):
    """从各种响应形状里取出条目列表。"""
    if payload is None or depth > MAX_COLLECT_DEPTH:
        return []
    if isinstance(payload, str):
        return collect_items(_loads(payload), depth + 1)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            if key in payload:
                found = collect_items(payload[key], depth + 1)
                if found:
                    return found
        values = list(payload.values())
        if values and all(isinstance(value, dict) for value in values):
            return values                    # {"123": {...}, "456": {...}} 形式
        if any(key in payload for key in _TITLE_KEYS + _URL_KEYS + _ID_KEYS):
            return [payload]                 # 单个条目
    return []


def parse_hn_json(
    hn_json,
    tz_offset_hours=DEFAULT_TZ_OFFSET_HOURS,
    time_format=DEFAULT_TIME_FORMAT,
    max_items=0,
):
    """核心逻辑：清洗 + 去重 + 时间格式化，返回 Dify 节点输出。"""
    limit = _to_int(max_items, 0) or 0
    stories = []
    index = {}
    skipped = 0
    deduplicated = 0

    for raw_item in collect_items(hn_json):
        if limit and len(stories) >= limit:
            break
        story = build_story(raw_item, tz_offset_hours, time_format)
        if story is None:
            skipped += 1
            continue
        key = dedupe_key(story)
        existing = index.get(key)
        if existing is None:
            index[key] = story
            stories.append(story)
        else:
            deduplicated += 1
            _merge_missing(existing, story)

    return {
        "stories": stories,
        "count": len(stories),
        "skipped": skipped,
        "deduplicated": deduplicated,
        "error": "",
    }


def main(
    hn_json,
    tz_offset_hours=DEFAULT_TZ_OFFSET_HOURS,
    time_format=DEFAULT_TIME_FORMAT,
    max_items=0,
):
    """Dify 代码节点入口，保证不抛异常。"""
    try:
        return parse_hn_json(hn_json, tz_offset_hours, time_format, max_items)
    except Exception as exc:                                   # noqa: BLE001
        return {
            "stories": [],
            "count": 0,
            "skipped": 0,
            "deduplicated": 0,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


# ---------------------------------------------------------------- 手动运行示例数据

SAMPLE = {
    "hits": [
        {
            "objectID": "41234567",
            "title": "Show HN:  I built a tiny workflow engine",
            "url": "https://example.com/posts/workflow?utm_source=hn&utm_medium=social",
            "created_at": "2026-10-06T23:40:05Z",
            "created_at_i": 1791330005,
            "author": "alice",
            "points": 321,
            "num_comments": 87,
        },
        {
            "objectID": "41234568",
            "title": "Ask HN: How do you keep up with AI news?",
            "url": None,
            "story_text": "每天信息太多，想知道大家用什么方式筛选。",
            "created_at_i": 1791333605,
            "author": "bob",
            "points": 56,
        },
        {
            "objectID": "41234569",
            "title": "  Show HN: I built a tiny workflow engine  ",
            "url": "https://www.example.com/posts/workflow/?utm_campaign=weekly",
            "created_at_i": 1791330005,
        },
        {
            "objectID": "41234570",
            "title": "",
            "url": "https://example.com/broken",
        },
        {
            "title": "Rust 1.90 released",
            "url": "https://blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
            "time": None,
            "score": "410",
        },
    ]
}


if __name__ == "__main__":
    print(json.dumps(main(json.dumps(SAMPLE, ensure_ascii=False)), ensure_ascii=False, indent=2))
