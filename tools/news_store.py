"""已处理条目的本地库（SQLite），替代飞书多维表格。

一个库同时承载三件事，取代原方案里的「飞书 URL 表 + Dify 知识库 + 飞书评估表」：
    Day 1-2  URL 去重      filter_new() 查重、record_pushed() 处理完写回
    Day 3-4  语义去重状态  history_items() 导出历史标题给语义去重节点比对
    Day 5    指标统计      stats() 按天按源出数，喂给 tools/weekly_report.py

为什么不用飞书多维表格：
    1. Dify 的 HTTP 节点只看状态码，多维表格业务失败也返 200，状态会被写脏
    2. 要换 tenant_access_token、受 QPS 限制，每次运行多两轮网络往返
    3. 写回时机绑不到「真的推送成功」上，推送失败会漏掉一整天资讯
    本地文件没有这些问题，而且和 scheduler/state/history.json 是同一套思路。

关于向量：
    这里不存向量，只存 title 和 raw。向量由 dify-hn-workflow/code_node_semantic_dedup.py
    的编码器在每次运行时重新算——它是确定性的，重算结果和存下来完全一致，
    这样以后换编码器（词法 -> 真 embedding 模型）不需要迁移任何历史数据。

用法：
    store = NewsStore()                       # 默认 scheduler/state/news.db
    fresh = store.filter_new(articles)        # [(文章), ...] 只返回没见过的
    ... 处理、推送 ...
    store.record_pushed(fresh, run_date)      # 处理完写回
    print(store.history_items())              # 导出给语义去重节点的历史

命令行：
    python tools/news_store.py --stats                 看库里的统计
    python tools/news_store.py --prune                 按 history_days 清理过期记录
    python tools/news_store.py --import-history        把 history.json 迁移进来
    python tools/news_store.py --self-test             跑一遍自检
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "scheduler", "state", "news.db")
HISTORY_JSON_PATH = os.path.join(BASE_DIR, "scheduler", "state", "history.json")

# 与 dify-ai-news-v1.yml 里 code_parse 节点保持一致，否则库里的 key 和工作流算出来的对不上。
# tools/validate_dsl.py 会拿真实 URL 逐条比对两边，改一边忘了另一边会直接报错。
DROP_KEYS = {"ref", "fbclid", "gclid", "spm", "from", "source", "share_token"}

# 传给语义去重节点的历史条目上限，避免超过 Dify 入参长度限制
HISTORY_ITEMS_LIMIT = 400
RAW_STORE_LIMIT = 400
RAW_HISTORY_LIMIT = 120


class NewsStoreError(RuntimeError):
    """库不可用（损坏、只读、被独占）时抛出，调用方应落盘跳过而不是崩掉。"""


def normalize_url(raw):
    """URL 归一化：补 https、小写主机、去跟踪参数、去尾斜杠（与 code_parse 一致）。

    只做和去重有关的归一化，不改动路径大小写——很多站点的路径是区分大小写的。
    """
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    # 补全协议头：HN 抓到的链接不带协议（blog.example.com/x），不补全就取不到主机名，
    # 归一化会整个退化成原样返回，跨天去重就失效了
    if "//" not in raw.split("?", 1)[0]:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    if not host:
        return raw
    # www 前缀折叠：HN 常链到 www.example.com，RSS 常给 example.com，
    # 不折叠的话同一条会被当成两条推两遍
    if host.startswith("www.") and len(host) > 4:
        host = host[4:]
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if low.startswith("utm_") or low in DROP_KEYS:
            continue
        query.append((key, value))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def _clean(value, limit=0):
    """把任意标量转成干净字符串；非字符串当空串。"""
    if value is None or isinstance(value, bool):
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        text = ""
    text = " ".join(text.split()).strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _article_url(article):
    """从条目里取 URL：支持 dict（含 url 字段）和裸字符串两种输入。"""
    if isinstance(article, dict):
        return _clean(article.get("url"))
    if isinstance(article, str):
        return _clean(article)
    return ""


def _article_title(article):
    if isinstance(article, dict):
        return _clean(article.get("title"))
    return ""


def _article_source(article):
    if isinstance(article, dict):
        for key in ("origin", "source", "site"):
            value = _clean(article.get(key))
            if value:
                return value
    return ""


def _article_raw(article):
    if isinstance(article, dict):
        for key in ("raw", "summary", "text", "description"):
            value = _clean(article.get(key))
            if value:
                return value
    return ""


class NewsStore:
    """已处理条目的本地库。所有写操作走事务，失败整体回滚。"""

    def __init__(self, path=DEFAULT_DB_PATH, history_days=30, timeout=10.0):
        self.path = path
        self.history_days = int(history_days)
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            self.connection = sqlite3.connect(path, timeout=timeout)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema()
        except sqlite3.Error as error:
            raise NewsStoreError("打开本地库失败（%s）：%s" % (path, error))

    # ------------------------------------------------------------------ 生命周期

    def _ensure_schema(self):
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    url_key   TEXT PRIMARY KEY,
                    url       TEXT NOT NULL,
                    run_date  TEXT NOT NULL,
                    source    TEXT NOT NULL DEFAULT '',
                    title     TEXT NOT NULL DEFAULT '',
                    raw       TEXT NOT NULL DEFAULT '',
                    pushed_at TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_run_date ON articles(run_date)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
            )

    def close(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    # -------------------------------------------------------------------- URL 去重

    def filter_new(self, articles):
        """返回还没处理过的文章列表，顺序保持输入顺序。

        做两件事：批内按归一化 URL 去重，再和库里的历史比对。
        缺 URL 或 URL 非法的条目直接丢弃（它们本来就无法去重，留着只会重复推送）。
        """
        fresh, report = self.partition(articles)
        return fresh

    def partition(self, articles):
        """同 filter_new，但额外返回被拦下的明细，用于排查和 Day 5 指标。"""
        rows = list(articles or [])
        keys = []
        skipped = []
        for article in rows:
            key = normalize_url(_article_url(article))
            if not key:
                skipped.append(article)
            keys.append(key)

        seen_keys = set()
        known = self._known_keys([key for key in keys if key])
        fresh, duplicates = [], []
        for article, key in zip(rows, keys):
            if not key:
                continue
            if key in seen_keys or key in known:
                duplicates.append({"url_key": key, "title": _article_title(article)})
                continue
            seen_keys.add(key)
            fresh.append(article)

        report = {
            "input": len(rows),
            "fresh": len(fresh),
            "duplicates": len(duplicates),
            "invalid": len(skipped),
            "duplicate_detail": duplicates[:20],
        }
        return fresh, report

    def is_seen(self, url):
        """单条查询：URL 是否已经处理过。"""
        key = normalize_url(url)
        if not key:
            return False
        try:
            cursor = self.connection.execute(
                "SELECT 1 FROM articles WHERE url_key = ? LIMIT 1", (key,)
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as error:
            raise NewsStoreError("查询失败：%s" % error)

    def _known_keys(self, keys):
        """批量查已有的 key，分批查避免 SQLite 的变量个数上限。"""
        known = set()
        chunk_size = 400
        try:
            for start in range(0, len(keys), chunk_size):
                chunk = keys[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cursor = self.connection.execute(
                    "SELECT url_key FROM articles WHERE url_key IN (%s)" % placeholders, chunk
                )
                known.update(row["url_key"] for row in cursor.fetchall())
        except sqlite3.Error as error:
            raise NewsStoreError("批量查询失败：%s" % error)
        return known

    # -------------------------------------------------------------------- 写回

    def record_pushed(self, articles, run_date, pushed_at=None):
        """处理完写回。同一 URL 重复写入时更新元信息，不产生第二行。

        run_date 用真实推送日期，跨天去重和 Day 5 的按天统计都依赖它。
        """
        rows = list(articles or [])
        if not rows:
            return 0
        date = _clean(run_date) or datetime.date.today().isoformat()
        stamp = pushed_at or datetime.datetime.now().isoformat(timespec="seconds")

        payload = []
        skipped = 0
        seen = set()
        for article in rows:
            key = normalize_url(_article_url(article))
            if not key or key in seen:
                skipped += 1
                continue
            seen.add(key)
            payload.append((
                key,
                _article_url(article),
                date,
                _article_source(article),
                _article_title(article),
                _article_raw(article)[:RAW_STORE_LIMIT],
                stamp,
            ))

        try:
            with self.connection:
                self.connection.executemany(
                    """
                    INSERT INTO articles (url_key, url, run_date, source, title, raw, pushed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url_key) DO UPDATE SET
                        run_date  = excluded.run_date,
                        source    = CASE WHEN excluded.source <> '' THEN excluded.source ELSE articles.source END,
                        title     = CASE WHEN excluded.title  <> '' THEN excluded.title  ELSE articles.title  END,
                        raw       = CASE WHEN excluded.raw    <> '' THEN excluded.raw    ELSE articles.raw    END,
                        pushed_at = excluded.pushed_at
                    """,
                    payload,
                )
        except sqlite3.Error as error:
            raise NewsStoreError("写回失败，已回滚：%s" % error)
        if skipped:
            print("!! %d 条记录缺 URL 或批内重复，已跳过" % skipped, file=sys.stderr)
        return len(payload)

    def commit_new(self, articles, run_date):
        """filter_new + record_pushed 的组合：返回新文章列表，同时写回。

        对应「先查表、跳过、处理完写回」的完整流程；推送失败时不要调这个，
        否则没送达的条目会被当成已处理，第二天就漏了。
        """
        fresh, report = self.partition(articles)
        self.record_pushed(fresh, run_date)
        report["recorded"] = len(fresh)
        return fresh, report

    # -------------------------------------------------------------------- 历史导出

    def _cutoff(self, days=None):
        window = self.history_days if days is None else int(days)
        return (datetime.date.today() - datetime.timedelta(days=window)).isoformat()

    def history_rows(self, days=None):
        """取历史窗口内的条目，新的在前，同时受 HISTORY_ITEMS_LIMIT 限制。"""
        try:
            cursor = self.connection.execute(
                """
                SELECT url, title, raw, source, run_date
                FROM articles
                WHERE run_date >= ?
                ORDER BY run_date DESC, pushed_at DESC
                LIMIT ?
                """,
                (self._cutoff(days), HISTORY_ITEMS_LIMIT),
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as error:
            raise NewsStoreError("读历史失败：%s" % error)

    def history_urls(self, days=None):
        """换行分隔的 URL 串，直接喂给工作流的 history_urls 入参（跨天 URL 去重）。"""
        return "\n".join(row["url"] for row in self.history_rows(days) if row.get("url"))

    def history_items(self, days=None):
        """带标题的历史条目 JSON，喂给语义去重节点的 history_items 入参。

        只传标题和截断后的摘要片段：条目本身是原文的浓缩，向量由节点确定性重算，
        不需要传向量，入参因此能压得很小。
        """
        rows = [
            {
                "url": row["url"],
                "title": row["title"],
                "raw": (row.get("raw") or "")[:RAW_HISTORY_LIMIT],
                "origin": row.get("source") or "",
            }
            for row in self.history_rows(days)
            if row.get("url")
        ]
        return json.dumps(rows, ensure_ascii=False)

    # -------------------------------------------------------------------- 统计

    def stats(self, days=None):
        """按天、按源统计已推送条目，供 Day 5 周报使用。"""
        cutoff = self._cutoff(days)
        try:
            by_day = self.connection.execute(
                """
                SELECT run_date AS date, COUNT(*) AS pushed, COUNT(DISTINCT source) AS sources
                FROM articles WHERE run_date >= ?
                GROUP BY run_date ORDER BY run_date
                """,
                (cutoff,),
            ).fetchall()
            by_source = self.connection.execute(
                """
                SELECT source, COUNT(*) AS pushed, MIN(run_date) AS first_seen, MAX(run_date) AS last_seen
                FROM articles WHERE run_date >= ?
                GROUP BY source ORDER BY pushed DESC
                """,
                (cutoff,),
            ).fetchall()
            total = self.connection.execute(
                "SELECT COUNT(*) AS total, MIN(run_date) AS first_date, MAX(run_date) AS last_date FROM articles"
            ).fetchone()
        except sqlite3.Error as error:
            raise NewsStoreError("统计失败：%s" % error)

        return {
            "window_days": self.history_days if days is None else int(days),
            "cutoff": cutoff,
            "total_all_time": total["total"] if total else 0,
            "first_date": total["first_date"] if total else None,
            "last_date": total["last_date"] if total else None,
            "in_window": sum(row["pushed"] for row in by_day),
            "by_day": [dict(row) for row in by_day],
            "by_source": [dict(row) for row in by_source],
        }

    def prune(self, days=None):
        """删掉历史窗口外的记录，控制库体积。返回删除行数。"""
        try:
            with self.connection:
                cursor = self.connection.execute(
                    "DELETE FROM articles WHERE run_date < ?", (self._cutoff(days),)
                )
            return cursor.rowcount
        except sqlite3.Error as error:
            raise NewsStoreError("清理失败：%s" % error)

    def import_history_json(self, path=HISTORY_JSON_PATH):
        """把旧的 scheduler/state/history.json 迁移进来（幂等，可重复跑）。"""
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
        if not isinstance(rows, list):
            return 0
        return self.record_pushed(rows, run_date="", pushed_at=None) if rows else 0


# --------------------------------------------------------------------------- 命令行


def _self_test():
    """自检：归一化、批内去重、跨天去重、写回幂等、过期清理。"""
    import shutil

    # 临时库放在仓库内：系统临时目录在部分环境（沙箱 / 受限账户）不可写
    folder = os.path.join(BASE_DIR, "scheduler", "state", "_selftest")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    try:
        store = NewsStore(os.path.join(folder, "news.db"), history_days=30)
        today = datetime.date.today().isoformat()

        articles = [
            {"title": "GPT-5 发布", "url": "https://openai.com/index/gpt-5/?utm_source=hn",
             "origin": "OpenAI Blog"},
            {"title": "重复的同一条", "url": "https://www.openai.com/index/gpt-5/", "origin": "HN"},
            {"title": "Rust 1.90", "url": "blog.rust-lang.org/2026/10/05/Rust-1.90.0.html",
             "origin": "Hacker News"},
            {"title": "没有链接", "url": "", "origin": "HN"},
        ]
        fresh, report = store.partition(articles)
        assert len(fresh) == 2, "批内去重后应剩 2 条，实际 %d" % len(fresh)
        assert report["duplicates"] == 1, "应识别出 1 条批内重复"
        assert report["invalid"] == 1, "应识别出 1 条无链接"
        assert normalize_url("https://openai.com/index/gpt-5/?utm_source=hn") == (
            "https://openai.com/index/gpt-5"
        ), "归一化结果不符合 code_parse 的形式"
        assert normalize_url("blog.rust-lang.org/a/") == "https://blog.rust-lang.org/a", (
            "裸域名应补全 https 并去掉尾斜杠"
        )

        store.record_pushed(fresh, today)
        assert len(store.filter_new(articles)) == 0, "写回之后同一批应该全部判重"
        store.record_pushed(fresh, today)
        assert store.stats()["total_all_time"] == 2, "重复写回不应该产生第二行"

        history = json.loads(store.history_items())
        assert len(history) == 2 and all(row["title"] for row in history), (
            "历史导出必须带标题，语义去重节点要靠它比对"
        )
        assert store.history_urls().count("\n") == 1, "history_urls 应为换行分隔的 2 条"

        store.record_pushed([{"url": "https://example.com/old", "title": "旧条目"}], "2020-01-01")
        assert store.prune() == 1, "过期记录应被清理"
        assert store.stats()["total_all_time"] == 2, "清理不应误删窗口内的记录"

        try:
            store.record_pushed([{"url": "https://example.com/x"}], None)
        except NewsStoreError as error:
            raise AssertionError("缺少 run_date 不该导致写回失败：%s" % error)
        store.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("NewsStore 自检通过")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="已处理 URL 的本地库（替代飞书多维表格）")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="库文件路径")
    parser.add_argument("--days", type=int, default=None, help="历史窗口天数，默认用库里的 history_days")
    parser.add_argument("--stats", action="store_true", help="打印按天按源的统计")
    parser.add_argument("--prune", action="store_true", help="清理历史窗口外的记录")
    parser.add_argument("--import-history", action="store_true", help="迁移 scheduler/state/history.json")
    parser.add_argument("--self-test", action="store_true", help="跑自检，不碰真实库")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    try:
        store = NewsStore(args.db)
    except NewsStoreError as error:
        print(error)
        return 1

    with store:
        if args.import_history:
            count = store.import_history_json()
            print("已从 history.json 迁移 %d 条" % count)
        if args.prune:
            print("已清理 %d 条过期记录" % store.prune(args.days))
        if args.stats or not any((args.import_history, args.prune)):
            print(json.dumps(store.stats(args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
