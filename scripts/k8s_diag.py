#!/usr/bin/env python3
"""K8s 连通性诊断脚本：在 AgentScope 主服务容器内运行，零额外依赖。

依次测试：
  1. kubeconfig 环境变量与 apiserver 地址
  2. TCP 连接（5s 超时）
  3. TLS 握手 + /readyz HTTP 请求（10s 超时）
  4. 用 kubernetes_asyncio 发真实 list_namespaced_pod 请求
     （与主服务完全相同的代码路径，15s request timeout + 20s 兜底）

用法：
    docker cp k8s_diag.py <服务容器名>:/tmp/k8s_diag.py
    docker exec <服务容器名> python /tmp/k8s_diag.py
"""
import asyncio
import os
import re
import socket
import ssl
import time
import urllib.parse


def parse_server_from_kubeconfig(path: str) -> str:
    """从 kubeconfig 中解析 server 地址。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"server:\s*(https?://\S+)", text)
    return m.group(1) if m else ""


def test_socket(host: str, port: int) -> str:
    """TCP 连接 + TLS 握手 + /readyz HTTP 请求。"""
    s = socket.socket()
    s.settimeout(5)
    t0 = time.monotonic()
    try:
        s.connect((host, port))
        tcp_ok = f"TCP connect OK ({time.monotonic() - t0:.2f}s)"
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ss = ctx.wrap_socket(s, server_hostname=host)
            tls_ok = f"TLS handshake OK ({time.monotonic() - t0:.2f}s)"
            ss.sendall(
                b"GET /readyz HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n\r\n",
            )
            ss.settimeout(10)
            data = ss.recv(4096)
            return (
                f"{tcp_ok}; {tls_ok}; HTTP 响应: {data[:80]!r}"
            )
        except Exception as e:
            return (
                f"{tcp_ok}; TLS/HTTP FAILED "
                f"({time.monotonic() - t0:.2f}s): "
                f"{type(e).__name__}: {e}"
            )
    except Exception as e:
        return (
            f"TCP FAILED ({time.monotonic() - t0:.2f}s): "
            f"{type(e).__name__}: {e}"
        )
    finally:
        try:
            s.close()
        except Exception:
            pass


async def test_api(kubeconfig: str) -> str:
    """用 kubernetes_asyncio 发真实 list 请求（与主服务同一代码路径）。"""
    from kubernetes_asyncio import client
    from kubernetes_asyncio import config

    await config.load_kube_config(config_file=kubeconfig)
    api = client.CoreV1Api()
    namespace = os.environ.get("ADP_K8S_NAMESPACE", "agentscope")
    try:
        pods = await asyncio.wait_for(
            api.list_namespaced_pod(
                namespace,
                # 不存在的 label，避免拉全量、不影响生产
                label_selector="app=k8s-diag-nonexistent-probe",
                _request_timeout=15,
            ),
            timeout=20,
        )
        return f"OK: list_namespaced_pod 返回 {len(pods.items)} 个 Pod"
    except asyncio.TimeoutError:
        return "TIMEOUT: 请求 20s 无响应（apiserver 假死或网络挂起）"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def main() -> None:
    kubeconfig = os.environ.get("ADP_K8S_KUBECONFIG", "") or os.environ.get(
        "KUBECONFIG",
        "",
    )
    # 兜底：Rainbond 部署的默认挂载路径
    if not kubeconfig and os.path.exists("/app/kubeconfig/config"):
        kubeconfig = "/app/kubeconfig/config"
    print(f"[1] ADP_K8S_KUBECONFIG = {kubeconfig or '(未设置!)'}")
    if not kubeconfig:
        print("    未找到 kubeconfig，请确认环境变量")
        return

    server = parse_server_from_kubeconfig(kubeconfig)
    print(f"[2] apiserver 地址 = {server}")
    if not server:
        print("    无法从 kubeconfig 解析 server 地址")
        return

    parsed = urllib.parse.urlparse(server)
    host, port = parsed.hostname, parsed.port or 443
    print(f"[3] 目标 {host}:{port}")
    print(f"    结果: {test_socket(host, port)}")

    print("[4] 用 kubernetes_asyncio 发真实 API 请求（与主服务同路径）...")
    print(f"    结果: {asyncio.run(test_api(kubeconfig))}")


if __name__ == "__main__":
    main()
