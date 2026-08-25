# -*- coding: utf-8 -*-
"""通过 HTTP 接口向 runtime_configs 写入/更新多个运行时配置段。

调用后端 HTTP 接口 ``PUT {API_BASE}/config/{key}``（body 为配置 payload，
UPSERT：不存在则增、存在则改）。无需直接连数据库，也无需 import bocomadp。

所有参数都是本文件顶部的代码变量：改配置只需编辑下方变量。

用法::

    # 编辑 API_BASE / CONFIGS 后直接运行
    python set_runtime_config.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 配置区：按需修改
# ---------------------------------------------------------------------------

#: 后端接口基础地址（不带末尾斜杠）。完整写接口为 PUT {API_BASE}/config/{key}
API_BASE = "http://localhost:9000/api"

#: 调用方用户 ID，作为 X-User-ID 请求头（接口 get_current_user_id 必需）。
USER_ID = "lwh"

#: 请求超时（秒）。
TIMEOUT = 10

#: 要写入的配置段列表。每个元素为 {"key": <段key>, "payload": <dict>}。
#: 一次运行会依次写入所有段；重复执行按 key 覆盖。
CONFIGS: list[dict] = [
    {
        "key": "df_session_config_ttl",
        "payload": {
            "ttl_seconds": 14400
        },
    },
    {
        "key": "summarization",
        "payload": {
            "enabled": True,
            "user_id": "lwh",
            "credential_id": "87405761bd544aa99bc4aba9da0e8a08",
            "model_name": "deepseek-v4-flash"
        },
    },
    # {
    #     "key": "personal_search",
    #     "payload": {"top_k": 5},
    # },
]

# ---------------------------------------------------------------------------
# 以下无需改动
# ---------------------------------------------------------------------------


def _put_config(key: str, payload: dict) -> None:
    url = f"{API_BASE.rstrip('/')}/config/{key}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "X-User-ID": USER_ID,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    print(f"[set] PUT {url} -> {resp.status} {body}")


def main() -> int:
    if not CONFIGS:
        print("[warn] CONFIGS 为空，未写入任何配置", file=sys.stderr)
        return 0

    failed = False
    for item in CONFIGS:
        key = item.get("key")
        payload = item.get("payload")
        if not key or not isinstance(payload, dict):
            print(f"[skip] 无效配置项: {item!r}", file=sys.stderr)
            failed = True
            continue
        try:
            _put_config(key, payload)
        except urllib.error.HTTPError as exc:
            print(f"[error] PUT {key} HTTP {exc.code}: {exc.read().decode('utf-8','replace')}",
                  file=sys.stderr)
            failed = True
        except urllib.error.URLError as exc:
            print(f"[error] PUT {key} 网络错误: {exc.reason}", file=sys.stderr)
            failed = True
        except Exception as exc:  # noqa: BLE001
            print(f"[error] PUT {key} 失败: {exc}", file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
