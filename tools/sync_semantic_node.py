"""把 dify-hn-workflow/code_node_semantic_dedup.py 同步进 dify-ai-news-v1.yml 的 code_semantic 节点。

改完语义去重节点代码后先跑这个脚本，再跑 validate_dsl.py 校验。
脚本是幂等的：内容已经一致时不会改动文件。

用法：python tools/sync_semantic_node.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSL_PATH = os.path.join(BASE, "dify-ai-news-v1.yml")
MODULE_PATH = os.path.join(BASE, "dify-hn-workflow", "code_node_semantic_dedup.py")

# 模块里这一段之后是本地标定/演示数据，不进入 DSL（与 code_hn 用同一个分隔标记）
DEMO_MARKER = "# ---------------------------------------------------------------- 手动运行示例数据"

NODE_ID_LINE = "      id: code_semantic\n"
CODE_HEADER = "        code: |\n"
CODE_TAIL = "        code_language:"
BODY_INDENT = "          "          # code: | 块内容的缩进，与其它 code 节点保持一致


def render_body() -> str:
    with open(MODULE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    if DEMO_MARKER in source:
        source = source[: source.index(DEMO_MARKER)]
    source = source.rstrip()
    lines = [(BODY_INDENT + line) if line else "" for line in source.split("\n")]
    return "\n".join(lines) + "\n"


def main() -> int:
    with open(DSL_PATH, encoding="utf-8") as handle:
        dsl = handle.read()

    id_at = dsl.find(NODE_ID_LINE)
    if id_at < 0:
        print("!! dify-ai-news-v1.yml 里找不到 code_semantic 节点")
        return 1
    header_at = dsl.rfind(CODE_HEADER, 0, id_at)
    if header_at < 0:
        print("!! code_semantic 节点里找不到 code: | 代码块")
        return 1
    body_at = header_at + len(CODE_HEADER)
    tail_at = dsl.find(CODE_TAIL, header_at)
    if tail_at < 0:
        print("!! code_semantic 的代码块没有以 code_language 收尾")
        return 1

    new_body = render_body()
    if dsl[body_at:tail_at] == new_body:
        print("code_semantic 内嵌代码已是最新，未改动文件")
        return 0

    updated = dsl[:body_at] + new_body + dsl[tail_at:]
    with open(DSL_PATH, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    print("已把语义去重节点同步进 code_semantic，共 %d 行" % new_body.count("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
