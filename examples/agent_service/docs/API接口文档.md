# BocomADP threads/runs 对话接口文档（curl 速查）

本服务对外提供 **threads/runs 对话接口**（`/bocomadp/v1/threads`）：thread/run
资源模型 + SSE 流式输出，覆盖创建 run、流式 / 阻塞等待、断线续传、取消等完整对话闭环。

## 1. threads/runs 对话接口（`/bocomadp/v1/threads`）

> - `thread_id` 即 `session_id`（同一资源）；会话**懒创建**——thread 不存在时
>   `runs/stream` 按 agent_id 自动建库
> - `agent_id` 为服务端已注册的智能体 ID，缺省 `jhzd_lead_agent`；未注册 → 404
> - `x-user-id` 请求头必填，用来做沙箱和工作区间隔离
> - 同 session 已有活跃 run 时再次创建 → `409 Conflict`

### 1.1 对话端点总览

| 方法/路径 | 用途 |
|---|---|
| `POST /api/bocomadp/v1/threads/{tid}/runs/stream` | 创建 run 并 SSE 流式返回（响应头 Content-Location 携带 run_id） |
| `POST /api/bocomadp/v1/threads/{tid}/runs/wait` | 创建 run 并阻塞至完成，返回终态 `{run_id, thread_id, status, error}` |
| `GET /api/bocomadp/v1/threads/{tid}/runs/{rid}/stream` | join 已有 run：先回放再 live（`Last-Event-ID` 断点续传；`?cancel_on_disconnect=1` 断线即取消） |
| `GET /api/bocomadp/v1/threads/{tid}/runs/{rid}` | run 状态查询（断线重连时先判断 run 是否已结束；run_id 不存在或不属于该 thread → 404） |
| `POST /api/bocomadp/v1/threads/{tid}/runs/{rid}/cancel` | 取消 run（响应 `{"run_id","status":"interrupted"}`） |

```bash
# ① 创建 run + SSE 流式（-N 实时输出；首帧 metadata 的 data 携带 run_id，下面 ③④⑤ 用到）
curl -N -X POST http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"你好，帮我查一下余额"}}'
# 帧序列：event: metadata（首帧，data 含 run_id）→ event: messages / custom（增量）→ event: end（结束）
# （响应头 Content-Location 同样携带 run_id，两种取法等价）

# ② 创建 run + 阻塞等待（返回终态 JSON：run_id / thread_id / status / error）
curl -X POST http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/wait \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"agent_id":"customer_service","input":{"type":"human","content":"简单回答：1+1=?"}}'

# ③ join 已有 run（{run_id} 换成 ① 首帧 metadata 中的 run_id；回放全部事件；带 Last-Event-ID 则从断点续传）
curl -N http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1'
curl -N http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/{run_id}/stream \
  -H 'x-user-id: u1' -H 'Last-Event-ID: 1-0'

# ④ run 状态查询（已结束 → 直接取终态，无需再订阅流；404 → 回退 ③ 的 join 流端点）
curl http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/{run_id} \
  -H 'x-user-id: u1'
# → {"run_id","thread_id","assistant_id","status":"success","error":null,"metadata":{}}

# ⑤ 取消 run（join 方随后收到 end 帧收敛）
curl -X POST http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/{run_id}/cancel \
  -H 'x-user-id: u1'
```

### 1.2 请求体字段（CreateRunRequest）

`.../runs/stream` 与 `.../runs/wait` 共用同一请求体：

| 字段 | 必填 | 说明 |
|---|---|---|
| `agent_id` | 否 | 智能体 ID（服务端已注册的 agent）；缺省 `jhzd_lead_agent`，未注册 → 404 |
| `assistant_id` | 否 | 历史别名，接受但忽略（前端适配已废弃） |
| `session_id` | 是 | 必须等于路径 `thread_id`（thread_id == session_id 同一资源）；缺失或不一致 → 400 |
| `input` | 否 | 输入消息：单条消息 dict `{"type":"human","content":"..."}` 或 `{"messages":[...]}` 列表；**不接受纯字符串**（→ 400）；HITL（人在回路确认）卡片应答为 `human_input_response` 事件形态（不拦截） |
| `on_disconnect` | 否 | `cancel`（默认，断线即中断 run，停止消耗模型额度）/ `continue`（仅断开订阅，run 继续） |
| `custom_params` | 否 | 请求级运行时配置（空间码 / custom_prompt / 检索开关 / guwp_token / `llm_model_name` 请求级模型名 / `files` 文件引用 / `additional_urls` 等），见 1.5 |
| `reasoning_effort` / `thinking_enabled` | 否 | 请求级 run 配置（根路径）：推理强度（low/medium/high）、是否启用思考模式 |
| `stream_mode` / `multitask_strategy` / `is_plan_mode` / `subagent_enabled` / `mode` / `context` / `config` | 否 | 接受但忽略（固定 messages+custom 流、reject 并发策略） |

### 1.3 SSE 帧格式与事件类型

帧格式：field 顺序 `event:` → `data:` → `id:`（可选）→ 空行，被标准 SSE 客户端直接消费。

```
event: messages
data: [{"type": "AIMessageChunk", "content": "你好", "id": "r1"}]
id: 1725000000000-0

```
- **心跳**：空闲 15s 发纯注释帧 `: heartbeat\n\n`，防止代理/浏览器超时断连
- **结束哨兵**：`event: end\ndata: null\n\n`（客户端以此识别流终止）
- **事件 id**：Redis Stream entry_id（`{ms}-{seq}`），客户端回传 `Last-Event-ID` 头
  断点续传（只认 `<数字>-0` 格式游标）

事件类型：`metadata` / `updates` / `messages` / `custom` / `error` / `end`。
平台内部事件 → SSE 帧的翻译映射：

| 内部事件 | 输出帧 | data 载荷 |
|---|---|---|
| `ReplyStartEvent` | `metadata` | `{run_id, thread_id, assistant_id, reply_id}` 首帧 |
| `TextBlock*`（正文增量） | `messages` | `[{"type":"AIMessageChunk","content":"<delta>","id":"<reply_id>"}]` |
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

支撑前端对话闭环（历史列表 / 恢复界面 / 分页 / 删除）：

| 方法/路径 | 用途 |
|---|---|
| `POST /api/bocomadp/v1/threads` | 创建 thread（仅生成 id，session 懒创建于首次 run） |
| `POST /api/bocomadp/v1/threads/search` | 会话列表（历史列表数据源，按 updated_at 降序 + offset/limit 分页） |
| `GET /api/bocomadp/v1/threads/{tid}/state` | 读取最新状态（`values.messages`，供恢复界面使用） |
| `GET /api/bocomadp/v1/threads/{tid}/messages/page` | 消息分页（仅向后分页：`before_seq` 游标；传 `after_seq` → 422） |
| `POST /api/bocomadp/v1/threads/{tid}/history` | 最近一个 checkpoint（`values.messages`） |
| `DELETE /api/bocomadp/v1/threads/{tid}` | 删除会话及其消息（先中断活跃 run，未找到幂等成功） |

```bash
# ① 创建 thread（可省略：runs/stream 首次调用自动懒创建）→ {"thread_id": "..."}
# 请求体均可选：thread_id（预置 id，缺省服务端生成）；metadata / if_exists 接受但忽略
curl -X POST http://53.192.28.254/api/bocomadp/v1/threads \
  -H 'Content-Type: application/json' -d '{}'

# ② 会话列表（limit/offset 有效，默认 10/0；metadata/status/sortBy/sortOrder/select 接受但忽略）
# → [{"thread_id","status":"idle","created_at","updated_at","metadata":{},"values":{"title":...},"interrupts":{}}]
curl -X POST http://53.192.28.254/api/bocomadp/v1/threads/search \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{"limit":10,"offset":0}'

# ③ 线程最新状态（无请求体；values.messages 供恢复界面使用，最多 50 条）
# → {"values":{"messages":[{type,id,content}]},"next":[],"tasks":[],"checkpoint_id":null,"metadata":{}}
# （checkpoint_id 恒为 null，占位字段；恢复数据取 values.messages）
curl http://53.192.28.254/api/bocomadp/v1/threads/t1/state -H 'x-user-id: u1'

# ④ 消息分页（query：limit 1~200 默认 50；before_seq≥1 游标，不带取最新一页；传 after_seq → 422）
curl 'http://53.192.28.254/api/bocomadp/v1/threads/t1/messages/page?limit=50&before_seq=100' \
  -H 'x-user-id: u1'
# → {"data": [{run_id,seq,content,metadata,created_at}], "has_more": true, "next_before_seq": 51}

# ⑤ 最近 checkpoint（请求体 limit 有效，默认 10；before / metadata 接受但忽略；流结束前拉取最终状态）
# → [{"checkpoint_id","values":{"messages":[...]},"metadata":{}}]（会话为空 → []）
# 注意：checkpoint_id 每次调用随机生成，非稳定标识，不可作分页游标；历史翻页用 ④ 的 before_seq
curl -X POST http://53.192.28.254/api/bocomadp/v1/threads/t1/history \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' -d '{"limit":10}'

# ⑥ 删除会话（无请求体；先中断活跃 run 再删；未找到幂等成功）
curl -X DELETE http://53.192.28.254/api/bocomadp/v1/threads/t1 -H 'x-user-id: u1'
# → {"success": true, "message": "thread t1 deleted"}
```

### 1.5 custom_params 请求级配置

custom_params 是请求级运行时配置通道：每次请求携带的 JSON 对象在该次 run 内生效，
供工具与中间件消费（空间码强制覆盖、自定义提示词、检索开关、认证等）。

| key | 类型 | 语义 |
|---|---|---|
| `space_code_list` | list[str] | 场景知识空间代码列表（强制覆盖模型传参） |
| `team_space_code_list` | list[str] | 团队知识空间代码列表（强制覆盖） |
| `psnl_space_code_id` | str | 个人知识空间 ID（强制覆盖） |
| `user_code` | str | 用户编码（强制覆盖） |
| `search_type` | str | 检索类型（`0` 混合 / `1` 全文 / `2` 向量） |
| `customized_tag_list` | list[str] | 自定义标签过滤（强制覆盖） |
| `psnl_category_id_list` | list[str] | 个人知识分类 ID（强制覆盖） |
| `custom_prompt` | str | 请求级自定义提示词（整体覆盖 system 提示词） |
| `vector_search_switch` | bool | 显式 `false` → 不挂 vector_search 工具（默认挂载；cross_search 始终挂载） |
| `online_search_switch` | bool | 显式 `true` → 挂 online_search 联网搜索（默认不挂） |
| `personal_search_switch` | bool | 显式 `true` 且空间参数齐备 → 挂 personal_search 个人知识库搜索 |
| `tools_param.personalKnowledgeSearch` | dict | 个人空间参数（`psnlSpaceCodeId` / `psnlCategoryIdList`，强制覆盖） |
| `tools_param.source_param` | dict | 向量检索源参数（sourceType / repository / aggRepositories / HNSSParam） |
| `llm_model_name` | str | 请求级模型名（唯一通道，直接采用所传模型名） |
| `files` | list | 文件引用，附加到 human 消息（注入 `<context name="files">` 供工具使用） |
| `additional_urls` | list[str] | 直链列表，run 启动前下载到会话 uploads 目录 |
| `guwp_token` / `jrt_auth_code` / `okic_token` / `okic_type` / `muwp_user` | str / dict | 认证方案字段（优先级 guwp > jrt > okic > muwp） |

**存储与回退语义**：首次带值请求按 session 持久化（TTL 4h），之后不带 custom_params
的请求自动回退加载上次的值（HITL 确认续跑等场景开关状态持续生效）；每次带值请求
整体覆盖旧记录（非合并）；未列出的 key 会被保存但静默忽略。

```bash
curl -N -X POST http://53.192.28.254/api/bocomadp/v1/threads/t1/runs/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: u1' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "你好，介绍一下你自己"},
    "custom_params": {
      "custom_prompt": "你是内部知识助手，回答必须简洁、引用检索结果。",
      "vector_search_switch": true,
      "guwp_token": "demo-guWP-token"
    }
  }'
```
