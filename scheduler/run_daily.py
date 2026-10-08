"""AI 资讯日报调度器：触发 Dify Workflow，推送微信，维护跨天去重历史，落盘运行日志。

职责划分：
  - 工作流负责抓取、去重、摘要、组装 Markdown（无状态）
  - 本脚本负责触发时机、推送 PushPlus、跨天去重状态、失败落盘、运行日志（有状态）

跨天状态存在两个地方：
  - state/news.db（SQLite，主）：URL 去重的 key、语义去重要用的历史标题，都由它供数
  - state/history.json（镜像）：给人看的副本，方便直接打开翻，不作为权威来源
  本地库优先；库打不开时退回只读 history.json，工作流照跑，不会因为存储问题停摆。

推送为什么放在这里而不是工作流里：
  Dify 的 HTTP 节点只看 HTTP 状态码，PushPlus/飞书这类接口业务失败时
  依然返回 200，工作流会显示"成功"，历史库也会被写脏。
  放在这里可以拿到响应体里的 code 字段，推送失败就不写历史、只落盘待补发。

为什么要传两份历史给工作流：
  history_urls  给 code_parse 做跨天 URL 去重（精确匹配）
  history_items 给 code_semantic 做跨天语义去重（带标题，比向量）

用法：
    python run_daily.py                正常跑一次
    python run_daily.py --dry-run      只检查配置和历史，不调用 Dify
    python run_daily.py --no-push      真跑工作流看结果，但不推送、不写历史、不落日志
    python run_daily.py --no-history   忽略历史强制重跑（会重复推送已推送过的条目）

产出文件：
    state/news.db             跨天去重的权威存储（URL + 标题 + 来源 + 日期）
    state/history.json        同上的人类可读镜像
    logs/<时间戳>.json        每次运行的完整结果，含各阶段计数
    outbox/<日期>.md          推送失败时，日报 Markdown 落盘待补发
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(os.path.dirname(BASE_DIR), "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from news_store import DEFAULT_DB_PATH, NewsStore, NewsStoreError  # noqa: E402 - 需要先把 tools 加进 sys.path

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "state", "history.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def log(message):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("找不到 %s" % CONFIG_PATH)
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        config = json.load(handle)
    if not config.get("api_key"):
        raise SystemExit("config.json 里的 api_key 还是空的。在 Dify 应用里打开「访问 API」创建密钥后再跑。")
    config.setdefault("api_base", "http://localhost/v1")
    config.setdefault("history_days", 30)
    config.setdefault("timeout_seconds", 600)
    config.setdefault("user", "daily-scheduler")
    config.setdefault("pushplus_token", "")
    config.setdefault("pushplus_title", "AI 日报")
    return config


def load_history(days):
    """读 history.json 镜像（只有 URL 和日期，没有标题）。本地库不可用时才走这里。"""
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as handle:
        rows = json.load(handle)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [row for row in rows if (row.get("date") or "") >= cutoff]


def save_history(rows):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)


def open_store(config):
    """打开本地库；打不开就返回 (None, 原因)，让调用方退回 history.json。"""
    try:
        store = NewsStore(
            config.get("store_path") or DEFAULT_DB_PATH,
            history_days=config["history_days"],
        )
    except NewsStoreError as error:
        return None, str(error)

    # 首次运行时把旧的 history.json 迁进来，避免丢掉已经推送过的 URL
    try:
        if store.stats()["total_all_time"] == 0 and os.path.exists(HISTORY_PATH):
            count = store.import_history_json(HISTORY_PATH)
            if count:
                log("已把 history.json 里的 %d 条迁移进本地库" % count)
    except NewsStoreError as error:
        return store, "读取本地库失败：%s" % error
    return store, ""


def collect_history(store, days):
    """返回 (history_urls 换行串, history_items JSON, 历史条数)。

    优先用本地库（带标题，语义去重需要）；库不可用时退回 history.json，
    这时只有 URL，语义去重拿不到文本，会退化成只做当天比对。
    """
    rows = []
    if store is not None:
        try:
            rows = store.history_rows(days)
        except NewsStoreError as error:
            log("读本地库失败（%s），退回 history.json" % error)
            rows = []
    if not rows:
        rows = load_history(days)

    urls = [row.get("url") for row in rows if row.get("url")]
    items = [
        {
            "url": row.get("url"),
            "title": row.get("title") or "",
            "raw": (row.get("raw") or "")[:128],
            "origin": row.get("source") or row.get("origin") or "",
        }
        for row in rows
        if row.get("url") and (row.get("title") or "")
    ]
    return "\n".join(urls), json.dumps(items, ensure_ascii=False), len(rows)


def as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            return [value]
    return []


def call_workflow(config, history_urls, history_items):
    payload = json.dumps(
        {
            "inputs": {"history_urls": history_urls, "history_items": history_items},
            "response_mode": "blocking",
            "user": config["user"],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        config["api_base"].rstrip("/") + "/workflows/run",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
        return json.loads(response.read().decode("utf-8"))


def push_to_pushplus(config, markdown, today):
    """把日报推到微信（PushPlus）。返回 (是否成功, 说明)。"""
    token = (config.get("pushplus_token") or "").strip()
    if not token:
        return False, "config.json 里没有填 pushplus_token"

    payload = json.dumps(
        {
            "token": token,
            "title": "%s · %s" % (config.get("pushplus_title") or "AI 日报", today),
            "content": markdown,
            "template": "markdown",
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        PUSHPLUS_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=config["timeout_seconds"]) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - 推送失败要落盘，不能中断
        return False, "请求异常：%s" % error

    if body.get("code") == 200:
        return True, "消息 id %s" % body.get("data")
    return False, "code=%s msg=%s" % (body.get("code"), body.get("msg"))


def write_log(payload):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    path = os.path.join(LOG_DIR, stamp + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def write_outbox(markdown, today):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    path = os.path.join(OUTBOX_DIR, today + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    return path


def main():
    parser = argparse.ArgumentParser(description="触发 Dify AI 资讯日报工作流")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置和历史，不调用 Dify")
    parser.add_argument("--no-push", action="store_true",
                        help="真跑工作流但不推送、不写历史、不落日志，用于验证改动")
    parser.add_argument("--no-history", action="store_true", help="忽略历史，强制重跑")
    args = parser.parse_args()

    config = load_config()
    today = datetime.date.today().isoformat()

    store, store_problem = open_store(config)
    if store_problem:
        log("本地库不可用（%s），本次退回 history.json" % store_problem)

    if args.no_history:
        history_urls, history_items, history_count = "", "[]", 0
    else:
        history_urls, history_items, history_count = collect_history(store, config["history_days"])
    log("历史窗口 %d 天，载入 %d 条已推送条目（语义去重可用文本 %d 条）"
        % (config["history_days"], history_count, history_items.count('"title"')))

    if args.dry_run:
        if store is not None:
            store.close()
        log("dry-run 结束，未调用 Dify")
        return 0

    log("开始调用 Dify workflow ...")
    started = time.time()
    try:
        response = call_workflow(config, history_urls, history_items)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        log("HTTP %s：%s" % (error.code, body[:400]))
        write_log({"date": today, "status": "http_error", "http_status": error.code, "body": body[:2000]})
        if store is not None:
            store.close()
        return 1
    except Exception as error:  # noqa: BLE001 - 调度脚本要把任何异常都记下来
        log("调用失败：%s" % error)
        write_log({"date": today, "status": "exception", "error": str(error)})
        if store is not None:
            store.close()
        return 1

    elapsed = round(time.time() - started, 1)
    data = response.get("data") or {}
    status = data.get("status")
    outputs = data.get("outputs") or {}
    pushed_urls = as_list(outputs.get("pushed_urls"))
    pushed_items = as_list(outputs.get("pushed_items"))
    count = outputs.get("count")
    markdown = outputs.get("markdown") or ""

    stats = outputs.get("stats")
    if isinstance(stats, str) and stats.strip():
        try:
            stats = json.loads(stats)
        except ValueError:
            pass
    # 语义去重的计数来自 code_semantic 节点，不在 code_parse 的 stats 里，单独合进来
    semantic_stats = outputs.get("semantic_stats")
    if isinstance(semantic_stats, str) and semantic_stats.strip():
        try:
            semantic_stats = json.loads(semantic_stats)
        except ValueError:
            semantic_stats = None
    if isinstance(stats, dict) and isinstance(semantic_stats, dict):
        stats.update({key: value for key, value in semantic_stats.items() if key not in stats})
    elif isinstance(semantic_stats, dict):
        stats = semantic_stats

    record = {
        "date": today,
        "status": status,
        "elapsed_seconds": elapsed,
        "workflow_run_id": response.get("workflow_run_id"),
        "total_tokens": data.get("total_tokens"),
        "count": count,
        "stats": stats,
        "pushed_urls": pushed_urls,
        "error": data.get("error"),
        "history_used": history_count,
    }
    if isinstance(stats, dict):
        record["semantic_dedup"] = {
            "kept": stats.get("semantic_kept"),
            "dropped": stats.get("semantic_dropped"),
            "suspect": stats.get("semantic_suspect"),
            "encoder": stats.get("semantic_encoder"),
            "max_history_score": stats.get("max_history_score"),
        }

    if status != "succeeded":
        log("工作流执行失败：%s" % (data.get("error") or status))
        write_log(record)
        if store is not None:
            store.close()
        return 1

    log("工作流完成，耗时 %.1fs，推送 %s 条，tokens=%s" % (elapsed, count, data.get("total_tokens")))
    if isinstance(stats, dict):
        log("各阶段计数：%s" % stats)
        if stats.get("semantic_in") is not None:
            log("语义去重：保留 %s / 判重 %s / 疑似 %s（编码器 %s，历史最高相似度 %s）" % (
                stats.get("semantic_kept"), stats.get("semantic_dropped"),
                stats.get("semantic_suspect"), stats.get("semantic_encoder"),
                stats.get("max_history_score"),
            ))

    if not markdown.strip() and not args.no_push:
        log("注意：本次没有产出内容")

    if args.no_push:
        push_ok, push_detail = True, "no-push：跳过推送"
    else:
        push_ok, push_detail = push_to_pushplus(config, markdown, today)
    record["push"] = {"ok": push_ok, "detail": push_detail}
    if push_ok:
        log("推送结果：%s" % push_detail)
    else:
        path = write_outbox(markdown, today)
        record["outbox"] = path
        log("推送失败（%s），日报已落盘：%s" % (push_detail, path))

    # 只有真正送达才写历史库；推送失败时这些条目下次还会再抓一遍，避免漏掉一天的资讯
    if args.no_push:
        log("--no-push 模式：不写历史、不落日志")
    elif not push_ok:
        log("本次不写历史，这些条目下次还会再出现")
    elif pushed_urls or pushed_items:
        # 工作流回了 pushed_items（带标题）就写它；老版本只回 pushed_urls，照样能用
        records = pushed_items or [{"url": url} for url in pushed_urls]
        if store is not None:
            try:
                written = store.record_pushed(records, today)
                log("已写入本地库 %d 条，窗口内 %d 条"
                    % (written, store.stats()["in_window"]))
                # history.json 退化成人类可读的镜像，权威数据在 news.db
                save_history([
                    {"url": row["url"], "date": row["run_date"], "title": row.get("title") or ""}
                    for row in store.history_rows(config["history_days"])
                ])
            except NewsStoreError as error:
                log("写本地库失败（%s），改回直接写 history.json" % error)
                rows = [row for row in load_history(config["history_days"])
                        if row.get("url") not in set(pushed_urls)]
                rows.extend({"url": url, "date": today} for url in pushed_urls)
                save_history(rows)
        else:
            rows = [row for row in load_history(config["history_days"])
                    if row.get("url") not in set(pushed_urls)]
            rows.extend({"url": url, "date": today} for url in pushed_urls)
            save_history(rows)
            log("已写入 history.json，当前 %d 条" % len(rows))
    else:
        log("没有新条目，历史不变")

    if not args.no_push:
        record["log_path"] = write_log(record)
    if store is not None:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
