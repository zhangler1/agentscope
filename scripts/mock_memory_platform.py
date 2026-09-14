# -*- coding: utf-8 -*-
"""Mock 记忆平台服务 — 打印请求输入,返回固定响应。

用于本地联调记忆中间件,验证请求组装与响应解析逻辑。
响应结构与 docs/记忆.txt 样例一致。

用法: python3 mock_memory_platform.py [--port 9200]
"""
import argparse
import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


def _envelope(result) -> dict:
    """样例响应信封。"""
    return {
        "RSP_BODY": {
            "TRAN_PROCESS": "",
            "result": result,
            "param": None,
            "TRAN_ID": "",
        },
        "RSP_HEAD": {
            "TRAN_SUCCESS": "1",
            "TRACE_NO": "mock-trace",
            "TRACE_ID": "mock-trace-id",
            "PROCESS_STATUS_CODE": "N",
            "BIZ_TRACE_NO": None,
        },
    }


# saveMemoriesStandard.do 累计调用次数（= 当前记忆批次序号）。
# HTTPServer.serve_forever 单线程串行处理请求，模块级 int 无并发竞争。
_save_seq = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 — HTTP 方法名按协议
        global _save_seq
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")

        # 解析并打印请求输入(REQ_MESSAGE 表单字段)
        try:
            fields = parse_qs(raw)
            payload = fields.get("REQ_MESSAGE", [raw])[0]
            param = json.loads(payload).get("REQ_BODY", {}).get("param", {})
        except Exception:
            param = {}
        print(f"\n===== {self.path} =====")
        print(json.dumps(param, ensure_ascii=False, indent=2))
        print("========================\n")

        # 按接口返回响应；save 每调用一次计数 +1，search 返回内容带当前序号
        # （HTTPServer 单线程串行处理请求，模块级计数器无并发竞争）
        if self.path.endswith("/registerAgent.do"):
            agent_id = param.get("agentId", "mock-agent")
            result = {
                "caller": f"{agent_id}_0_{random.randint(10**14, 10**15 - 1)}",
                "agentId": agent_id,
                "agentName": param.get("agentName", ""),
                "agentPlat": param.get("agentPlat", 0),
            }
        elif self.path.endswith("/saveMemoriesStandard.do"):
            _save_seq += 1
            print(f"[mock] saveMemories called -> memory seq now {_save_seq}")
            result = "success"
        elif self.path.endswith("/searchMemory.do"):
            result = [{
                "id": f"mock-memory-id-{_save_seq}",
                "memory": f"这是 mock 返回的第 {_save_seq} 批记忆:"
                          "用户偏好中文回复,负责支付模块的日常答疑。",
                "score": 0.85,
                "createdAt": "2026-09-03T10:00:00+08:00",
                "userId": param.get("userCode", ""),
                "agentId": param.get("agentId", ""),
                "updatedAt": "2026-09-03T10:00:00+08:00",
            }]
        else:
            return self._reply(404, {"detail": f"unknown path {self.path}"})
        self._reply(200, _envelope(result))

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock 记忆平台服务(固定响应)")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"mock memory platform listening on http://{args.host}:{args.port}")
    print("  POST /registerAgent.do  POST /saveMemoriesStandard.do  "
          "POST /searchMemory.do")
    server.serve_forever()


if __name__ == "__main__":
    main()
