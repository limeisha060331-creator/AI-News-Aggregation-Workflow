"""读取 scheduler/logs/*.json，输出周报：漏斗、源质量、去重阈值评估。

数据来源只有一个：调度脚本每次运行落盘的日志。
不建飞书表格的理由和 URL 去重那块一样——日志本来就是结构化 JSON，
再抄一份到外部表里，等于多一个会写脏、会漂移的副本。

用法：
    python tools/weekly_report.py                   最近 7 天，Markdown 到终端
    python tools/weekly_report.py --days 30         改窗口
    python tools/weekly_report.py --out report.md   写文件
    python tools/weekly_report.py --json            输出聚合结果，便于再接别的工具
"""

import argparse
import datetime
import glob
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "scheduler", "logs")

# 日志里的源标识 -> 展示名。stats 用缩写，selected_by_source 用全名，这里统一
SOURCE_LABELS = {"hn": "Hacker News", "openai": "OpenAI Blog", "jqzx": "量子位"}
SOURCE_ALIASES = {
    "hn": "hn", "hacker news": "hn",
    "openai": "openai", "openai blog": "openai",
    "jqzx": "jqzx", "量子位": "jqzx", "机器之心": "jqzx",
}
# 抓取数字段名。第三源改过名：早期节点写 jqzx_raw，现在是 qbitai_raw，
# 只认其中一个会把另一个源的历史数据统计成 0
SOURCE_RAW_KEYS = {
    "hn": ("hn_raw",),
    "openai": ("openai_raw",),
    "jqzx": ("qbitai_raw", "jqzx_raw"),
}


def raw_count(stats, key):
    """取某个源的抓取数，返回 (条数, 日志里是否记录了该字段)。

    字段缺失和「明确记了 0 条」是两回事：缺失说明那次运行的节点版本没这个字段，
    不能算成零产出，否则源质量统计会把老日志误判成源失效。
    """
    for name in SOURCE_RAW_KEYS[key]:
        value = stats.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value), True
    return 0, False


def load_runs(log_dir=LOG_DIR):
    """读全部运行日志，按日期排序。坏文件跳过并记一行警告。"""
    runs, broken = [], []
    for path in sorted(glob.glob(os.path.join(log_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as error:
            broken.append((os.path.basename(path), str(error)))
            continue
        if not isinstance(record, dict):
            broken.append((os.path.basename(path), "不是 JSON 对象"))
            continue
        stats = record.get("stats")
        if isinstance(stats, str):
            try:
                stats = json.loads(stats)
            except ValueError:
                stats = None
        record["stats"] = stats if isinstance(stats, dict) else {}
        record["_file"] = os.path.basename(path)
        runs.append(record)
    runs.sort(key=lambda row: (row.get("date") or "", row["_file"]))
    return runs, broken


def in_window(runs, days):
    """只保留最近 days 天的运行记录（按 date 字段）。"""
    if not days:
        return runs
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [row for row in runs if (row.get("date") or "") >= cutoff]


def aggregate(runs):
    """把每次运行的日志聚成一个字典，缺字段一律按 0 处理。"""
    summary = {
        "runs": len(runs),
        "succeeded": 0,
        "failed": 0,
        "push_ok": 0,
        "push_failed": 0,
        "total_tokens": 0,
        "elapsed_seconds": 0.0,
        "funnel": {"merged_raw": 0, "after_url_dedup": 0, "semantic_kept": 0,
                   "semantic_dropped": 0, "pushed": 0, "selected": 0},
        "by_day": [],
        "sources": {},
        "semantic": {"runs_with_data": 0, "dropped": 0, "suspect": 0,
                     "max_history_score": [], "dropped_detail": [], "suspect_detail": [],
                     "runs_without_data": 0},
        "issues": [],
    }

    for label in SOURCE_LABELS.values():
        summary["sources"][label] = {"raw": 0, "selected": 0, "missing_runs": 0, "days": 0}

    for record in runs:
        stats = record.get("stats") or {}
        status = record.get("status")
        push = record.get("push") or {}
        ok = status == "succeeded"
        summary["succeeded" if ok else "failed"] += 1
        if push.get("ok"):
            summary["push_ok"] += 1
        elif push:
            summary["push_failed"] += 1

        tokens = record.get("total_tokens")
        if isinstance(tokens, (int, float)):
            summary["total_tokens"] += int(tokens)
        elapsed = record.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            summary["elapsed_seconds"] += float(elapsed)

        def number(key):
            value = stats.get(key)
            return int(value) if isinstance(value, (int, float)) else 0

        merged = number("merged_raw")
        after_url = number("after_url_dedup")
        semantic_dropped = number("semantic_dropped")
        pushed = int(record.get("count") or 0)
        has_semantic = "semantic_kept" in stats or "semantic_dropped" in stats
        # 语义去重跑在 TOP_N 截断之后的候选上，不是跑在全部去重结果上。
        # 老日志没有 selected 字段，就退回用 URL 去重后的条数当候选数，避免凭空造一级。
        selected = number("selected") or (after_url if "selected" not in stats else 0)
        semantic_kept = number("semantic_kept") if has_semantic else selected

        summary["funnel"]["merged_raw"] += merged
        summary["funnel"]["after_url_dedup"] += after_url
        summary["funnel"]["selected"] += selected
        summary["funnel"]["semantic_kept"] += semantic_kept
        summary["funnel"]["semantic_dropped"] += semantic_dropped
        summary["funnel"]["pushed"] += pushed

        day_row = {
            "date": record.get("date"),
            "status": status,
            "merged_raw": merged,
            "after_url_dedup": after_url,
            "selected": selected,
            "semantic_dropped": semantic_dropped,
            "pushed": pushed,
            "history_used": record.get("history_used"),
            "push_ok": bool(push.get("ok")),
        }
        summary["by_day"].append(day_row)

        selected_by_source = stats.get("selected_by_source") or {}
        for key, label in SOURCE_LABELS.items():
            entry = summary["sources"][label]
            raw, reported = raw_count(stats, key)
            if reported:
                entry["raw"] += raw
                entry["days"] += 1
                if raw == 0:
                    entry["missing_runs"] += 1
        for name, count in selected_by_source.items():
            key = SOURCE_ALIASES.get(str(name).strip().lower())
            if key:
                summary["sources"][SOURCE_LABELS[key]]["selected"] += int(count or 0)

        if has_semantic:
            summary["semantic"]["runs_with_data"] += 1
            summary["semantic"]["dropped"] += semantic_dropped
            summary["semantic"]["suspect"] += number("semantic_suspect")
            score = stats.get("max_history_score")
            if isinstance(score, (int, float)) and score:
                summary["semantic"]["max_history_score"].append(round(float(score), 4))
            summary["semantic"]["dropped_detail"].extend(stats.get("dropped_detail") or [])
            summary["semantic"]["suspect_detail"].extend(stats.get("suspect_detail") or [])
        else:
            summary["semantic"]["runs_without_data"] += 1

        for warning in stats.get("warnings") or []:
            summary["issues"].append("%s 警告：%s" % (record.get("date"), warning))
        for missing in stats.get("missing_sources") or []:
            summary["issues"].append("%s 无产出源：%s" % (record.get("date"), missing))
        if not ok:
            summary["issues"].append("%s 运行失败：%s" % (record.get("date"), record.get("error") or status))
        elif not push.get("ok"):
            summary["issues"].append("%s 推送失败：%s" % (record.get("date"), push.get("detail")))

    summary["elapsed_seconds"] = round(summary["elapsed_seconds"], 1)
    return summary


def render_markdown(summary, days, broken):
    """把聚合结果渲染成 Markdown 周报。"""
    lines = []
    window = "最近 %d 天" % days if days else "全部记录"
    lines.append("# AI 日报 · 运行周报（%s）" % window)
    lines.append("")
    lines.append("生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    lines.append("")

    lines.append("## 运行概览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 运行次数 | %d |" % summary["runs"])
    lines.append("| 成功 / 失败 | %d / %d |" % (summary["succeeded"], summary["failed"]))
    lines.append("| 推送成功 / 失败 | %d / %d |" % (summary["push_ok"], summary["push_failed"]))
    lines.append("| 累计耗时 | %.1f 秒 |" % summary["elapsed_seconds"])
    lines.append("| 累计 tokens | %d |" % summary["total_tokens"])
    lines.append("")

    funnel = summary["funnel"]
    lines.append("## 漏斗")
    lines.append("")
    lines.append("| 阶段 | 条数 | 相对上一级 |")
    lines.append("| --- | --- | --- |")
    lines.append("| 抓取合并 | %d | — |" % funnel["merged_raw"])
    lines.append("| URL 去重后 | %d | %s |" % (funnel["after_url_dedup"], _ratio(funnel["after_url_dedup"], funnel["merged_raw"])))
    lines.append("| 进入候选（截断后） | %d | %s |" % (funnel["selected"], _ratio(funnel["selected"], funnel["after_url_dedup"])))
    lines.append("| 语义去重后 | %d | %s |" % (funnel["semantic_kept"], _ratio(funnel["semantic_kept"], funnel["selected"])))
    lines.append("| 实际推送 | %d | %s |" % (funnel["pushed"], _ratio(funnel["pushed"], funnel["semantic_kept"])))
    lines.append("")
    lines.append("> 语义去重跑在截断之后的候选上，所以它跟 URL 去重不是同一批条目，两级要分开看："
                 "URL 去重反映同一链接的重复，截断反映每条源配额与 TOP_N 上限，"
                 "语义去重反映同一事件不同链接的重复。")
    lines.append(">")
    lines.append("> 语义去重累计拦下 %d 条；其中 %d 次运行没有回传语义数据，这几级按候选数计。"
                 % (funnel["semantic_dropped"], summary["semantic"]["runs_without_data"]))
    lines.append("")

    if summary["by_day"]:
        lines.append("## 每日明细")
        lines.append("")
        lines.append("| 日期 | 状态 | 抓取 | URL 去重后 | 候选 | 语义判重 | 推送 | 历史条目 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in summary["by_day"]:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                row["date"], row["status"] or "-", row["merged_raw"], row["after_url_dedup"],
                row["selected"], row["semantic_dropped"], row["pushed"],
                "-" if row["history_used"] is None else row["history_used"],
            ))
        lines.append("")

    lines.append("## 源质量")
    lines.append("")
    lines.append("| 源 | 累计抓取 | 累计入选 | 入选率 | 零产出天数 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for label, entry in summary["sources"].items():
        lines.append("| %s | %d | %d | %s | %d |" % (
            label, entry["raw"], entry["selected"],
            _ratio(entry["selected"], entry["raw"]), entry["missing_runs"],
        ))
    lines.append("")
    lines.append("> 零产出天数连续增长说明这个源的 feed 已经失效，该换地址或下线，"
                 "不能只看「日报还有内容」就当它正常。")
    lines.append("")

    lines.append("## 去重阈值评估")
    lines.append("")
    sem = summary["semantic"]
    if sem["runs_with_data"]:
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        lines.append("| 参与统计的运行 | %d |" % sem["runs_with_data"])
        lines.append("| 语义判重条目 | %d |" % sem["dropped"])
        lines.append("| 疑似重复条目 | %d |" % sem["suspect"])
        if sem["max_history_score"]:
            lines.append("| 历史最高相似度 | %.3f（%d 次运行） |" % (
                max(sem["max_history_score"]), len(sem["max_history_score"])))
        lines.append("")
        if sem["dropped"] == 0 and sem["suspect"] == 0:
            lines.append("- 一条都没命中：阈值可能偏高，或向量抓不到重复（跨语言场景尤其明显）。")
        if sem["suspect"] > sem["dropped"] * 3 and sem["suspect"] > 5:
            lines.append("- 疑似远多于判重：gray_low 太低，llm_sum 承担了过多判断，考虑上调。")
        if sem["suspect_detail"]:
            lines.append("")
            lines.append("疑似重复样本（确认是同一事件的，说明阈值该往下调）：")
            lines.append("")
            for row in sem["suspect_detail"][:10]:
                lines.append("- `%s` ≈ `%s`（%.3f）" % (
                    row.get("title") or row.get("url"), row.get("similar_to"), row.get("score") or 0))
    else:
        lines.append("工作流还没回传语义去重指标。")
        lines.append("")
        lines.append("`code_semantic` 节点的 stats 里带了 `semantic_kept` / `semantic_dropped` / "
                     "`semantic_suspect` / `max_history_score`，工作流跑过一次之后这里自动就有数。")
    lines.append("")

    lines.append("## 摘要可用率")
    lines.append("")
    lines.append("**未采集。** 现在摘要是 `llm_sum` 一次性写完整篇日报的副产品，"
                 "没有逐条结构化的摘要字段，所以算不出这个指标。"
                 "要采集需要让 LLM 节点输出逐条 JSON（摘要 + 评分），再由 code_build 组装成 Markdown。")
    lines.append("")

    if summary["issues"]:
        lines.append("## 异常清单")
        lines.append("")
        for issue in summary["issues"]:
            lines.append("- %s" % issue)
        lines.append("")
    if broken:
        lines.append("## 读取失败的日志文件")
        lines.append("")
        for name, error in broken:
            lines.append("- %s：%s" % (name, error))
        lines.append("")
    return "\n".join(lines)


def _ratio(part, whole):
    """百分比字符串，分母为 0 时返回 —。"""
    try:
        part, whole = float(part), float(whole)
    except (TypeError, ValueError):
        return "—"
    if whole <= 0:
        return "—"
    return "%.1f%%" % (part / whole * 100)


def main(argv=None):
    parser = argparse.ArgumentParser(description="读取运行日志，输出周报")
    parser.add_argument("--logs", default=LOG_DIR, help="日志目录")
    parser.add_argument("--days", type=int, default=7, help="统计窗口天数，0 表示全部")
    parser.add_argument("--out", default="", help="把 Markdown 写到指定文件")
    parser.add_argument("--json", action="store_true", help="输出聚合结果的 JSON")
    args = parser.parse_args(argv)

    runs, broken = load_runs(args.logs)
    if not runs:
        print("在 %s 里没找到运行日志" % args.logs)
        return 1

    summary = aggregate(in_window(runs, args.days or None))
    if args.json:
        print(json.dumps({"summary": summary, "broken": broken}, ensure_ascii=False, indent=2))
        return 0

    markdown = render_markdown(summary, args.days, broken)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown.rstrip() + "\n")
        print("周报已写入 %s" % args.out)
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
