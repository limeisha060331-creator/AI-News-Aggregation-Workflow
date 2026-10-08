"""网页取用器的本地接收端：把 Edge 插件推来的网页追加成 JSONL。

用法（在仓库根目录）：
    python tools/page_ingest_server.py                 # 监听 http://127.0.0.1:8787
    python tools/page_ingest_server.py --port 9000
    python tools/page_ingest_server.py --out some/other.jsonl

接口：
    GET  /health   返回 {"ok": true, "count": <已收条数>}
    POST /ingest   接收插件推来的 JSON，追加一行到数据文件

数据默认落在 scheduler/state/inbox.jsonl，每行一条 JSON，额外带 received_at 字段。
只依赖标准库，不联网。
"""

import argparse
import datetime
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 8 * 1024 * 1024

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scheduler", "state", "inbox.jsonl",
)


class IngestHandler(BaseHTTPRequestHandler):
    server_version = "PageIngest/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        sys.stdout.write("[%s] %s\n" % (stamp, fmt % args))
        sys.stdout.flush()

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/"):
            self._send_json(200, {"ok": True, "count": self.server.record_count})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = self.headers.get("Content-Length")
        try:
            size = int(length or 0)
        except ValueError:
            size = 0
        if size <= 0:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        if size > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return

        raw = self.rfile.read(size)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(400, {"ok": False, "error": "invalid json: %s" % error})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "body must be a json object"})
            return

        record = dict(payload)
        record["received_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            with open(self.server.out_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            self._send_json(500, {"ok": False, "error": "write failed: %s" % error})
            return

        self.server.record_count += 1
        title = str(record.get("title") or "").strip() or "(无标题)"
        self.log_message("saved #%d  %s", self.server.record_count, title[:60])
        self._send_json(200, {
            "ok": True,
            "count": self.server.record_count,
            "saved_to": self.server.out_path,
        })


def parse_args(argv):
    parser = argparse.ArgumentParser(description="网页取用器本地接收端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    parser.add_argument("--out", default=DEFAULT_OUT, help="JSONL 输出路径")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    count = 0
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as handle:
            count = sum(1 for line in handle if line.strip())

    server = ThreadingHTTPServer((args.host, args.port), IngestHandler)
    server.out_path = out_path
    server.record_count = count

    print("网页取用器接收端已启动")
    print("  地址   http://%s:%d/ingest" % (args.host, args.port))
    print("  数据   %s" % out_path)
    print("  已有   %d 条" % count)
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
