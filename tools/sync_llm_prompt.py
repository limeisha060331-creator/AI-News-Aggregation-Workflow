"""把 prompts/llm_daily_report_prompt.md 里的提示词同步进 dify-ai-news-v1.yml 的 llm_sum 节点。

提示词只有这一个来源：改完 md 就跑这个脚本，再跑 validate_dsl.py 校验。
脚本是幂等的：内容已经一致时不会改动文件。

用法：D:\\anaconda\\python.exe tools\\sync_llm_prompt.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSL_PATH = os.path.join(BASE, "dify-ai-news-v1.yml")
PROMPT_PATH = os.path.join(BASE, "prompts", "llm_daily_report_prompt.md")

SYSTEM_START = "<!-- SYSTEM_PROMPT_START -->"
SYSTEM_END = "<!-- SYSTEM_PROMPT_END -->"
USER_START = "<!-- USER_PROMPT_START -->"
USER_END = "<!-- USER_PROMPT_END -->"

NODE_ID_LINE = "      id: llm_sum\n"
PROMPT_HEADER = "        prompt_template:\n"
PROMPT_TAIL = "        retry_config:\n"
ROLE_INDENT = "        "            # prompt_template 的列表项缩进
TEXT_INDENT = "          "          # text: | 的缩进
BODY_INDENT = "            "        # text: | 块内容的缩进


def extract_block(source: str, start_marker: str, end_marker: str) -> str:
    """取出标记之间的 text 代码块内容（去掉围栏和首尾空行）。"""
    start = source.find(start_marker)
    end = source.find(end_marker)
    if start < 0 or end < 0 or end < start:
        raise SystemExit("!! %s 里找不到 %s / %s" % (PROMPT_PATH, start_marker, end_marker))

    lines = source[start + len(start_marker) : end].strip().split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def render_prompt_block() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as handle:
        source = handle.read()

    system_text = extract_block(source, SYSTEM_START, SYSTEM_END)
    user_text = extract_block(source, USER_START, USER_END)
    if not system_text or not user_text:
        raise SystemExit("!! 提示词不能为空：system=%d 字，user=%d 字" % (len(system_text), len(user_text)))

    lines = [PROMPT_HEADER.rstrip("\n")]
    for role, text in (("system", system_text), ("user", user_text)):
        lines.append("%s- role: %s" % (ROLE_INDENT, role))
        lines.append("%stext: |" % TEXT_INDENT)
        for line in text.split("\n"):
            lines.append(BODY_INDENT + line if line else "")
    return "\n".join(lines) + "\n"


def main() -> int:
    with open(DSL_PATH, encoding="utf-8") as handle:
        dsl = handle.read()

    id_at = dsl.find(NODE_ID_LINE)
    if id_at < 0:
        print("!! dify-ai-news-v1.yml 里找不到 llm_sum 节点")
        return 1
    header_at = dsl.rfind(PROMPT_HEADER, 0, id_at)
    if header_at < 0:
        print("!! llm_sum 节点里找不到 prompt_template")
        return 1
    tail_at = dsl.find(PROMPT_TAIL, header_at)
    if tail_at < 0:
        print("!! llm_sum 的 prompt_template 没有以 retry_config 收尾")
        return 1

    new_block = render_prompt_block()
    if dsl[header_at:tail_at] == new_block:
        print("llm_sum 提示词已是最新，未改动文件")
        return 0

    updated = dsl[:header_at] + new_block + dsl[tail_at:]
    with open(DSL_PATH, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    print("已把提示词同步进 llm_sum 节点，共 %d 行" % new_block.count("\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
