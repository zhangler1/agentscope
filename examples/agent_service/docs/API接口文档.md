# BocomADP 接口文档（curl 速查）

> 本文件是 `docs/` 文档集的一部分；接口语义细节见同目录 [README.md](./README.md)、
> [api.md](./api.md)（工具白名单 / Token 用量）、[custom_params.md](./custom_params.md)
> （请求级运行时配置）。

- 网关地址：`http://192.168.0.106`（nginx 80 端口，统一入口）
- 转发规则：`/api/xxx` → 剥掉 `/api` 前缀 → `agentscope-service:8000/xxx`（`Docker-agentscope/nginx/nginx.conf`）
- 直连方式（绕过网关）：`http://192.168.0.106:8000` + 服务端原始路径（**不带** `/api` 前缀）
- 路径参数 `{...}` 需替换为实际 ID

## 0. 健康检查

```bash
curl http://192.168.0.106/api/healthz
curl http://192.168.0.106/api/readyz
curl http://192.168.0.106/api/platform/health
curl http://192.168.0.106/api/stats/ping
curl http://192.168.0.106/api/stats/storage
```

## 1. DeerFlow 风格场景对话（`/api/threads`）

> 2026-08 起替代已删除的 `/api/chat/run` + `/api/chat/stop`。执行引擎复用原生
> `ChatService`（agent 构建 / 模型 / 工具 / 审计中间件与原生 `/chat/` 完全一致），
> 输出 deer-flow 2.0（LangGraph Platform）SSE 协议：`event:` → `data:` → `id:`
> 帧 + `Last-Event-ID` 断线续传 + `Content-Location` 响应头。
>
> - `thread_id` 即原生 `session_id`（同一资源）；`agent_id` 为原生
>   storage 中的智能体 ID（由原生 `/agent` 接口创建，见第 2 节）
> - `x-user-id` 请求头**必填**（缺失或为空 → 401）
> - 同 session 已有活跃 run 时再次创建 → `409 Conflict`

```bash
# ① 创建 run + SSE 流式（-N 实时输出；响应头 Content-Location 携带 run_id）
curl -N -X POST http://192.168.0.106/api/threads/t1/runs/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"你好，帮我查一下余额"}}'
# 帧序列：event: metadata（首帧）→ event: messages / custom（增量）→ event: end（结束）
# 帧格式：event: <名> \n data: <JSON> \n id: <游标>（Last-Event-ID 断线续传用）

# ② 创建 run + 阻塞等待（返回终态 JSON：run_id / thread_id / status / error）
curl -X POST http://192.168.0.106/api/threads/t1/runs/wait \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"简单回答：1+1=?"}}'

# ③ join 已有 run（回放全部事件；带 Last-Event-ID 则从断点续传）
curl -N http://192.168.0.106/api/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1'
curl -N http://192.168.0.106/api/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1' -H 'Last-Event-ID: 1-0'

# ④ 取消 run（映射原生 session 级 interrupt；join 方随后收到 end 帧收敛）
curl -X POST http://192.168.0.106/api/threads/t1/runs/{run_id}/cancel \
  -H 'x-user-id: u1'
```

请求体（`CreateRunRequest`）字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `agent_id` | 是 | 原生智能体 ID（storage 中注册的 agent，见第 2 节）；缺失 → 400 |
| `assistant_id` | 否 | LangGraph SDK 别名，接受但忽略（前端适配已废弃） |
| `input` | 否 | 输入消息：单条消息 dict `{"type":"human","content":"..."}` 或 `{"messages":[...]}` 列表；**不接受纯字符串** |
| `custom_params` | 否 | 请求级运行时配置（空间码 / custom_prompt / 检索开关 / guwp_token 等），见下节 |
| `session_id` | 是 | 必须等于路径 `thread_id`（thread_id == session_id 同一资源）；缺失或不一致 → 400 |
| `stream_mode` / `multitask_strategy` | 否 | 接受但忽略（固定 messages+custom 流、reject 并发策略） |
| `on_disconnect` | 否 | `cancel`（默认，断线即中断 run）/ `continue`（仅断开订阅） |

### 1.1 custom_params 请求级配置（速查）

```bash
curl -N -X POST http://192.168.0.106/api/threads/t1/runs/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "你好，介绍一下你自己"},
    "custom_params": {
      "space_code_list": ["SP0000001"],
      "team_space_code_list": ["TEAM01"],
      "user_code": "U001",
      "search_type": "0",
      "custom_prompt": "你是内部知识助手，回答必须简洁、引用检索结果。",
      "vector_search_switch": true,
      "guwp_token": "demo-guWP-token"
    }
  }'
```

> custom_params 完整机制（字段表 / 落盘回退 / 开关语义 / 端到端验证手册）见
> [custom_params.md](./custom_params.md)。首次带值请求会落盘到会话 workspace
> （`sessions/{session_id}/custom_params.json`），后续不带参数的请求自动回退加载。

## 2. 智能体（原生 `/agent` 路由）

智能体全部存于框架 StorageBase（config.yaml `agents` 场景种子机制已移除）。
创建 / 修改 / 删除走框架内置 `/agent` 路由，`system_prompt` 随智能体记录
入库；chat / deerflow 运行时按 `agent_id` 从 storage 解析（不可见 → 404）。
模型选择不再按 agent 绑定：请求级模型名未指定时回退凭证 model 字段 → 内置条目 model_name，全部缺失直接报错（无全局 active provider 兜底）。

## 3. 模型候选（`/ellm-models`、`/model`）

```bash
# 模型候选管理（Redis 模型表 bocomadp:model:think_tag）
curl http://192.168.0.106/api/ellm-models
curl http://192.168.0.106/api/ellm-models/Qwen3-235B-A22B
curl -X POST http://192.168.0.106/api/ellm-models \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3-235B-A22B","think_tag":1,"context_size":1000000,"output_size":384000}'
curl -X PUT http://192.168.0.106/api/ellm-models/Qwen3-235B-A22B \
  -H 'Content-Type: application/json' -d '{"think_tag":0}'
curl -X DELETE http://192.168.0.106/api/ellm-models/Qwen3-235B-A22B

# 按凭证查询模型 / 单模型绑定过滤
curl http://192.168.0.106/api/model/credential
curl -X PATCH http://192.168.0.106/api/model/credential/{credential_id} \
  -H 'Content-Type: application/json' -d '{}'
```

> 原 `/api/models` / `/api/models/active`（ProviderManager 列表与 active 切换）已随 ProviderManager 一并移除。

## 4. 文件上传（`/uploads`）

```bash
curl http://192.168.0.106/api/uploads/limits
curl http://192.168.0.106/api/uploads/files

# 上传文件（multipart）
curl -X POST http://192.168.0.106/api/uploads/files \
  -F 'file=@./test.txt'
curl -X POST http://192.168.0.106/api/uploads/files/streaming \
  -F 'file=@./test.txt'

# 删除 / 下载
curl -X DELETE http://192.168.0.106/api/uploads/files \
  -H 'Content-Type: application/json' -d '{"filename":"test.txt"}'
curl 'http://192.168.0.106/api/uploads/files/download?filename=test.txt'
```

## 5. 框架内置：会话（`/sessions`）

```bash
curl http://192.168.0.106/api/sessions/
curl -X POST http://192.168.0.106/api/sessions/ \
  -H 'Content-Type: application/json' -d '{}'
curl -X PATCH http://192.168.0.106/api/sessions/{session_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/sessions/{session_id}
curl -X POST http://192.168.0.106/api/sessions/{session_id}/interrupt \
  -H 'Content-Type: application/json' -d '{}'
curl http://192.168.0.106/api/sessions/{session_id}/messages
curl http://192.168.0.106/api/sessions/{session_id}/status
curl -N http://192.168.0.106/api/sessions/{session_id}/stream

# 会话 Token 用量（详见 api.md）
curl 'http://192.168.0.106/api/sessions/{session_id}/usage?agent_id=xxx&user_id=xxx'
```

## 6. 框架内置：智能体（`/agent`）

```bash
curl http://192.168.0.106/api/agent/schema
curl http://192.168.0.106/api/agent/schema/v2
curl http://192.168.0.106/api/agent/
curl -X POST http://192.168.0.106/api/agent/ \
  -H 'Content-Type: application/json' -d '{"type":"researcher"}'
curl -X PATCH http://192.168.0.106/api/agent/{agent_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/agent/{agent_id}

# 智能体工具白名单（详见 api.md）
curl http://192.168.0.106/api/agents/{agent_id}/tools
curl -X PUT http://192.168.0.106/api/agents/{agent_id}/tools/{tool_name}
curl -X DELETE http://192.168.0.106/api/agents/{agent_id}/tools/{tool_name}
```

## 7. 框架内置：凭证 / 聊天（`/credential`、`/chat`）

```bash
curl http://192.168.0.106/api/credential/schemas
curl http://192.168.0.106/api/credential/
curl -X POST http://192.168.0.106/api/credential/ \
  -H 'Content-Type: application/json' -d '{}'
curl -X PATCH http://192.168.0.106/api/credential/{credential_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/credential/{credential_id}

# 框架官方聊天接口（fire-and-forget + 订阅模式：POST 后事件经 GET /sessions/{sid}/stream
# 推送；与 deerflow /api/threads 端点并存，web_ui 前端使用）
curl -X POST http://192.168.0.106/api/chat/ \
  -H 'Content-Type: application/json' -d '{"session_id":"s1","input":"你好"}'
```

> deerflow 链路的模型凭证为自动供给：首次建会话时按
> `deerflow-<user_id>-<provider_id>` 幂等写入 credential 存储（不同用户互不冲突），
> 无需手动创建。

## 8. 框架内置：知识库（`/knowledge_bases`）

```bash
curl http://192.168.0.106/api/knowledge_bases/embedding_models
curl http://192.168.0.106/api/knowledge_bases/supported_content_types
curl http://192.168.0.106/api/knowledge_bases/
curl -X POST http://192.168.0.106/api/knowledge_bases/ \
  -H 'Content-Type: application/json' -d '{"name":"kb1"}'
curl http://192.168.0.106/api/knowledge_bases/{kb_id}
curl -X PATCH http://192.168.0.106/api/knowledge_bases/{kb_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/knowledge_bases/{kb_id}
curl http://192.168.0.106/api/knowledge_bases/{kb_id}/documents
curl -X POST http://192.168.0.106/api/knowledge_bases/{kb_id}/documents \
  -F 'file=@./doc.pdf'
curl -X DELETE http://192.168.0.106/api/knowledge_bases/{kb_id}/documents/{doc_id}
curl -X POST http://192.168.0.106/api/knowledge_bases/{kb_id}/search \
  -H 'Content-Type: application/json' -d '{"query":"关键词"}'
```

## 9. 框架内置：工作区 / 技能 / MCP / Hub

```bash
# workspace：MCP 与技能管理
curl http://192.168.0.106/api/workspace/mcp
curl -X POST http://192.168.0.106/api/workspace/mcp \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/workspace/mcp/{mcp_name}
curl http://192.168.0.106/api/workspace/skill
curl -X POST http://192.168.0.106/api/workspace/skill/upload \
  -F 'file=@./skill.zip'
curl -X DELETE http://192.168.0.106/api/workspace/skill/{skill_name}

# skill（框架内置 + bocomadp 外部技能市场）
curl http://192.168.0.106/api/skill/
curl http://192.168.0.106/api/skill/{skill_id}
curl -X DELETE http://192.168.0.106/api/skill/{skill_id}
curl http://192.168.0.106/api/workspace/skills/external
curl http://192.168.0.106/api/workspace/skills/uploaded
curl -X POST http://192.168.0.106/api/workspace/skill/download/{skill_full_name}

# mcp
curl http://192.168.0.106/api/mcp/
curl -X PATCH http://192.168.0.106/api/mcp/{mcp_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/mcp/{mcp_id}

# hub（ClawHub / GitHub MCP 市场代理）
curl http://192.168.0.106/api/hub/mcp
curl http://192.168.0.106/api/hub/mcp/{hub_id}/cards
curl http://192.168.0.106/api/hub/mcp/{hub_id}/cards/{card_id}
curl -X POST http://192.168.0.106/api/hub/mcp/{hub_id}/cards/{card_id}/install
curl http://192.168.0.106/api/hub/skill
curl -X POST http://192.168.0.106/api/hub/skill/{hub_id}/cards/{card_id}/install
```

## 10. 其他（`/schedule`、`/tts-model`）

```bash
# 定时任务
curl http://192.168.0.106/api/schedule/
curl -X POST http://192.168.0.106/api/schedule/ \
  -H 'Content-Type: application/json' -d '{}'
curl -X PATCH http://192.168.0.106/api/schedule/{schedule_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/schedule/{schedule_id}
curl http://192.168.0.106/api/schedule/{schedule_id}/sessions

# TTS 模型
curl http://192.168.0.106/api/tts-model/
```

## 备注

- 场景会话闭环验证只需第 0/1/2 组命令：`POST /api/threads/t1/runs/stream` 用 `agent_id` 验证 agent 路由（不同 agent → 不同 system_prompt / 工具白名单）
- OpenAPI 在线文档：`http://192.168.0.106:8000/docs` 或 `http://192.168.0.106:8000/openapi.json`（直连端口）
- 会话状态与消息由原生 storage 落库（config.yaml `db.url`，PostgreSQL）；工作区文件根目录见 config.yaml `workspace_dir`（docker 挂载 `examples/agent_service/workspaces`）
- custom_params 落盘位置：会话 workspace 的 `sessions/{session_id}/custom_params.json`（本地模式即 `{workspace_dir}/{agent_id}/sessions/{session_id}/` 下）
