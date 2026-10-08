"""调度脚本的端到端串联测试：不连 Dify、不推送，验证状态写回的时机和内容。

覆盖三条链路：
  1. 推送成功 -> 条目写进本地库（带标题），下一次运行能把标题当作语义去重的历史
  2. 推送失败 -> 一条都不写，日报落盘待补发，下次还会再抓一遍
  3. 工作流只回 pushed_urls（老版本 DSL）-> 依然能写回，只是没有标题

跑法：python -m unittest tools.test_scheduler_flow -v
"""

import datetime
import json
import os
import shutil
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "tools"))
sys.path.insert(0, os.path.join(BASE_DIR, "scheduler"))

import news_store  # noqa: E402
import run_daily  # noqa: E402

WORK_DIR = os.path.join(BASE_DIR, "scheduler", "state", "_test_flow")


def workflow_response(items, stats=None):
    """造一个 Dify workflow 的成功响应，字段和真实返回一致。"""
    urls = [row["url"] for row in items]
    stats = stats or {
        "merged_raw": 100,
        "after_url_dedup": 20,
        "semantic_in": 20,
        "semantic_kept": len(items),
        "semantic_dropped": 20 - len(items),
        "semantic_suspect": 1,
        "semantic_encoder": "lexical",
        "max_history_score": 0.42,
    }
    markdown = "# AI 日报 · %s\n\n## 今日头条\n\n" % datetime.date.today().isoformat()
    markdown += "\n".join("### %s\n来源：[原文](%s)\n" % (row["title"], row["url"]) for row in items)
    return {
        "workflow_run_id": "test-run",
        "data": {
            "status": "succeeded",
            "total_tokens": 100,
            "error": None,
            "outputs": {
                "markdown": markdown,
                "pushed_urls": urls,
                "pushed_items": items,
                "count": len(items),
                "stats": json.dumps(stats, ensure_ascii=False),
                "semantic_stats": json.dumps({
                    "semantic_kept": stats["semantic_kept"],
                    "semantic_dropped": stats["semantic_dropped"],
                    "semantic_suspect": stats["semantic_suspect"],
                    "semantic_encoder": stats["semantic_encoder"],
                    "max_history_score": stats["max_history_score"],
                }, ensure_ascii=False),
            },
        },
    }


class SchedulerFlowTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        os.makedirs(WORK_DIR, exist_ok=True)
        self.db_path = os.path.join(WORK_DIR, "news.db")
        self.config_path = os.path.join(WORK_DIR, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "api_base": "http://localhost/v1",
                "api_key": "app-test",
                "pushplus_token": "test-token",
                "history_days": 30,
                "timeout_seconds": 5,
                "user": "test",
                "store_path": self.db_path,
            }, handle)

        # 把调度脚本的所有外部路径都指到工作目录，别碰真实状态
        self.original = {
            "CONFIG_PATH": run_daily.CONFIG_PATH,
            "HISTORY_PATH": run_daily.HISTORY_PATH,
            "LOG_DIR": run_daily.LOG_DIR,
            "OUTBOX_DIR": run_daily.OUTBOX_DIR,
            "call_workflow": run_daily.call_workflow,
            "push_to_pushplus": run_daily.push_to_pushplus,
        }
        run_daily.CONFIG_PATH = self.config_path
        run_daily.HISTORY_PATH = os.path.join(WORK_DIR, "history.json")
        run_daily.LOG_DIR = os.path.join(WORK_DIR, "logs")
        run_daily.OUTBOX_DIR = os.path.join(WORK_DIR, "outbox")
        sys.argv = ["run_daily.py"]

    def tearDown(self):
        run_daily.CONFIG_PATH = self.original["CONFIG_PATH"]
        run_daily.HISTORY_PATH = self.original["HISTORY_PATH"]
        run_daily.LOG_DIR = self.original["LOG_DIR"]
        run_daily.OUTBOX_DIR = self.original["OUTBOX_DIR"]
        run_daily.call_workflow = self.original["call_workflow"]
        run_daily.push_to_pushplus = self.original["push_to_pushplus"]
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    def test_push_success_writes_history_with_titles(self):
        items = [
            {"url": "https://openai.com/index/gpt-6", "title": "GPT-6 发布", "origin": "OpenAI Blog"},
            {"url": "https://news.mit.edu/x", "title": "MIT 新闻", "origin": "Hacker News"},
        ]
        seen = {}

        def fake_workflow(config, history_urls, history_items):
            seen["urls"] = history_urls
            seen["items"] = history_items
            return workflow_response(items)

        run_daily.call_workflow = fake_workflow
        run_daily.push_to_pushplus = lambda config, markdown, today: (True, "ok")

        self.assertEqual(run_daily.main(), 0)
        with news_store.NewsStore(self.db_path) as store:
            self.assertEqual(store.stats()["total_all_time"], 2)
            sources = {row["source"] for row in store.stats()["by_source"]}
            self.assertEqual(sources, {"OpenAI Blog", "Hacker News"})
        # 第一次运行没有历史，两边都应该是空的
        self.assertEqual(seen["urls"], "")
        self.assertEqual(json.loads(seen["items"]), [])

        # 第二次运行：历史里必须有标题，语义去重才有文本可比
        run_daily.main()
        self.assertIn("https://openai.com/index/gpt-6", seen["urls"])
        history = json.loads(seen["items"])
        self.assertEqual(len(history), 2, "第二次运行应该带上 2 条历史")
        self.assertTrue(all(row["title"] for row in history), "历史必须带标题")
        self.assertEqual({row["origin"] for row in history}, {"OpenAI Blog", "Hacker News"})

        # 语义计数来自 code_semantic 节点，调度脚本要把它并进日志，周报才有数
        log_files = sorted(os.listdir(run_daily.LOG_DIR))
        with open(os.path.join(run_daily.LOG_DIR, log_files[-1]), encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(record["semantic_dedup"]["dropped"], 18)
        self.assertEqual(record["stats"]["semantic_encoder"], "lexical")

        # history.json 是镜像，应该也有 2 条
        with open(run_daily.HISTORY_PATH, encoding="utf-8") as handle:
            mirror = json.load(handle)
        self.assertEqual(len(mirror), 2)
        self.assertTrue(all(row.get("title") for row in mirror))

    def test_push_failure_keeps_history_untouched(self):
        items = [{"url": "https://openai.com/index/gpt-6", "title": "GPT-6 发布", "origin": "OpenAI Blog"}]
        run_daily.call_workflow = lambda config, urls, items_json: workflow_response(items)
        run_daily.push_to_pushplus = lambda config, markdown, today: (False, "接口 500")

        self.assertEqual(run_daily.main(), 0)
        with news_store.NewsStore(self.db_path) as store:
            self.assertEqual(store.stats()["total_all_time"], 0, "推送失败不能写历史，否则第二天会漏掉这条")
        outbox = os.path.join(run_daily.OUTBOX_DIR, datetime.date.today().isoformat() + ".md")
        self.assertTrue(os.path.exists(outbox), "推送失败必须落盘待补发")

    def test_legacy_workflow_without_titles_still_writes(self):
        urls = ["https://openai.com/index/gpt-6"]
        response = workflow_response([{"url": urls[0], "title": "GPT-6 发布", "origin": "OpenAI Blog"}])
        del response["data"]["outputs"]["pushed_items"]  # 模拟老版本 DSL 只回 pushed_urls
        run_daily.call_workflow = lambda config, urls_arg, items_arg: response
        run_daily.push_to_pushplus = lambda config, markdown, today: (True, "ok")

        self.assertEqual(run_daily.main(), 0)
        with news_store.NewsStore(self.db_path) as store:
            self.assertEqual(store.stats()["total_all_time"], 1)
            row = store.history_rows()[0]
            self.assertEqual(row["url"], urls[0])
            self.assertEqual(row["title"], "", "没有 pushed_items 时标题为空，但不该失败")


if __name__ == "__main__":
    unittest.main()
