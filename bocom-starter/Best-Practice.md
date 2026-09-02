# 最佳实践 — 业务开发者接入指南

面向把本服务作为 **agent 能力**接入自身业务的开发者。**日常业务开发无需
修改任何服务端代码**：本框架把「模型平台接入」抽象为 4 类 REST 资源，通过
API 组合即可完成接入、上线与运营。

> 环境准备、安装与 API 详细示例见 [Readme.md](Readme.md)「一、本地开发」；
> 打包 / 部署见其「二、CI/CD 交付」。本文档先给两种启动方式，再给接入
> 路径与框架开发模式。

## 1. 快速启动（体验 / 联调环境）

先跑起一个服务实例，再按第 2 节接入。两种方式二选一：

### 1.1 方式一：本地开发启动（源码直跑）

适合改代码调试、快速验证接口：

```bash
# 前置：Python ≥ 3.11 + uv；本机 Redis（localhost:6379，可起 redis 容器映射端口）
export PIP_INDEX_URL=https://<行内pip源>/simple

cd bocom-starter
uv pip install wheels/bocom_as-0.1.0-py3-none-any.whl   # 自动带 agentscope SDK
uvicorn main:app --reload        # 或 python main.py
```

验证：浏览器打开 `http://localhost:8000/docs`。

### 1.2 方式二：Docker 开发启动（镜像容器）

适合体验完整部署形态、协议联调（与生产同一镜像同一命令）：

```bash
# 前置：本机 Docker；Redis 可达（本机 redis 容器或行内测试 Redis）
cd bocom-starter

# 构建需行内 pip 源；容器内访问宿主机 Redis 用 host.docker.internal
# （若 Redis 在 agentscope 同一容器网络内，可改用其容器名）
PIP_INDEX_URL=https://<行内pip源>/simple \
REDIS_HOST=host.docker.internal \
ELLM_REDIS_HOST=host.docker.internal \
docker compose up -d --build
```

验证：`http://localhost:8000/docs`；日志 / 停止：
`docker compose logs -f agentscope`、`docker compose down`。

> Linux 原生 Docker 无 `host.docker.internal` 时改用宿主机 IP；
> 有行内测试 Redis 时直接 `REDIS_HOST=<测试地址>`；
> `.env` 保持 localhost 默认即可（environment 注入优先于 .env）。

## 2. 整体认知：4 类资源、一条链路

一次业务对话由 4 步 API 调用完成，每类资源按 `X-User-ID` 隔离：

```text
① 凭证（钥匙）  POST /credential/       行内模型访问身份（api_key、模型、scene）
② agent（人设） POST /agent/            角色定义，可被多个会话复用
③ 会话（上下文）POST /sessions/         对话历史 + 绑定「凭证 × 模型」
④ 对话（触发）  POST /chat/            回复经 GET /sessions/{id}/stream 事件流下发
```

要点：

- **凭证与 agent 是低频对象**：建一次、长期复用，跨会话共享；**会话是高频
  对象**：每个「用户 × 对话」建一个，同会话连续对话自动携带上下文；
- **模型绑定在会话上**（`chat_model_config`），模型名必须存在于 `/ellm-models`
  候选，换模型只影响该会话；
- 全程只依赖 REST API + SSE 事件流，调用方可以是任意能发 HTTP 的后端。

## 3. 推荐接入路径（30 分钟跑通）

> 「§3.x」均指 [Readme.md](Readme.md)「3. 快速上手」的对应小节（含完整
> curl 示例）。

| 步骤 | 做什么 | 要点 | 详见 |
|---|---|---|---|
| 1 | 确认服务与模型 | 浏览器打开 `/docs`；`GET /ellm-models` 查可用候选 | §3.3 |
| 2 | 创建凭证 | `api_key` + `base_url`（**以 `/v1` 结尾**）+ `model`；建议同时填 `scene_code` / `api_key_url` 启用 key 自动刷新 | §3.1 |
| 3 | 创建 agent | 按业务角色建模，一次创建、多处复用 | §3.2 |
| 4 | 创建会话并绑模型 | 每个「用户 × 对话」建一个；也可先建后 `PATCH` 补模型 | §3.3 |
| 5 | 对话 | **先订阅事件流，再 `POST /chat/`**；以 `REPLY_END` 事件作为一轮结束 | §3.4 |

验证通过后，把以上步骤固化到业务代码（凭证 / agent 建一次缓存复用，会话
按需创建），即完成接入。

## 4. 框架的开发模式

### 4.1 配置驱动为主：90% 场景不写代码

凭证、模型候选、会话 think-tag 均为运行时资源，通过 API 增改即时生效，
不需要服务端发版：

| 业务诉求 | 做法 |
|---|---|
| 换模型 | `PATCH /sessions/{id}` 改 `chat_model_config`，或更新凭证的 `model` |
| 开/关思考过程 | `PUT /ellm-models/session/{id}/think-tag`（会话级覆盖，见 §3.5） |
| 模型上/下架 | `POST`/`PUT`/`DELETE` 管理 `/ellm-models` 候选（见 §3.3） |
| key 过期 | 无需处理：凭证配了 `scene_code` + `api_key_url` 即自动刷新（§3.6） |

平台能力（key 自动刷新、think-tag 注入）对业务透明：开启 think 后事件流
多出 `THINKING_BLOCK_*` 段，业务按需渲染或忽略即可。

### 4.2 会话即上下文：多轮、迁移与恢复

- **连续多轮**：同一 session 重复 `POST /chat/`，历史自动延续；
- **历史回灌**：`input` 传消息列表（业务迁移存量对话、开场铺垫）；
- **中断恢复**：收到 `REQUIRE_USER_CONFIRM`（人工确认）等暂停事件时，用
  对应恢复事件续跑——仅在出现暂停事件时需要；
- **话题隔离**：新对话开新 session，上下文互不串扰。

### 4.3 扩展模式：能力边界外才改代码

框架按「SDK 核心 → bocom-as（行内平台扩展）→ bocom-starter（装配层）」
分层。需要服务端新增能力时才改动代码，并按 Readme「二、CI/CD 交付」
打包发布：

- 新模型供应商 / 凭证形态 → 扩展 bocom-as providers；
- 新 agent 模板、路由、中间件 → 扩展 bocom-starter（main.py）装配层。

判断原则：**能用现有 API 组合解决的诉求一律不动代码**，保证与平台升级
兼容。

### 4.4 开箱能力（服务已装配，按需使用）

| 能力 | 业务效果 | 说明 |
|---|---|---|
| 长程记忆 | 同一 agent 跨会话记住用户事实 | 默认开启 |
| 知识库问答 | 文档入库后检索增强回答 | 业务侧建库并上传文档（接口见 `/docs`） |
| 工作区与文件 | agent 可读写文件、执行命令，产物随会话持久化 | 部署已挂载 docker.sock（compose 默认） |

## 5. 高频避坑清单

| 现象 / 误区 | 建议 |
|---|---|
| 缺 `X-User-ID` 报 422 | 业务侧统一注入身份头；服务不鉴权，用户隔离由业务网关负责 |
| 拿 `POST /chat/` 响应当回复 | chat 是异步调度，回复在事件流中——**先订阅再触发** |
| 不确定一轮何时结束 | 以 `REPLY_END` 事件为准；同会话并发触发会 409 冲突 |
| 报模型不存在 / 调用失败 | `base_url` 以 `/v1` 结尾；`model` 须与 `/ellm-models` 候选完全一致 |
| 凭证忘配刷新字段 | 不填 `scene_code` / `api_key_url` 则 key 过期后调用中断，需手动更新凭证 |
| think-tag 不生效 | 按优先级排查：会话级覆盖 > 模型表 > 凭证 `inject_think_tag` > 默认 false |
| Docker 起服务后连不上 Redis | 容器内 `localhost` 非宿主机：注入 `REDIS_HOST=host.docker.internal`（或同网络容器名）再 up |
