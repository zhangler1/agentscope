# CICD 集成

> 目标：把智能体服务从本地 demo 推向生产——掌握部署形态、容器化、分布式扩容与密钥注入。

## 部署形态总览

```text
单进程（默认）                     分布式（生产推荐）
┌──────────────────┐              ┌─────────┐   ┌─────────┐
│ API + 内置 worker │              │ API × N │   │ Worker×M│
│ + 本地工作区      │              └────┬────┘   └────┬────┘
└──────────────────┘                   └──── 共享 ─────┘
本地开发、原型、轻流量            Redis（storage + bus）+ S3 + 云沙箱
```

| 维度     | 单进程部署                  | 分布式部署                     |
| -------- | --------------------------- | ------------------------------ |
| 进程拓扑 | API + 索引同进程            | API + N 个 worker              |
| 资源隔离 | 解析重时挤占请求线程        | API 不受解析负载影响           |
| 扩容方式 | 整体扩 API 副本             | API 与 worker 独立扩           |
| 适用场景 | 本地、原型、轻流量          | 生产、解析/嵌入是瓶颈          |

## 基础设施依赖

| 组件           | 用途                                   | 生产建议                     |
| -------------- | -------------------------------------- | ---------------------------- |
| Redis          | `RedisStorage`（持久化）+ `RedisMessageBus`（会话锁/回放/收件箱/唤醒） | 高可用实例，API 与 worker 共享 |
| 向量库（可选） | RAG 服务（如 `QdrantStore`）           | 独立部署，网络可达           |
| 对象存储（可选）| `blob_store`（上传文件）               | 分布式必须用 `S3BlobStore`   |
| Docker/K8s     | 工作区隔离执行环境                     | 见下文工作区选型             |

## 最小生产配置（Docker 工作区）

```python
import uvicorn

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import (
    DockerWorkspaceManager,
    IsolationPolicy,
)

storage = RedisStorage(host="redis.internal", port=6379)
message_bus = RedisMessageBus(host="redis.internal", port=6379)

# 每个工作区跑在独立 Docker 容器中（按 agent 隔离，空闲 ttl 秒后淘汰）
workspace_manager = DockerWorkspaceManager(
    basedir="/data/workspaces",                 # 宿主根目录（bind mount 到容器 /workspace）
    isolation=IsolationPolicy.PER_AGENT,
    base_image="python:3.11-slim",              # 按内容哈希缓存构建
    node_version="20",
    ttl=3600.0,
)

app = create_app(
    storage=storage,
    message_bus=message_bus,
    workspace_manager=workspace_manager,
)

uvicorn.run(app, host="0.0.0.0", port=8000)
```

## 工作区管理器选型

| Manager                        | 部署边界         | 说明                                       |
| ------------------------------ | ---------------- | ------------------------------------------ |
| `LocalWorkspaceManager`        | 单节点           | 宿主目录，零基础设施                       |
| `BubblewrapWorkspaceManager`   | 单节点           | bubblewrap 沙箱（与宿主共享网络命名空间）  |
| `DockerWorkspaceManager`       | 单节点           | 每工作区一个容器，bind mount 持久化        |
| `E2BWorkspaceManager`          | 跨节点           | 沙箱按 metadata 寻址，空闲自动挂起/恢复    |
| `DaytonaWorkspaceManager`      | 跨节点           | 按沙箱标签重挂接                           |
| `OpenSandboxWorkspaceManager`  | 跨节点           | 按 metadata 重挂接，任意副本可重连         |
| `K8sWorkspaceManager`          | 跨节点           | 每工作区一个 Pod + PVC                     |

选型口诀：**本地开发 `Local`，单机生产 `Bubblewrap`/`Docker`，横向扩容切 `E2B`/`Daytona`/`OpenSandbox`/`K8s`**。

> **注意**：`Local`/`Bubblewrap`/`Docker` 三个 manager 是单节点的——工作区状态在服务进程所在机器上，水平扩容后同一 `workspace_id` 的请求可能落到无缓存节点。多副本部署必须用可跨节点寻址的云后端。

### 隔离粒度

`isolation` 决定 workspace 在 `(user_id, agent_id, session_id)` 三元组上如何共享：

| 取值                | 共享规则                                    | 典型场景                                |
| ------------------- | ------------------------------------------- | --------------------------------------- |
| `PER_AGENT`（默认） | 同一 `(user_id, agent_id)` 共享一个工作区   | 每个智能体一份持久化工作目录，跨会话保留 |
| `PER_SESSION`       | 每会话独立工作区                            | 一次性沙箱评测、短生命周期自动化         |
| `PER_USER`          | 同一用户所有会话共享（不区分智能体）        | 同一用户多个智能体协作（少见，慎用）     |

## 启用 RAG 服务的分布式部署

```python
# API 端：关掉内置 worker
app = create_app(..., enable_index_worker=False)

# Worker 端（库方式）：与 API 共享同一组 storage / bus / blob / manager 配置
from agentscope.app.rag import run_worker

await run_worker(
    storage=storage,
    message_bus=message_bus,
    blob_store=blob_store,
    knowledge_base_manager=kb_manager,
    parsers=[TextParser(), PDFParser(), PPTParser(), ImageParser()],
    chunkers=[ApproxTokenChunker],
    worker_max_concurrency=4,   # 单 worker 并发处理文档上限
    consumer_max_batch=32,      # 单次信号最多拉取任务数
)
```

CLI 方式：写一个返回上述 kwargs 的 `bootstrap()` 工厂函数，然后：

```bash
AGENTSCOPE_WORKER_BOOTSTRAP=mydeploy.worker_bootstrap:bootstrap \
    python -m agentscope.app.rag.index_worker
```

> **关键约定**：API 与 worker 必须挂**同一份** storage / 消息总线 / blob store / 知识库 manager 配置——共享 collection、blob URI 与文档 lease，任何一项不一致都会导致索引失败或数据错位。

> **警告**：parser 默认跑在事件循环线程内。引入 PDF/Office 等 CPU 密集解析器时，**务必**给 worker 传 `parser_executor=ProcessPoolExecutor(...)`，否则会阻塞同进程其他 asyncio 任务（单进程部署下直接拖慢 API 响应）。

## 启动顺序与生命周期

Lifespan 启动进入顺序（关闭逆序退出）：`storage` → `message_bus` → `workspace_manager` → 其余 manager 与后台任务。

- K8s/Compose 编排时只需保证 **Redis 先于服务就绪**（配置连接池重试即可，无需复杂依赖编排）；
- 服务关闭时 `close_all()` 自动拆除全部缓存工作区——优雅停机（SIGTERM → 排空 → 退出）即可，无需手动清理容器。

## CI/CD 密钥注入

密钥走**环境变量**，由 CI/CD secret 管理系统注入（GitHub Secrets / GitLab CI Variables / K8s Secret）：

```yaml
# 示例：K8s Deployment 环境变量注入
env:
  - name: DASHSCOPE_API_KEY
    valueFrom:
      secretKeyRef:
        name: agentscope-secrets
        key: dashscope-api-key
```

> **警告**：服务**不自带用户鉴权**（默认信任 `X-User-ID` header）。上线前必须替换为 JWT/OAuth2——见 [对接微应用 · 自定义鉴权](对接微应用.md#自定义鉴权)。

## 上线检查清单

| 检查项                                       | 通过标准                                     |
| -------------------------------------------- | -------------------------------------------- |
| 存储与消息总线                               | 生产 Redis，API/worker 共享同一实例          |
| 工作区管理器与副本数匹配                     | 多副本时使用跨节点后端（E2B/K8s 等）         |
| `blob_store`                                 | 分布式部署使用 `S3BlobStore`                 |
| CPU 密集解析器                               | worker 已配置 `parser_executor`              |
| 密钥注入                                     | 环境变量来自 secret 管理，无硬编码           |
| 用户鉴权                                     | 已替换占位 `X-User-ID` 依赖                 |
| 观测                                         | TracingMiddleware + OTel 后端已接入（见 [监控](../运维规范/监控.md)） |
