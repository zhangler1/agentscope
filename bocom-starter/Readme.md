# bocom-agent — 行内模型平台服务

基于行内 agentscope SDK 的参考服务：`bocom-as` 为行内模型平台扩展
（**凭证、选择模型、add_think（think-tag）、api_key 刷新**），`bocom-starter`
为启动程序与交付物。

本文档覆盖：**[一、本地开发](#一本地开发)**（安装、启动与 API 快速上手）、
**[二、CI/CD 交付](#二cicd-交付行内)**（打包、构建与部署）。

> 业务接入指南（本地 / Docker 快速启动、接入路径与框架开发模式）见独立
> 文档 **[Best-Practice.md](Best-Practice.md)**。

## 仓库结构

```
bocom-agent/
├── bocom-as/          # 行内模型平台扩展源码（config + providers）→ 打包为 whl
└── bocom-starter/     # ★ 交付物（CI/CD 上传本目录）
    ├── main.py            # 服务入口（create_app + 行内模型平台路由）
    ├── Dockerfile         # 服务镜像：装 agentscope + wheels/bocom_as whl
    ├── docker-compose.yml # 部署编排（agentscope 单服务，Redis 用生产实例）
    ├── .env               # 环境变量默认值（示例，全部可省略）
    ├── wheels/            # bocom_as-0.1.0-py3-none-any.whl（本地包）
    └── Readme.md
```

- **agentscope SDK** 从行内 pip 源安装（`==2.0.7.post1`），不随交付物分发源码；
- **bocom_as** 为本地 whl（含 `config` + `providers` 及模型候选 yaml）；
- **生产 Redis** 复用行内实例，不自建（地址由部署平台注入）。

---

# 一、本地开发

## 1. 环境准备

依赖：Python ≥ 3.11、uv、Redis（本地 `redis-server` 或行内测试实例）。

```bash
# 行内 pip 源（若已配置到 uv 默认源可省略）
export PIP_INDEX_URL=https://<行内pip源>/simple

cd bocom-agent/bocom-starter

# 安装行内模型平台扩展（自动解析依赖，含 agentscope SDK）
uv pip install wheels/bocom_as-0.1.0-py3-none-any.whl
```

> 需同时修改 `bocom-as/` 源码联调时，改用源码可编辑安装
> （`cd bocom-as && uv pip install -e . --no-deps`，此时源码包配置与
> pyproject 为准，代理官方 pip 源行为一致）。

Redis 地址用环境变量指定（默认 `localhost:6379`）：

```bash
export REDIS_HOST=localhost        # 应用主存储（会话、凭证等）
export REDIS_PORT=6379
export ELLM_REDIS_HOST=localhost   # 行内模型平台 Redis（默认与主存储同实例）
export ELLM_REDIS_PORT=6379
```

## 2. 启动服务

两种启动方式：**本地开发启动**（源码直跑 + 热重载，改代码调试用）与
**Docker 开发启动**（镜像容器运行，贴近生产形态，体验 / 联调用）。

### 2.1 本地开发启动

```bash
cd bocom-starter
uvicorn main:app --reload          # 开发模式（热重载）
# 或 python main.py（reload 由 UVICORN_RELOAD=true 控制，默认关闭）
```

### 2.2 Docker 开发启动

本地构建并运行服务容器（镜像自包含，装 agentscope + wheels whl；Redis
复用本机容器 / 行内测试实例，与「二、CI/CD」生产部署同一镜像同一命令）：

```bash
cd bocom-starter
# 构建需行内 pip 源拉取 agentscope；容器内 localhost 非宿主机，
# Redis 须指向宿主机或测试实例（environment 注入优先于 .env 默认值）
PIP_INDEX_URL=https://<行内pip源>/simple \
REDIS_HOST=host.docker.internal \
ELLM_REDIS_HOST=host.docker.internal \
docker compose up -d --build
```

- **Redis 可达性**：Docker Desktop / WSL2 用 `host.docker.internal` 指向
  宿主机；Linux 原生 Docker 用宿主机 IP 或 `--add-host`；连行内测试 Redis
  时直接填测试地址；`.env` 保持 `localhost` 默认即可（仅本地直跑可用）；
- **验证 / 日志 / 停止**：`http://localhost:8000/docs`；
  `docker compose logs -f agentscope`、`docker compose down`；
- 镜像自包含：改代码后需重新构建（`--build`）；纯调代码建议回到 2.1
  本地直跑（热重载即时生效）。

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
      "api_key": "sk-xxx",
      "base_url": "http://ellm-gateway.example/v1",
      "model": "deepseek-v4-flash",
      "scene_code": "P2024146",
      "api_key_url": "http://ellm.example/ELLM-OMSERVICE/createSceneApiKey.do",
      "inject_think_tag": true
    }
  }'
# → {"credential_id": "cred-xxx"}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `api_key` | ✅ | `SecretStr`，传普通字符串即可 |
| `base_url` | ✅ | OpenAI 兼容端点（**以 `/v1` 结尾**） |
| `model` | ✅ | 行内模型名（须在 `GET /ellm-models` 候选中） |
| `scene_code` / `api_key_url` | 否 | api_key 自动刷新用（不填不影响 chat） |
| `inject_think_tag` | 否 | 是否注入 `<think>`（默认 false） |
| `apikey_expires_at` | 否 | 过期时间戳；不填视为已过期（每次调用触发刷新） |

查询/管理：`GET /credential/` 列表、`GET /credential/schemas` 确认注册、
`GET /model/credential?credential_id=...` 按凭证查候选模型、
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

模型候选来自 Redis 模型表（`bocomadp:model:think_tag`，field=模型名、
value=JSON `{think_tag, context_size, output_size}`；Redis 不可用时降级
`providers/_models/*.yaml`），由 `/ellm-models` 管理：

```bash
# 查看候选；POST/PUT/DELETE 同路径管理（field=模型名）
curl -H 'x-user-id: test-user' http://localhost:8000/ellm-models
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

- think-tag 开启时 `TEXT_BLOCK_*` 前有 `THINKING_BLOCK_*` 三连（首文本段
  即 `<think>` 内容）；工具调用见 `TOOL_CALL_*` … `TOOL_RESULT_END`；
  HITL 暂停见 `REQUIRE_USER_CONFIRM`（携带 `reply_id`，用
  `UserConfirmResultEvent` 恢复）。

**常见错误**：

| 现象 | 原因 |
|---|---|
| 422 `x-user-id` 缺失 | 所有业务接口要求 `X-User-ID` 头 |
| 422 `input` 校验失败 | `Msg` 缺 `role` 或 `content` 不是块列表（非字符串） |
| 409 冲突 | 同会话已有 run 在飞（等上一轮 `REPLY_END` 再触发） |
| 404 | session/agent 不存在或不属于该用户 |
| SSE 里 `MODEL_CALL_END` 带 error / run 中断 | 模型调用失败（连接/鉴权），看服务端日志 |

### 3.5 会话级 think-tag 覆盖（可选）

覆盖优先级：**会话级覆盖 > Redis 模型表 > 凭证 `inject_think_tag` > 默认
false**。会话级覆盖写 Redis，TTL 4h：

```bash
# 开启（body {"think_tag": true|false}）/ 查询 / 清除
curl -X PUT http://localhost:8000/ellm-models/session/sess-xxx/think-tag \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' -d '{"think_tag": true}'
curl -H 'x-user-id: test-user' http://localhost:8000/ellm-models/session/sess-xxx/think-tag
curl -X DELETE http://localhost:8000/ellm-models/session/sess-xxx/think-tag \
  -H 'x-user-id: test-user'
```

开启后同会话流式响应的首个文本段出现 `<think>` 前缀。

### 3.6 api_key 自动刷新（无需人工干预）

每次模型调用前中间件惰性检查：`apikey_expires_at` 过期（含
`ELLM_KEY_REFRESH_AHEAD_SECS` 提前窗口）→ `MessageBus.acquire_lock` 防抖
→ 同步调 `fetch_ellm_key(api_key_url, scene_code)` 取新 key → 写回凭证
记录 → `set_api_key` 注入请求头；401 `invalid_api_key` 时强制刷新并重试
当前调用一次；刷新失败标记凭证过期，下次调用走惰性刷新恢复。
日志关键字：`injected refreshed ELLM key`。

---

# 二、CI/CD 交付（行内）

## 1. 打包与上传

**上传内容 = `bocom-starter/` 整个目录**（含 `wheels/`），SDK 源码不随包。

### 1.1 bocom-as 源码变更后：重新打包 whl

```bash
cd bocom-agent/bocom-as
rm -f ../bocom-starter/wheels/*.whl        # 保持 wheels/ 单版本
python -m pip wheel --no-deps -w ../bocom-starter/wheels .
```

- 版本：改 `bocom-as/pyproject.toml` 的 `version` 字段 → whl 文件名随之
  变化 → 同步更新 `bocom-starter/Dockerfile` 中的 whl 文件名；
- agentscope SDK 版本约束也在该 pyproject 声明（`==2.0.7.post1`），升级
  SDK 时同步更新 Dockerfile 的 `AGENTSCOPE_VERSION` 构建参数。

### 1.2 上传

上传 `bocom-starter/`（Dockerfile + main.py + .env + docker-compose.yml +
wheels/ + Readme.md），并在行内平台配置启动命令（见下）。

## 2. 构建镜像

构建上下文 = `bocom-starter/`；行内无外网，**必须注入行内 pip 源**
（`docker compose build` 时用宿主环境变量 `PIP_INDEX_URL` 透传，效果相同）：

```bash
cd bocom-starter
docker build -t agentscope-service:bocom \
  --build-arg PIP_INDEX_URL=https://<行内pip源>/simple .
```

镜像特性：

- **包安装、无源码**：agentscope SDK 从行内 pip 源安装，bocom_as 为本地
  whl（config + providers），镜像内不拷贝任何项目源码；
- **分层缓存**：SDK 及其依赖为独立缓存层（`AGENTSCOPE_VERSION` 不变即
  命中），日常只重建 whl 层与应用层，迭代构建快；
- uvicorn 生产默认不 reload（main.py 内 `UVICORN_RELOAD` 控制）。

## 3. 部署启动

```bash
# 仓库根执行（context 自动指向 bocom-starter/）：
docker compose -f bocom-starter/docker-compose.yml up -d
# 或：cd bocom-starter && docker compose up -d
```

- **单服务**：只部署 agentscope；**生产 Redis 复用行内实例，不自建**，
  地址由平台环境变量注入（见下节）；
- **端口**：宿主 `8000` → 容器 `8000`（可参数化 `AGENTSCOPE_HOST_PORT`）；
- **持久化**：`workspace-data` 命名 volume 挂载到 `/app/workspaces`
  （agent 工作区 + 长期记忆 Markdown 文件，容器重建不丢）；
- **Docker 工作区沙箱**：已挂载 `/var/run/docker.sock`，agent 工作区可跑
  嵌套 Docker 沙箱；如行内禁止，删除 compose 中对应挂载行即可。

## 4. 平台需注入的环境变量

compose 透传宿主环境变量（environment 优先于 `.env` 默认值），行内平台
在启动命令前注入以下变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` | ✅ | 生产 Redis（应用主存储：会话、凭证等） |
| `ELLM_REDIS_HOST` / `ELLM_REDIS_PORT` | 否 | 行内模型平台 Redis（默认与主存储同实例） |
| `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 按需 | 各模型供应商 key |
| `ELLM_MODEL_THINK_TAG_KEY` | 否 | 模型 think-tag 表 key（默认 `bocomadp:model:think_tag`，与 bocomadp 数据兼容） |
| `ELLM_KEY_REFRESH_AHEAD_SECS` | 否 | api_key 提前刷新窗口（默认 120s） |

> 未注入的变量不进入容器，退化为 `bocom-starter/.env` 中的默认值
> （`localhost` 类默认值仅适合本地直跑，生产必须注入真实地址）。

## 5. 部署运维要点

- **多实例**：改端口变量 + `-p` project 名（容器/网络/volume 全隔离）：

  ```bash
  AGENTSCOPE_HOST_PORT=8010 docker compose -f bocom-starter/docker-compose.yml -p bocom2 up -d
  ```

  各实例共享生产 Redis（模型表 / 会话数据隔离由业务侧控制）；
- **日志**：`docker compose -f bocom-starter/docker-compose.yml logs -f
  agentscope`；API 文档见 `http://<宿主>:8000/docs`。

---

## 环境变量（见 .env 示例）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` | `localhost` / `6379` | 应用主存储（会话、凭证等）；Docker 部署时由平台注入生产地址 |
| `ELLM_REDIS_HOST` / `ELLM_REDIS_PORT` | `localhost` / `6379` | 行内模型平台 Redis（可独立指定）；Docker 部署时由平台注入 |
| `ELLM_REDIS_TIMEOUT` | `1.0` | Redis 连接超时（秒） |
| `ELLM_REDIS_MAX_CONNECTIONS` | `200` | Redis 连接池上限 |
| `ELLM_MODEL_THINK_TAG_KEY` | `bocomadp:model:think_tag` | 模型 think-tag 表 key（与 bocomadp 数据兼容，可覆盖隔离） |
| `ELLM_KEY_REFRESH_AHEAD_SECS` | `120.0` | api_key 提前刷新窗口（秒） |
| `UVICORN_RELOAD` | `false` | 是否开启 uvicorn 热重载（本地开发设 `true`） |

> bocom-as 的 `config.get_ellm_settings()` 每次调用重建、环境变量热读；
> `.env` 由宿主应用加载（本仓库提供示例），config 不主动加载。
