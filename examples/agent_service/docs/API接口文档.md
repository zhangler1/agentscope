# BocomADP 接口文档（curl 速查）

> 本文件是 `docs/` 文档集的一部分；接口语义细节见同目录 [README.md](./README.md)、
> [api.md](./api.md)（工具白名单 / Token 用量）、[custom_params.md](./custom_params.md)
> （请求级运行时配置）。

本服务对外暴露**两套接口体系**：

- **DeerFlow 风格接口**（第 1 节）：`bocomadp/deerflow/` 适配层新增路由
  （`/deerflow/threads`、`/deerflow/v1/auth`），对齐 deer-flow 2.0（LangGraph Platform）
  SSE 协议，供 deer-flow 前端（`useStream`）/ `langgraph-sdk` 零改动接入；
- **AgentScope 原生接口**（第 2~9 节）：框架 `create_app()` 内置注册的 12 个路由模块
  （/agent、/sessions、/chat、/credential、/model、/skill、/workspace、/hub、/mcp、
  /knowledge_bases、/schedule、/tts-model），自研前端 / 脚本接入走这套。

两套接口共享同一执行引擎（`ChatService`）与配置存储，智能体 / 提示词 / 工具 / 凭证
配置完全一致，仅对话通道不同（对比见第 10 节）。

- 网关地址：`http://192.168.0.106`（nginx 80 端口，统一入口）
- 转发规则：`/api/xxx` → 剥掉 `/api` 前缀 → `agentscope-service:8000/xxx`（`Docker-agentscope/nginx/nginx.conf`）
- 直连方式（绕过网关）：`http://192.168.0.106:8000` + 服务端原始路径（**不带** `/api` 前缀）
- 路径参数 `{...}` 需替换为实际 ID
- 身份头 `X-User-ID`：原生接口缺省 `default`（自带默认凭证）；DeerFlow 风格接口缺省
  `jhzd`（jx_chat 前端不携带）；显式携带时均原样采用

## 0. 健康检查

```bash
curl http://192.168.0.106/api/healthz
curl http://192.168.0.106/api/readyz
curl http://192.168.0.106/api/platform/health
curl http://192.168.0.106/api/stats/ping
curl http://192.168.0.106/api/stats/storage
```

## 1. DeerFlow 风格对话接口（`/deerflow/threads`）

> 2026-08 起替代已删除的 `/api/chat/run` + `/api/chat/stop`。执行引擎复用原生
> `ChatService`（agent 构建 / 模型 / 工具 / 审计中间件与原生 `/chat/` 完全一致），
> 输出 deer-flow 2.0（LangGraph Platform）SSE 协议：`event:` → `data:` → `id:` 帧 +
> `Last-Event-ID` 断线续传 + `Content-Location` 响应头。
>
> - `thread_id` 即原生 `session_id`（同一资源）；会话**懒创建**——thread 不存在时
>   `runs/stream` 按 agent_id 自动建库，无需先调原生 `/sessions/`
> - `agent_id` 为原生 storage 中的智能体 ID（由原生 `/agent` 接口创建，见第 3 节），
>   缺省 `jhzd_lead_agent`；storage 中不可见 → 404
> - `x-user-id` 请求头可选，缺失/为空回退 `jhzd`（显式携带时原样采用）
> - 同 session 已有活跃 run 时再次创建 → `409 Conflict`
> - 旧路径 `/api/threads/...`、`/api/v1/auth/...` 由 nginx 网关 rewrite 兼容到
>   `/api/deerflow/...`

### 1.1 对话端点总览

| 方法/路径 | 用途 |
|---|---|
| `POST /api/deerflow/threads/{tid}/runs/stream` | 创建 run 并 SSE 流式返回（响应头 Content-Location 携带 run_id） |
| `POST /api/deerflow/threads/{tid}/runs/wait` | 创建 run 并阻塞至完成，返回终态 `{run_id, thread_id, status, error}` |
| `GET /api/deerflow/threads/{tid}/runs/{rid}/stream` | join 已有 run：先回放再 live（`Last-Event-ID` 断点续传；`?cancel_on_disconnect=1` 断线即取消） |
| `GET /api/deerflow/threads/{tid}/runs/{rid}` | run 详情（对齐 LangGraph SDK `runs.get`，SDK 断线重连前做终态预检） |
| `POST /api/deerflow/threads/{tid}/runs/{rid}/cancel` | 取消 run（映射原生 session 级 interrupt） |

```bash
# ① 创建 run + SSE 流式（-N 实时输出；-D 保存响应头，Content-Location 携带 run_id）
curl -N -D /tmp/df_headers.txt -X POST http://192.168.0.106/api/deerflow/threads/t1/runs/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"你好，帮我查一下余额"}}'
# 帧序列：event: metadata（首帧）→ event: messages / custom（增量）→ event: end（结束）

# 从响应头提取 run_id（LangGraph SDK 内部同样用正则从该头解析）
RUN_ID=$(grep -i '^content-location:' /tmp/df_headers.txt | tr -d '\r' | awk -F/ '{print $NF}')

# ② 创建 run + 阻塞等待（返回终态 JSON：run_id / thread_id / status / error）
curl -X POST http://192.168.0.106/api/deerflow/threads/t1/runs/wait \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"简单回答：1+1=?"}}'

# ③ join 已有 run（回放全部事件；带 Last-Event-ID 则从断点续传）
curl -N http://192.168.0.106/api/deerflow/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1'
curl -N http://192.168.0.106/api/deerflow/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1' -H 'Last-Event-ID: 1-0'

# ④ run 详情（终态预检；run 不在记账内 → 404，SDK 捕获后回退 joinStream）
curl http://192.168.0.106/api/deerflow/threads/t1/runs/{run_id} \
  -H 'x-user-id: u1'

# ⑤ 取消 run（join 方随后收到 end 帧收敛；响应 {"run_id","status":"interrupted"}）
curl -X POST http://192.168.0.106/api/deerflow/threads/t1/runs/{run_id}/cancel \
  -H 'x-user-id: u1'
```

### 1.2 请求体字段（CreateRunRequest）

`.../runs/stream` 与 `.../runs/wait` 共用同一请求体，兼容 LangGraph SDK 调用契约：

| 字段 | 必填 | 说明 |
|---|---|---|
| `agent_id` | 否 | 原生智能体 ID（storage 中注册的 agent，见第 3 节）；缺省 `jhzd_lead_agent`，storage 中不可见 → 404 |
| `assistant_id` | 否 | LangGraph SDK 别名，接受但忽略（前端适配已废弃） |
| `session_id` | 是 | 必须等于路径 `thread_id`（thread_id == session_id 同一资源）；缺失或不一致 → 400 |
| `input` | 否 | 输入消息：单条消息 dict `{"type":"human","content":"..."}` 或 `{"messages":[...]}` 列表；**不接受纯字符串**（→ 400）；HITL 确认卡片应答为 `human_input_response` 事件形态（不拦截） |
| `on_disconnect` | 否 | `cancel`（默认，断线即中断 run，停止消耗模型额度）/ `continue`（仅断开订阅，run 继续） |
| `custom_params` | 否 | 请求级运行时配置（空间码 / custom_prompt / 检索开关 / guwp_token / `llm_model_name` 请求级模型名 / `files` 文件引用 / `additional_urls` 等），见 1.6 |
| `reasoning_effort` / `thinking_enabled` | 否 | 请求级 run 配置（根路径）：推理强度（low/medium/high）、是否启用思考模式 |
| `stream_mode` / `multitask_strategy` / `is_plan_mode` / `subagent_enabled` / `mode` / `context` / `config` | 否 | 接受但忽略（固定 messages+custom 流、reject 并发策略） |

### 1.3 SSE 帧格式与事件类型

帧格式（`bocomadp/deerflow/protocol.py`）对齐 deer-flow 2.0 `format_sse`：
field 顺序 `event:` → `data:` → `id:`（可选）→ 空行，被 LangGraph Platform 生态
（`useStream` / `langgraph-sdk` SSE decoder）直接消费。

```
event: messages
data: [{"type": "AIMessageChunk", "content": "你好", "id": "r1"}, {"langgraph_node": "agent"}]
id: 1725000000000-0

```
- **心跳**：空闲 15s 发纯注释帧 `: heartbeat\n\n`，防止代理/浏览器超时断连
- **结束哨兵**：`event: end\ndata: null\n\n`（LangGraph SDK 以此识别流终止）
- **事件 id**：Redis Stream entry_id（`{ms}-{seq}`），客户端回传 `Last-Event-ID` 头
  断点续传（只认 `<数字>-0` 格式游标）

事件类型（对齐 deer-flow StreamEvent 枚举）：`metadata` / `updates` / `messages` /
`custom` / `error` / `end`。原生 AgentEvent → deer-flow 帧的翻译映射
（`bocomadp/deerflow/formatter.py`）：

| 原生事件 | 输出帧 | data 载荷 |
|---|---|---|
| `ReplyStartEvent` | `metadata` | `{run_id, thread_id, assistant_id, reply_id}` 首帧 |
| `TextBlock*`（正文增量） | `messages` | `[{"type":"AIMessageChunk","content":"<delta>","id":"<reply_id>"}, {"langgraph_node":"agent"}]` |
| `ThinkingBlock*`（思考增量） | `messages` | chunk 附 `additional_kwargs.reasoning_content`，metadata 附 `reasoning:true` |
| `ToolCallStart/Delta/End` | `messages` + `updates` | `tool_call_chunks` 增量帧（按同 id concat）+ `{"model":{"messages":[完整 ai 消息快照]}}` |
| `ToolResult*`（工具结果） | `messages` | `[{"type":"tool","content","name","tool_call_id","id","status":"success"/"error","artifact"}, metadata]` |
| `RequireUserConfirmEvent`（HITL） | `messages` + `custom` + `end` | 确认卡片（`name=ask_clarification` + `artifact.human_input`）+ `on_require_confirm` + end 哨兵 |
| `CustomEvent` / 未知事件 | `custom` | 原样透传而非丢弃 |
| `ReplyEndEvent(normal)` | `end` | 哨兵（data=null） |
| `ReplyEndEvent(error)` | `error` + `end` | `{"message","name"}` 后接哨兵 |

run 状态枚举：`pending` / `running` / `success` / `error` / `interrupted`。

> HITL 挂起防护：会话仍在等待工具确认（ASKING）/ 外部执行（SUBMITTED）时，普通消息
> 返回命名错误帧 `event: error`（`name: ToolConfirmationPending`）+ end 哨兵；确认卡片
> 应答（`input` 带 `human_input_response`）本身就是解卡动作，不拦截。

### 1.4 threads 管理端点

对齐 deer-flow 2.0 同路径契约的最小集（`bocomadp/deerflow/routers/threads.py`），
支撑前端对话闭环（历史列表 / 恢复界面 / 分页 / 删除）：

| 方法/路径 | 用途 |
|---|---|
| `POST /api/deerflow/threads` | 创建 thread（仅生成 id，session 懒创建于首次 run） |
| `POST /api/deerflow/threads/search` | 会话列表（历史列表数据源，按 updated_at 降序 + offset/limit 分页） |
| `GET /api/deerflow/threads/{tid}/state` | 读取最新状态（`values.messages`，SDK `getState`） |
| `GET /api/deerflow/threads/{tid}/messages/page` | 消息分页（仅向后分页：`before_seq` 游标；传 `after_seq` → 422） |
| `POST /api/deerflow/threads/{tid}/history` | 最近一个 checkpoint（`values.messages`，SDK `getHistory`） |
| `DELETE /api/deerflow/threads/{tid}` | 删除会话及其消息（先中断活跃 run，未找到幂等成功） |

```bash
# ① 创建 thread（可省略：runs/stream 首次调用自动懒创建）→ {"thread_id": "..."}
curl -X POST http://192.168.0.106/api/deerflow/threads \
  -H 'Content-Type: application/json' -d '{}'

# ② 会话列表（历史列表：thread_id / status / created_at / updated_at / values.title）
curl -X POST http://192.168.0.106/api/deerflow/threads/search \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"limit":10,"offset":0}'

# ③ 线程最新状态（values.messages 供 SDK getState 恢复界面）
curl http://192.168.0.106/api/deerflow/threads/t1/state -H 'x-user-id: u1'

# ④ 消息分页（不带 before_seq 取最新一页；带 before_seq 取更早一页）
curl 'http://192.168.0.106/api/deerflow/threads/t1/messages/page?limit=50&before_seq=100' \
  -H 'x-user-id: u1'
# → {"data": [...], "has_more": true, "next_before_seq": 51}

# ⑤ 最近 checkpoint（SDK getHistory，流结束 onFinish 前拉取最终状态）
curl -X POST http://192.168.0.106/api/deerflow/threads/t1/history \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' -d '{"limit":10}'

# ⑥ 删除会话（先中断活跃 run 再删；未找到幂等成功）
curl -X DELETE http://192.168.0.106/api/deerflow/threads/t1 -H 'x-user-id: u1'
# → {"success": true, "message": "thread t1 deleted"}
```

### 1.5 认证桩（`/deerflow/v1/auth`）

deer-flow 前端 SSR 鉴权兼容（`bocomadp/deerflow/routers/auth_stub.py`）：bocomadp 无
独立用户体系，返回固定管理员用户；生产接入真实认证后应删除。

```bash
curl http://192.168.0.106/api/deerflow/v1/auth/me
# → {"id":"default","email":"default@test.local","system_role":"admin","needs_setup":false,"oauth_provider":null}
curl http://192.168.0.106/api/deerflow/v1/auth/setup-status
# → {"needs_setup": false}
```

### 1.6 custom_params 请求级配置（速查）

```bash
curl -N -X POST http://192.168.0.106/api/deerflow/threads/t1/runs/stream \
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

## 2. 原生对话链路（`/chat` + `/sessions`）

框架官方聊天接口：**fire-and-forget + 订阅模式**——`POST /api/chat/` 立即返回
`{"status":"started"}`，对话在后台异步执行，事件经 `GET /api/sessions/{sid}/stream`
SSE 推送（web_ui 前端使用；与 DeerFlow 风格 `/deerflow/threads` 端点并存）。

```bash
# 发起对话（fire-and-forget；需先创建会话）
curl -X POST http://192.168.0.106/api/chat/ \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"xxx","session_id":"s1","input":{"name":"user","role":"user","content":[{"type":"text","text":"你好"}],"metadata":{}}}'

# 会话 CRUD（创建时需带 agent_id 与 chat_model_config：凭证类型 / 凭证 id / 模型名 / 参数）
curl http://192.168.0.106/api/sessions/
curl -X POST http://192.168.0.106/api/sessions/ \
  -H 'Content-Type: application/json' -d '{}'
curl -X PATCH http://192.168.0.106/api/sessions/{session_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/sessions/{session_id}

# 中断运行中/停驻的 run（幂等：空闲会话为 no-op），释放"一个 session 至多一个活跃 run"的并发名额
curl -X POST http://192.168.0.106/api/sessions/{session_id}/interrupt \
  -H 'Content-Type: application/json' -d '{}'

# 会话消息 / 状态 / SSE 事件流订阅
curl http://192.168.0.106/api/sessions/{session_id}/messages
curl http://192.168.0.106/api/sessions/{session_id}/status
curl -N http://192.168.0.106/api/sessions/{session_id}/stream

# 会话 Token 用量（详见 api.md）
curl 'http://192.168.0.106/api/sessions/{session_id}/usage?agent_id=xxx&user_id=xxx'
```

## 3. 智能体（原生 `/agent`）

智能体全部存于框架 StorageBase（config.yaml `agents` 场景种子机制已移除）。创建 /
修改 / 删除走框架内置 `/agent` 路由，`system_prompt` 随智能体记录入库；chat /
deerflow 运行时按 `agent_id` 从 storage 解析（不可见 → 404）。模型选择不再按 agent
绑定：请求级模型名未指定时回退凭证 model 字段 → 内置条目 model_name，全部缺失直接
报错（无全局 active provider 兜底）。

```bash
# 创建表单 schema（前端表单据此渲染；v2 为含 BocomADP 扩展字段的版本）
curl http://192.168.0.106/api/agent/schema
curl http://192.168.0.106/api/agent/schema/v2

# 智能体 CRUD（创建返回 {"agent_id": "..."}）
curl http://192.168.0.106/api/agent/
curl -X POST http://192.168.0.106/api/agent/ \
  -H 'Content-Type: application/json' \
  -d '{"name":"customer_service","system_prompt":"你是客服助手","context_config":{},"react_config":{}}'
curl -X PATCH http://192.168.0.106/api/agent/{agent_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/agent/{agent_id}

# 智能体工具白名单（详见 api.md）
curl http://192.168.0.106/api/agents/{agent_id}/tools
curl -X PUT http://192.168.0.106/api/agents/{agent_id}/tools/{tool_name}
curl -X DELETE http://192.168.0.106/api/agents/{agent_id}/tools/{tool_name}
```

## 4. 凭证（`/credential`）

```bash
# 凭证类型 schema（type 判别字段与必填项，前端表单据此渲染）
curl http://192.168.0.106/api/credential/schemas

# 凭证 CRUD（创建返回 {"credential_id": "..."}）
curl http://192.168.0.106/api/credential/
curl -X POST http://192.168.0.106/api/credential/ \
  -H 'Content-Type: application/json' \
  -d '{"data":{"type":"deepseek_credential","api_key":"sk-xxx","base_url":"https://api.deepseek.com"}}'
curl -X PATCH http://192.168.0.106/api/credential/{credential_id} \
  -H 'Content-Type: application/json' -d '{}'
curl -X DELETE http://192.168.0.106/api/credential/{credential_id}

# 按凭证查询模型 / 单模型绑定过滤
curl http://192.168.0.106/api/model/credential
curl -X PATCH http://192.168.0.106/api/model/credential/{credential_id} \
  -H 'Content-Type: application/json' -d '{}'
```

> deerflow 链路的模型凭证为自动供给：首次建会话时按
> `deerflow-<user_id>-<provider_id>` 幂等写入 credential 存储（不同用户互不冲突），
> 无需手动创建。

## 5. 模型候选（`/ellm-models`）

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
```

> 原 `/api/models` / `/api/models/active`（ProviderManager 列表与 active 切换）已随 ProviderManager 一并移除。

## 6. 文件上传（`/uploads`）

```bash
curl http://192.168.0.106/api/uploads/limits
curl http://192.168.0.106/api/uploads/files

# 上传文件（multipart；streaming 为流式上传）
curl -X POST http://192.168.0.106/api/uploads/files \
  -F 'file=@./test.txt'
curl -X POST http://192.168.0.106/api/uploads/files/streaming \
  -F 'file=@./test.txt'

# 删除 / 下载
curl -X DELETE http://192.168.0.106/api/uploads/files \
  -H 'Content-Type: application/json' -d '{"filename":"test.txt"}'
curl 'http://192.168.0.106/api/uploads/files/download?filename=test.txt'
```

## 7. 知识库（`/knowledge_bases`）

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

## 8. 工作区 / 技能 / MCP / Hub

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

## 9. 其他（`/schedule`、`/tts-model`）

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

## 10. 原生与 DeerFlow 对话接口对比

| 维度 | 原生（/chat + /sessions） | DeerFlow 风格（/deerflow/threads） |
|---|---|---|
| 发起对话 | `POST /api/chat/`（fire-and-forget，立即返回 started） | `POST /api/deerflow/threads/{tid}/runs/stream`（单连接流式） |
| 订阅结果 | `GET /api/sessions/{sid}/stream` | `GET /api/deerflow/threads/{tid}/runs/{rid}/stream`（回放 + live） |
| 资源模型 | agent / session / reply | thread / run（thread_id == session_id） |
| run 标识 | 事件流内 reply_id | 响应头 `Content-Location` 预生成 run_id |
| 消息格式 | 原生 Msg（role + content 块） | LangGraph 消息（human/ai/tool、AIMessageChunk 增量） |
| 断线续传 | 无游标，重连从头回放 | `Last-Event-ID`（Redis Stream entry_id）精确续传 |
| 断线行为 | 持续订阅 | `on_disconnect=cancel`（默认）/ `continue` |
| 会话创建 | `POST /api/sessions/`（需 chat_model_config） | 懒创建（runs/stream 自动建库，模型走请求级 `custom_params.llm_model_name` 或默认凭证） |
| 并发控制 | 一个 session 至多一个活跃 run（409） | 相同（复用 ChatRunRegistry + 分布式锁） |
| 取消 | `POST /api/sessions/{sid}/interrupt` | `POST /api/deerflow/threads/{tid}/runs/{rid}/cancel` |
| 消费方 | 任意 SSE 客户端 | deer-flow 前端（useStream）/ langgraph-sdk |

**选择建议**：接入方为自研前端 / 脚本 → 原生接口（配置控制最细：凭证、模型、参数全部显式）；接入方为 deer-flow 前端或 LangGraph SDK 生态 → DeerFlow 风格接口（协议对齐，免适配）。两者可混用：同一 thread 上由原生 `/chat/` 触发的 run 也可用 `runs/{rid}/stream` join。

## 备注

- 场景会话闭环验证只需第 0/1/3 组命令：`POST /api/deerflow/threads/t1/runs/stream` 用 `agent_id` 验证 agent 路由（不同 agent → 不同 system_prompt / 工具白名单）
- OpenAPI 在线文档：`http://192.168.0.106:8000/docs` 或 `http://192.168.0.106:8000/openapi.json`（直连端口）
- 会话状态与消息由原生 storage 落库（config.yaml `db.url`，PostgreSQL）；工作区文件根目录见 config.yaml `workspace_dir`（docker 挂载 `examples/agent_service/workspaces`）
- custom_params 落盘位置：会话 workspace 的 `sessions/{session_id}/custom_params.json`（本地模式即 `{workspace_dir}/{agent_id}/sessions/{session_id}/` 下）