"""用真实网络数据跑一遍解析链路，确认三个源都能取到、解析结果正确。

它会读 dify-ai-news-v1.yml 里 http_hn / http_openai / http_qbitai 实际配置的 URL，
在本地执行 code_hn 与 code_parse 节点的代码，打印真实统计。
不调用 Dify，也不触发任何推送（推送由 scheduler/run_daily.py 负责）。

用法：
    D:\\anaconda\\python.exe tools\\smoke_real_sources.py
    D:\\anaconda\\python.exe tools\\smoke_real_sources.py <离线目录>

给了离线目录时不联网，直接读该目录下的 hn.json / openai.xml / qbitai.xml，
适合在拿不到外网的机器上复跑。
"""

import json
import os
import sys
import urllib.error
import urllib.request

import yaml

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSL_PATH = os.path.join(BASE, "dify-ai-news-v1.yml")


def load_nodes():
    with open(DSL_PATH, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return {node["id"]: node["data"] for node in doc["workflow"]["graph"]["nodes"]}


def load_main(node_id, nodes):
    namespace = {}
    exec(compile(nodes[node_id]["code"], node_id, "exec"), namespace)
    return namespace["main"]


def fetch(url, timeout=25):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def load_bodies_from_dir(path):
    bodies = {}
    for node_id, filename in (("http_hn", "hn.json"), ("http_openai", "openai.xml"), ("http_qbitai", "qbitai.xml")):
        file_path = os.path.join(path, filename)
        if os.path.exists(file_path):
            with open(file_path, encoding="utf-8") as handle:
                bodies[node_id] = handle.read()
            print("[源] %-12s 离线读取 %s" % (node_id, filename))
        else:
            bodies[node_id] = ""
            print("[源] %-12s 离线文件缺失 %s" % (node_id, filename))
    return bodies


def main():
    nodes = load_nodes()
    hn_main = load_main("code_hn", nodes)
    parse_main = load_main("code_parse", nodes)

    offline_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if offline_dir:
        bodies = load_bodies_from_dir(offline_dir)
    else:
        bodies = {}
        for node_id, label in (("http_hn", "Hacker News"), ("http_openai", "OpenAI Blog"), ("http_qbitai", "量子位")):
            url = nodes[node_id]["url"]
            try:
                bodies[node_id] = fetch(url)
                print("[源] %-12s OK   %s" % (label, url))
            except Exception as exc:                                # noqa: BLE001
                bodies[node_id] = ""
                print("[源] %-12s 失败 %s  (%s)" % (label, url, exc))

    hn = hn_main(bodies["http_hn"])
    print()
    print("[code_hn] count=%s skipped=%s deduplicated=%s error=%s"
          % (hn["count"], hn["skipped"], hn["deduplicated"], hn["error"] or "-"))
    for story in hn["stories"][:3]:
        print("   %-16s %-22s %s" % (story["time"] or "(无时间)", story["source"], story["title"][:42]))

    result = parse_main(
        hn_items=hn["stories"],
        openai_body=bodies["http_openai"],
        qbitai_body=bodies["http_qbitai"],
        history_urls="",
    )
    stats = json.loads(result["stats"])
    print()
    print("[code_parse] stats: %s" % json.dumps(stats, ensure_ascii=False))
    print("[code_parse] 选中 %d 条，按来源：" % len(result["items"]))
    for item in result["items"]:
        print("   %-12s %s" % (item["origin"], item["title"][:46]))

    if stats["selected_by_source"].get("HN", 0) == 0:
        print()
        print("!! HN 一条都没被选中，检查权重与限额设置")
        return 1
    return 0


if __name__ == "__main__":
    # 控制台默认 GBK，源标题里的 ‑ 之类的字符会让 print 直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
