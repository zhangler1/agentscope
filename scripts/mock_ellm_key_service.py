# -*- coding: utf-8 -*-
"""Mock ELLM key-service — 返回固定 API key。

模拟 BOCOM ELLM key-service 的 createSceneApiKey.do 接口：
  POST /createSceneApiKey.do
    data: REQ_MESSAGE=<json>
  响应:
    {"RSP_HEAD": {"TRAN_SUCCESS": "1"},
     "RSP_BODY": {"result": {"apiKey": "<固定key>", "timeToLive": 1500000}}}

用法: python3 mock_ellm_key_service.py [--port 9100] [--key sk-xxx]
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_KEY = "sk-ada383a3be484460a7f2f8bbdf781c61"
DEFAULT_TTL_MS = 1500000  # 25 分钟


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    api_key = DEFAULT_KEY

    def do_POST(self):  # noqa: N802 — HTTP 方法名按协议
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        self.log_message("POST %s body=%s", self.path, body[:200])

        resp = {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {
                "result": {
                    "apiKey": self.api_key,
                    "timeToLive": DEFAULT_TTL_MS,
                }
            },
        }
        payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[mock-key] {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock ELLM key-service")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--key", default=DEFAULT_KEY)
    args = parser.parse_args()
    Handler.api_key = args.key
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[mock-key] listening on :{args.port} (key={args.key})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
