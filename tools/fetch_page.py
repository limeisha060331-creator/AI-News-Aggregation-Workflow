"""读取任意 URL 的内容：网页正文转 Markdown，JSON / RSS 原样整理。

用法（在仓库根目录）：
    python tools/fetch_page.py https://example.com
    python tools/fetch_page.py https://news.ycombinator.com/item?id=1 --json
    python tools/fetch_page.py <url1> <url2> --out out/pages.json
    python tools/fetch_page.py https://example.com --out out/example.md
    python tools/fetch_page.py https://openai.com/news/rss.xml --quiet

特点：
- 只依赖标准库，不需要装包。
- 自动识别 HTML / JSON / XML(RSS)，按类型输出。
- HTML 走一遍轻量正文抽取（去导航、广告、评论区），转成 Markdown。
- 中文站点编码（gb2312/gbk）自动转换。
- 需要外网可达；代理请先配好系统代理或 HTTPS_PROXY。
"""

import argparse
import gzip
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zlib
from html import unescape
from html.parser import HTMLParser

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
DROP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "svg", "canvas",
    "video", "audio", "form", "button", "input", "select", "textarea",
    "dialog", "object", "embed", "link", "meta",
}
INLINE_TAGS = {
    "a", "strong", "b", "em", "i", "code", "span", "sup", "sub", "u",
    "mark", "small", "time", "abbr", "cite", "q", "s", "del", "ins",
    "kbd", "var", "label", "font", "big",
}
NOISE_RE = re.compile(
    r"(^|[-_\s])(comment|comments|share|sharing|social|related|recommend|recommended"
    r"|promo|promotion|advert|ads?|sponsor|sidebar|side-bar|footer|header|nav|navbar"
    r"|menu|breadcrumb|subscribe|newsletter|paywall|popup|modal|cookie|banner"
    r"|toolbar|tags?|rating|pagination|hot-list|tuiguang)([-_\s]|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- HTML 树


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)

    def error(self, message):  # pragma: no cover - HTMLParser 兼容用
        pass


def iter_nodes(node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            for grand in iter_nodes(child):
                yield grand


def node_text(node):
    parts = []

    def walk(current):
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in DROP_TAGS:
                walk(child)

    walk(node)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def depth_of(node):
    depth = 0
    current = node
    while current.parent is not None:
        depth += 1
        current = current.parent
    return depth


def link_density(node):
    total = len(node_text(node))
    if not total:
        return 1.0
    chars = sum(len(node_text(n)) for n in iter_nodes(node) if n.tag == "a")
    return min(chars / float(total), 1.0)


def score_node(node):
    text = node_text(node)
    length = len(text)
    if length < 140:
        return 0.0
    paragraph_chars = sum(len(node_text(n)) for n in iter_nodes(node) if n.tag == "p")
    punctuation = len(re.findall(r"[。！？!?，,、；;.:]", text))
    score = (paragraph_chars + length * 0.2) * (1 - min(link_density(node), 0.95))
    score *= 1 + min(punctuation, 300) / 1500.0
    score *= 1 + min(depth_of(node), 25) / 80.0
    if node.tag in ("article", "main"):
        score *= 1.25
    return score


def pick_root(root):
    body = None
    candidates = []
    for node in iter_nodes(root):
        if node.tag == "body":
            body = node
        if node.tag in ("article", "main", "div", "section", "td"):
            if len(node_text(node)) >= 400:
                candidates.append(node)
    if not candidates:
        return body or root

    scored = [(score_node(n), depth_of(n), n) for n in candidates]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return body or root

    best_score = max(item[0] for item in scored)
    near = [item for item in scored if item[0] >= best_score * 0.8]
    near.sort(key=lambda item: (item[1], item[0]))
    return near[-1][2]


def clean_tree(node):
    for child in list(node.children):
        if isinstance(child, str):
            continue
        tag = child.tag
        name = " ".join(filter(None, [child.attrs.get("id", ""), child.attrs.get("class", "")]))
        if tag in DROP_TAGS:
            node.children.remove(child)
            continue
        if tag in ("nav", "aside", "footer", "header"):
            node.children.remove(child)
            continue
        if name and NOISE_RE.search(name):
            if len(node_text(child)) < 400 or link_density(child) > 0.5:
                node.children.remove(child)
                continue
        clean_tree(child)
    return node


# ---------------------------------------------------------------- Markdown


def abs_url(raw, base):
    raw = (raw or "").strip()
    if not raw or re.match(r"^(javascript|data|about|blob):", raw, re.IGNORECASE):
        return ""
    if re.match(r"^https?://", raw, re.IGNORECASE):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/") and base:
        match = re.match(r"^(https?://[^/]+)", base)
        return (match.group(1) + raw) if match else raw
    if base:
        return base.rsplit("/", 1)[0] + "/" + raw
    return raw


def render_inline(node, base):
    out = []
    for child in node.children:
        if isinstance(child, str):
            text = re.sub(r"\s+", " ", child)
            if text.strip():
                out.append(text)
            elif out:
                out.append(" ")
            continue
        tag = child.tag
        if tag in DROP_TAGS:
            continue
        if tag == "img":
            src = abs_url(child.attrs.get("src") or child.attrs.get("data-src"), base)
            if src:
                alt = (child.attrs.get("alt") or "").strip()
                out.append("![%s](%s)" % (alt, src))
            continue
        if tag == "br":
            out.append("\n")
            continue

        inner = render_inline(child, base).strip()
        if tag in ("strong", "b"):
            out.append("**%s**" % inner if inner else "")
        elif tag in ("em", "i"):
            out.append("*%s*" % inner if inner else "")
        elif tag in ("del", "s"):
            out.append("~~%s~~" % inner if inner else "")
        elif tag in ("code", "kbd"):
            out.append("`%s`" % inner if inner else "")
        elif tag == "a":
            href = abs_url(child.attrs.get("href"), base)
            out.append("[%s](%s)" % (inner, href) if (inner and href) else inner)
        else:
            out.append(render_inline(child, base))
    return "".join(out)


def render_list(node, base, depth=0):
    ordered = node.tag == "ol"
    try:
        index = int(node.attrs.get("start", "1"))
    except ValueError:
        index = 1

    lines = []
    for item in node.children:
        if isinstance(item, str) or item.tag != "li":
            continue
        sublists = [c for c in item.children if isinstance(c, Node) and c.tag in ("ul", "ol")]
        for sub in sublists:
            item.children.remove(sub)
        body = render_blocks(item, base).strip()
        indent = "  " * depth
        marker = "%d. " % index if ordered else "- "
        index += 1
        body_lines = [re.sub(r"\s+", " ", line).strip() for line in body.split("\n")]
        first = body_lines.pop(0) if body_lines else ""
        lines.append(indent + marker + first)
        for line in body_lines:
            if line:
                lines.append(indent + "  " + line)
        for sub in sublists:
            lines.append(render_list(sub, base, depth + 1))
    return "\n".join(lines)


def render_table(node, base):
    rows = []
    for tr in iter_nodes(node):
        if tr.tag != "tr":
            continue
        cells = [
            re.sub(r"\s+", " ", render_inline(td, base)).strip().replace("|", "\\|")
            for td in tr.children
            if isinstance(td, Node) and td.tag in ("td", "th")
        ]
        if cells:
            rows.append(cells)
        if len(rows) >= 60:
            break
    if len(rows) < 2:
        return ""
    width = max(len(row) for row in rows)
    if width > 8:
        return ""

    def pad(row):
        row = row + [""] * (width - len(row))
        return "| " + " | ".join(row) + " |"

    out = [pad(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    out.extend(pad(row) for row in rows[1:])
    return "\n".join(out)


def render_blocks(node, base):
    parts = []
    buffer = []

    def flush():
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(buffer))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            parts.append(text)
        buffer.clear()

    for child in node.children:
        if isinstance(child, str):
            text = re.sub(r"\s+", " ", child)
            if text.strip():
                buffer.append(text)
            continue
        tag = child.tag
        if tag in DROP_TAGS:
            continue
        if tag in INLINE_TAGS or tag in ("img", "br"):
            buffer.append(render_inline(child, base))
            continue

        flush()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            heading = re.sub(r"\s+", " ", render_inline(child, base)).strip()
            if heading:
                parts.append("#" * int(tag[1]) + " " + heading)
        elif tag == "p":
            paragraph = render_inline(child, base).strip()
            if paragraph:
                parts.append(paragraph)
        elif tag == "blockquote":
            quoted = render_blocks(child, base).strip()
            if quoted:
                parts.append("\n".join("> " + line for line in quoted.split("\n")))
        elif tag == "pre":
            code = child.children
            language = ""
            for sub in iter_nodes(child):
                if sub.tag == "code":
                    match = re.search(r"language-([\w+#.-]+)", sub.attrs.get("class", ""))
                    if match:
                        language = match.group(1)
                    break
            text = "".join(c for c in code if isinstance(c, str)).rstrip()
            if text.strip():
                parts.append("```%s\n%s\n```" % (language, text))
        elif tag in ("ul", "ol"):
            rendered = render_list(child, base)
            if rendered:
                parts.append(rendered)
        elif tag == "table":
            # 老式布局（如 Hacker News）整页都是表格且几乎全是链接，
            # 渲染成表格反而更乱，这种情况退回纯文本。
            if link_density(child) > 0.5:
                plain = node_text(child)
                if plain:
                    parts.append(plain)
            else:
                rendered = render_table(child, base)
                if rendered:
                    parts.append(rendered)
                else:
                    plain = node_text(child)
                    if plain:
                        parts.append(plain)
        elif tag == "hr":
            parts.append("---")
        elif tag == "figcaption":
            caption = re.sub(r"\s+", " ", render_inline(child, base)).strip()
            if caption:
                parts.append("*%s*" % caption)
        else:
            inner = render_blocks(child, base).strip()
            if inner:
                parts.append(inner)

    flush()
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 抓取


def pick_charset(raw, content_type):
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if not match:
        match = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.IGNORECASE)
    name = (match.group(1).decode("ascii", "ignore") if isinstance(match.group(1), bytes)
            else match.group(1)) if match else "utf-8"
    name = name.lower()
    if name in ("gb2312", "gbk", "gb18030"):
        return "gb18030"
    if name in ("big5",):
        return "big5"
    return name


def fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        content_type = response.headers.get("Content-Type") or ""
        charset = pick_charset(raw, content_type)
        try:
            text = raw.decode(charset, "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
        return {
            "final_url": response.geturl(),
            "status": getattr(response, "status", 200),
            "content_type": content_type,
            "charset": charset,
            "bytes": len(raw),
            "text": text,
        }


def meta(html, *patterns):
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            value = re.sub(r"\s+", " ", unescape(match.group(1))).strip()
            if value:
                return value
    return ""


def extract_article(html, final_url, max_chars):
    parser = TreeBuilder()
    parser.feed(html)
    parser.close()

    root = pick_root(parser.root)
    root = clean_tree(root)

    markdown = re.sub(r"\n{3,}", "\n\n", render_blocks(root, final_url)).strip()
    text = node_text(root)
    if len(text) < 200:  # 抽取失败时退回整页纯文本
        text = re.sub(r"\n{3,}", "\n\n", node_text(parser.root)).strip()
        markdown = markdown or text
    # 整页被识别成表格（典型的老式站点）时，表格 Markdown 反而难读，退回纯文本。
    if markdown.count("| ---") >= 2 and len(text) < 2000:
        markdown = text

    return {
        "title": meta(
            html,
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
            r"<title[^>]*>(.*?)</title>",
        ),
        "site_name": meta(
            html,
            r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
        ),
        "description": meta(
            html,
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
        ),
        "author": meta(
            html,
            r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+property=["\']article:author["\'][^>]+content=["\']([^"\']+)',
        ),
        "published": meta(
            html,
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+name=["\'](?:publishdate|date)["\'][^>]+content=["\']([^"\']+)',
        ),
        "canonical": abs_url(meta(html, r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)'), final_url),
        "markdown": markdown[:max_chars],
        "text": text[:max_chars],
        "char_count": len(re.sub(r"\s", "", text)),
    }


def read_url(url, timeout, max_chars):
    payload = fetch(url, timeout)
    text = payload["text"]
    content_type = payload["content_type"].lower()

    record = {
        "url": url,
        "final_url": payload["final_url"],
        "status": payload["status"],
        "content_type": payload["content_type"],
        "charset": payload["charset"],
        "bytes": payload["bytes"],
        "kind": "html",
    }

    if "json" in content_type or text.lstrip()[:1] in ("{", "["):
        record["kind"] = "json"
        try:
            data = json.loads(text)
            record["json"] = data
            record["markdown"] = "```json\n" + json.dumps(
                data, ensure_ascii=False, indent=2
            )[:max_chars] + "\n```"
            record["text"] = record["markdown"]
            record["char_count"] = len(text)
        except ValueError:
            record["kind"] = "text"
            record["text"] = text[:max_chars]
            record["markdown"] = text[:max_chars]
            record["char_count"] = len(text)
        return record

    if "xml" in content_type or text.lstrip().startswith("<?xml"):
        record["kind"] = "xml"
        titles = re.findall(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.DOTALL)
        links = re.findall(r"<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", text, re.DOTALL)
        items = []
        for title, link in zip(titles, links):
            items.append({
                "title": re.sub(r"\s+", " ", unescape(title)).strip(),
                "url": link.strip(),
            })
        record["item_count"] = len(items)
        record["items"] = items[:80]
        lines = ["# RSS/XML：%d 条" % len(items), ""]
        for item in record["items"]:
            lines.append("- [%s](%s)" % (item["title"], item["url"]))
        record["markdown"] = "\n".join(lines)[:max_chars]
        record["text"] = record["markdown"]
        record["char_count"] = len(text)
        return record

    record.update(extract_article(text, payload["final_url"], max_chars))
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description="读取网页/接口内容")
    parser.add_argument("urls", nargs="+", help="一个或多个 URL")
    parser.add_argument("--out", default="", help="输出文件：.md 写正文，其它写 JSON")
    parser.add_argument("--json", action="store_true", help="标准输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出摘要，不打印正文")
    parser.add_argument("--max-chars", type=int, default=200000, help="正文截断长度")
    parser.add_argument("--timeout", type=float, default=30.0, help="超时秒数")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    records = []
    failed = 0
    for url in args.urls:
        try:
            record = read_url(url, args.timeout, args.max_chars)
            records.append(record)
            if not args.json:
                print("[OK] %s" % url)
                print("     %s | %s | %d 字符"
                      % (record.get("kind"), (record.get("title") or "")[:60], record.get("char_count", 0)))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
            failed += 1
            records.append({"url": url, "error": str(error)})
            print("[FAIL] %s -> %s" % (url, error), file=sys.stderr)

    if args.out:
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if path.lower().endswith((".md", ".txt")) and len(records) == 1 and "markdown" in records[0]:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(records[0]["markdown"])
        else:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(records, handle, ensure_ascii=False, indent=2)
        print("[写入] %s" % path)
    elif args.json:
        json.dump(records, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif not args.quiet:
        for record in records:
            if "markdown" in record:
                print("\n" + "=" * 60)
                print(record.get("title") or record["url"])
                print("=" * 60)
                print(record["markdown"])

    return 1 if failed and failed == len(records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
