# bocom-starter — 行内模型平台参考启动程序

基于 agentscope 官方示例的启动程序，**额外装配行内模型平台**（bocom-as
发行版：`config` / `providers`），覆盖 3 条主流程：**行内模型凭证、选择模型并
chat、api_key 自动刷新**。

本文档分两部分：**[一、本地开发](#一本地开发)**（源码直跑、Mock 网关、API
联调）与 **[二、CI/CD 交付](#二cicd-交付行内)**（上传代码、构建镜像、部署）。

## 目录结构

```
bocomm-agent/              # 交付物根（= Docker 构建上下文）
├── bocom-as/              # 自包含发行版：config / providers / src（agentscope SDK）
└── bocom-starter/         # 本启动程序
    ├── main.py            # 服务入口（create_app + 行内模型平台路由）
    ├── Dockerfile         # 生产自包含镜像（构建上下文 = 交付物根）
    ├── docker-compose.yml # CI/CD 部署（agentscope 单服务，数据库用生产实例）
    ├── .env               # 环境变量示例（默认值，全部可省略）
    └── Readme.md
```

---

# 一、本地开发

## 1. 环境准备

```bash
# 交付/验证环境：bocom-as 自包含发行版，SDK 源码随包分发（bocom-as/src/agentscope）
pip install -e bocom-as
```

**依赖数据库（主存储）**：本服务用数据库存会话/凭证/Agent 等业务数据
（`AsyncSQLAlchemyStorage`；**OceanBase 兼容 MySQL 协议**，本地/测试可用
MySQL 模拟，切真 OB 只改 `DB_URL` 地址，驱动/建表逻辑不变）；**行内模型
平台本身不再依赖 Redis**（模型候选来自随包内置的
`providers/_models/*.yaml` 模型卡，key 刷新走主存储凭证记录）。

```bash
# 应用主存储（会话、凭证、Agent 等）。默认值即下方 localhost:3306
# （驱动 aiomysql；首次启动自动建表，dev/单机够用）
export DB_URL=mysql+aiomysql://agentscope:agentscope@localhost:3306/agentscope
```

## 2. 启动服务

```bash
cd bocom-starter
uvicorn main:app --reload          # 开发模式（热重载）
# 或 python main.py（reload 由 UVICORN_RELOAD=true 控制，默认关闭）
```

## 3. 快速上手：行内凭证 + 行内模型 chat

示例默认服务在 `http://localhost:8000`。

> 所有业务接口都要求 `X-User-ID` 请求头（临时 header 身份，缺失返回 422
> `Field required`），示例统一用 `test-user`。

### 3.1 创建行内凭证

`POST /credential/`（请求体为 `{"data": {...}}`，`type` 等字段全部位于
`data` 内；`type="bocom_ellm_credential"` 由 `providers.credential` 导入
时自动注册进 `CredentialFactory`）：

```bash
curl -X POST http://localhost:8000/credential/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "data": {
      "type": "bocom_ellm_credential",
      "base_url": "http://ellm-gateway.example/v1",
      "scene_code": "P2024146",
      "api_key_url": "http://ellm.example/ELLM-OMSERVICE/createSceneApiKey.do"
    }
  }'
# → {"credential_id": "cred-xxx"}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 否 | 固定 `bocom_ellm_credential` |
| `base_url` | ✅ | OpenAI 兼容端点（**以 `/v1` 结尾**） |
| `scene_code` | ✅ | 场景编码（api_key 自动刷新用） |
| `api_key_url` | ✅ | 取 key 的网关地址（api_key 自动刷新用） |
| `api_key` | 否 | 省略时用默认占位值 `sk-xxx`；运行时由中间件注入真实 key |
| `organization` | 否 | 组织 ID（可空） |
| `apikey_expires_at` | 否 | 过期时间戳；不填视为已过期（每次调用触发刷新） |

> 凭证**不绑定模型**：运行时可用的候选模型来自随包内置
> `providers/_models/*.yaml` 模型卡，通过官方
> `GET /model/?provider=bocom_ellm_credential` 查询。模型表 / 会话级
> think-tag 等 Redis 管理端点已移除（`/ellm-models` 不再存在）。

查询/管理：`GET /credential/` 列表、`GET /credential/schemas` 确认注册、
`GET /model/credential?credential_id=...` 查询凭证实际配置、
`PATCH /model/credential/{id}` 部分更新（仅覆盖传入字段，api_key 刷新
也写回同一凭证记录）。

### 3.2 创建 agent（无预置，需手动建一次）

```bash
curl -X POST http://localhost:8000/agent/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{"name": "ellm-assistant"}'
# → {"agent_id": "agent-xxx"}
```

### 3.3 创建会话并绑定行内模型

模型候选来自随包内置的 `providers/_models/*.yaml`（SDK 形态下模型名也可
在代码中直接指定）：

```bash
# 查看该 provider 下当前候选（内置模型卡）
curl -H 'x-user-id: test-user' \
  "http://localhost:8000/model/?provider=bocom_ellm_credential"
```

```bash
curl -X POST http://localhost:8000/sessions/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "agent_id": "agent-xxx",
    "chat_model_config": {
      "type": "bocom_ellm_credential",
      "credential_id": "cred-xxx",
      "model": "deepseek-v4-flash",
      "parameters": {}
    }
  }'
# → {"session_id": "sess-xxx"}
```

会话也可先建后补模型：`PATCH /sessions/{session_id}` 更新
`chat_model_config`。

### 3.4 chat（SSE 流式）

`POST /chat/` 是 **fire-and-forget**：响应体只返回 `{"status": "started",
"session_id": ...}` 确认已调度，**回复内容在
`GET /sessions/{session_id}/stream` 的 SSE 事件流**。需要**两个终端**：
先订阅事件流，再触发 chat。

```bash
# 终端 A：订阅会话事件流（先回放缓冲事件再实时推送；连接保持到断开，
# 同会话后续 run 也走这条流；30s 心跳注释帧 :\n\n）
curl -N -H 'x-user-id: test-user' \
  "http://localhost:8000/sessions/sess-xxx/stream?agent_id=agent-xxx"
```

```bash
# 终端 B：触发 chat
curl -X POST http://localhost:8000/chat/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "agent_id": "agent-xxx",
    "session_id": "sess-xxx",
    "input": {
      "name": "user",
      "role": "user",
      "content": [{"type": "text", "text": "你好，介绍一下你自己"}]
    }
  }'
# → {"status": "started", "session_id": "sess-xxx"}（此响应非错误！）
```

**`input` 四种形态**：

| 形态 | 说明 |
|---|---|
| `Msg` | 单条用户消息：`name` + `role`（user/assistant/system）必填，`content` 为块列表 `[{"type": "text", "text": "..."}]` |
| `list[Msg]` | 多条消息一次下发（历史回灌） |
| `UserConfirmResultEvent` | 恢复 HITL 暂停的工具调用：`reply_id` + `confirm_results` |
| `ExternalExecutionResultEvent` | 恢复外部执行：`reply_id` + `execution_results` |
| `null` | 从当前状态继续 |

**终端 A 的事件流**（每帧 JSON，`type` 区分事件）：

```
data: {"type": "REPLY_START", "session_id": "sess-xxx", "reply_id": "...", "name": "agent-xxx", "role": "assistant"}
data: {"type": "MODEL_CALL_START", ...}
data: {"type": "TEXT_BLOCK_START", "block_id": "...", "text": ""}
data: {"type": "TEXT_BLOCK_DELTA", "block_id": "...", "delta": "你好"}   # 文本增量
...
data: {"type": "TEXT_BLOCK_END", ...}
data: {"type": "REPLY_END", "reply_id": "...", "finished_reason": "completed"}
```

**常见错误**：

| 现象 | 原因 |
|---|---|
| 422 `x-user-id` 缺失 | 所有业务接口要求 `X-User-ID` 头 |
| 422 `input` 校验失败 | `Msg` 缺 `role` 或 `content` 不是块列表（非字符串） |
| 409 冲突 | 同会话已有 run 在飞（等上一轮 `REPLY_END` 再触发） |
| 404 | session/agent 不存在或不属于该用户 |
| SSE 里 `MODEL_CALL_END` 带 error / run 中断 | 模型调用失败（连接/鉴权），看服务端日志 |

### 3.5 api_key 自动刷新（无需人工干预）

每次模型调用前中间件惰性检查：`apikey_expires_at` 过期（默认在 key
过期前 300s 提前刷新；`refresh_ahead_secs` 非必填，需要调整时传入
`build_ellm_refresh_middleware`）→ `MessageBus.acquire_lock` 防抖
→ 同步调 `fetch_ellm_key(api_key_url, scene_code)` 取新 key → 写回凭证
记录 → `set_api_key` 注入请求头；401 `invalid_api_key` 时强制刷新并重试
当前调用一次；刷新失败标记凭证过期，下次调用走惰性刷新恢复。
日志关键字：`injected refreshed ELLM key`。

> key 刷新依赖的是注入给中间件的 **主存储**（`storage`，现为数据库）与
> **消息总线**（`message_bus`，单进程 InMemory；多进程部署换
> RedisMessageBus 后防抖锁跨进程生效），与模型候选无关。

---

# 二、CI/CD 交付（行内）

## 1. 交付物与上传

交付物只包含 2 个包（`bocom-as` / `bocom-starter`），
上传整个 `bocomm-agent/` 目录（即构建上下文）：

```
bocomm-agent/              # 交付物根（= Docker 构建上下文）
├── bocom-as/              # 自包含发行版：config / providers / src（agentscope SDK）
└── bocom-starter/         # 启动程序：main.py + Dockerfile + docker-compose.yml
                           #          + .env + Readme.md
```

- **bocom-as 自包含**：[pyproject.toml](../bocom-as/pyproject.toml) 打包
  `src/agentscope`（SDK）+ `config` + `providers`，依赖全量并入主列表
  （SDK 全部依赖 + service / storage-redis / storage-mysql(SQL 存储) /
  workspace-docker）。

## 2. 构建镜像

```bash
# 在交付物根（bocomm-agent/）下执行；Dockerfile 位于 bocom-starter/
docker build -t agentscope-service:bocom -f bocom-starter/Dockerfile .
```

镜像特性：

- **自包含**：SDK / config / providers / main.py 全部打进镜像，运行时
  无需挂载源码；uvicorn 生产默认不 reload；
- **分层缓存**：先 `COPY pyproject.toml` + `uv sync` 装依赖（依赖列表随
  bocom-as 发行版维护），再 COPY 源码——只要 pyproject.toml 不变，
  依赖层命中 Docker 缓存，迭代构建快。

## 3. 部署启动

```bash
docker compose -f bocom-starter/docker-compose.yml up -d
```

- **单服务**：只部署 agentscope；**生产数据库（OceanBase，兼容 MySQL
  协议）复用行内实例，不自建**，连接 URL 由平台环境变量注入（见下节）；
- **端口**：宿主 `9000`（默认，可用 `AGENTSCOPE_HOST_PORT` 覆盖）→ 容器 `8000`；
- **持久化**：`workspace-data` 命名 volume 挂载到
  `/app/bocom-starter/workspaces`（agent 工作区 + 长期记忆 Markdown 文件，
  容器重建不丢）；
- **Docker 工作区沙箱**：已挂载 `/var/run/docker.sock`，agent 工作区可跑
  嵌套 Docker 沙箱；如行内禁止，删除 compose 中对应挂载行即可。

## 4. 平台需注入的环境变量

compose 透传宿主环境变量（environment 优先于 `.env` 默认值），行内平台
在启动命令前注入以下变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DB_URL` | ✅ | 生产数据库 URL（应用主存储：会话、凭证、Agent 等；`mysql+aiomysql://...`，OceanBase 兼容 MySQL 协议） |
| `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 按需 | 各模型供应商 key |

> 未注入的变量不进入容器，退化为 `bocom-starter/.env` 中的默认值
> （`localhost` 类默认值仅适合本地直跑，生产必须注入真实地址）。

## 5. 部署运维要点

- **多实例**：改端口变量 + `-p` project 名（容器/网络/volume 全隔离）：

  ```bash
  AGENTSCOPE_HOST_PORT=8010 docker compose -f bocom-starter/docker-compose.yml -p bocom2 up -d
  ```

  各实例共享生产数据库（会话/凭证数据隔离由业务侧控制）；
- **日志**：`docker compose -f bocom-starter/docker-compose.yml logs -f
  agentscope`；API 文档见 `http://<宿主>:9000/docs`。

---

## 环境变量汇总

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_URL` | `mysql+aiomysql://agentscope:agentscope@localhost:3306/agentscope` | 应用主存储（会话、凭证、Agent 等；AsyncSQLAlchemyStorage，OceanBase 兼容 MySQL 协议）；Docker 部署时由平台注入生产地址 |
| `UVICORN_RELOAD` | `false` | 是否开启 uvicorn 热重载（本地开发设 `true`） |

> `.env` 由宿主应用加载（本仓库提供示例）。应用主存储为数据库（见上表），
> Redis 不再承担任何业务存储；行内模型平台（bocom-as `providers`）不依赖
> 额外配置：模型候选来自随包内置模型卡，api_key 刷新窗口默认 300s
> （`build_ellm_refresh_middleware` 的 `refresh_ahead_secs`，非必填）。
