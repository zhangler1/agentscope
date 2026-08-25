# bocomadp custom_params 机制教学文档

> 面向对象：bocomadp 后端开发者。
> 前置知识：Python asyncio、FastAPI、AgentScope 的 Tool / Middleware 概念。
> 代码位置：`examples/agent_service/bocomadp/`（下文用 `bocomadp/` 指代）。

## 1. 一句话概述

`custom_params` 是一条**请求级运行时配置通道**：前端在 `POST /threads/{thread_id}/runs/stream`（或 `/runs/wait`）请求体里携带一个 JSON 对象，框架把它注入到该次 run 的后台任务上下文中，供**工具中间件、Agent 中间件、工具构建工厂**在 run 任务内读取，从而在**不重启服务、不改配置、不侵入 Agent 代码**的前提下，按请求动态控制：

- 空间码检索参数（强制覆盖模型传参）
- 自定义提示词（custom_prompt）
- 检索开关（行内/联网/个人三个维度）
- 认证方案（guwp / jrt / okic / muwp 四选一）

并对齐 deer-flow 的持久化语义：请求带值时写入 Redis（按 session_id，TTL 4h 原生过期），之后不带也能回退加载。

## 2. 背景：为什么需要 custom_params

deer-flow 的 `run/stream` 接口原生支持 `custom_params` 请求体字段，前端可以随每次请求动态下发运行时配置。bocomadp 沿用了这一接口形态（`deerflow/` 目录本身就是 deer-flow 风格的兼容层），但早期实现中这些参数"传了没人消费"。

核心矛盾在于 **AgentScope 的 Agent 是请求处理开始后才在框架内部组装的**，业务侧拿不到 deer-flow 那种"agent 构建时注入"的钩子：

| 场景 | deer-flow | bocomadp（本方案） |
|---|---|---|
| 空间码注入 | Agent 构建时包装工具 | 工具级中间件在每次 tool call 时强制覆盖 |
| 自定义提示词 | `system_prompt=custom_prompt` 整体替换 | `on_system_prompt` 中间件整体覆盖（transformer 模式） |
| 检索开关 | 构建时过滤工具列表 | 工具构建工厂按开关决定挂载 |

bocomadp 的解法是**把"构建时参数"转化为"运行时参数"**：用 ContextVar 把请求级参数送进后台 run 任务，让各个运行时钩子点自行消费。

## 3. 整体架构：数据流全景

```
前端 POST /threads/{id}/runs/stream
  body.custom_params = {"space_code_list": ["S1"], "custom_prompt": "...",
                        "vector_search_switch": true, "guwp_token": "..."}
        │
        ▼
deerflow_chat.py  路由层（FastAPI 端点）
  1) _resolve_custom_params()   ← 带值：写入 Redis；不带：从 Redis 回退
  2) set_custom_params()        ← ContextVar.set(resolved)
  3) _set_run_auth_contexts()   ← ResolvedAuth + save_auth(Redis) + _current_token 联动
  4) _spawn_run()               ← asyncio.create_task 复制当前 ContextVar 快照
  5) reset（不影响已创建的后台任务）
        │
        ▼  run 任务内（ContextVar 已复制进来）
  ┌─────────────────────────────────────────────────────────────┐
  │ AgentToolFactory → build_enterprise_tools()                 │
  │   · cross_search 始终挂载                                    │
  │   · vector_search_switch=False → 不挂 vector_search 工具     │
  │   · online_search_switch=true → 挂 online_search             │
  │   · personal_search_switch=true + 空间参数齐备 → 挂 personal_search │
  │                                                             │
  │ Agent 组装 → build_enterprise_middlewares()                 │
  │   · CustomPromptMiddleware.on_system_prompt → 整体覆盖提示词 │
  │                                                             │
  │ 模型调用工具 → PersonalSpacecodeOverrideMiddleware.on_tool_call │
  │   · personal_search 空间码强制覆盖                           │
  │                                                             │
  │ 工具后端 → get_resolved_auth() 读取认证信息（预留消费点）     │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
Redis: bocomadp:session:{session_id}:custom_params（TTL 4h 原生过期，多 worker 共享）
```

### 3.1 涉及的 6 个文件

| 文件 | 角色 |
|---|---|
| `bocomadp/deerflow/custom_params.py` | ContextVar 存取 + Redis 存储委托（save/load） |
| `bocomadp/deerflow/_session_store.py` | 会话级 Redis 存储（custom_params + auth 同 key 同 TTL） |
| `bocomadp/deerflow/auth_context.py` | 认证方案解析（ResolvedAuth）+ ContextVar + save_auth/load_auth |
| `bocomadp/deerflow/routers/deerflow_chat.py` | 路由层入口：resolve → set → spawn → reset |
| `bocomadp/tools/cross_search.py` | 空间码覆盖中间件 + personal 开关参数层处理 |
| `bocomadp/middleware/custom_prompt.py` | 自定义提示词注入中间件 |
| `bocomadp/middleware/factory.py` + `bocomadp/tools/enterprise.py` | 中间件/工具装配点（读取开关） |

## 4. 核心机制一：ContextVar 请求级传播

### 4.1 为什么是 ContextVar 而不是全局变量

HTTP 服务天然并发：多个请求同时在处理，每个请求的参数必须**只对属于自己的那一个 run 可见**。

- **全局变量**：请求 A 设置后，请求 B 会读到 A 的值——串台，绝对不行。
- **threading.local**：单线程事件循环里所有请求共享同一个线程，thread-local 不隔离。
- **ContextVar**（`contextvars` 标准库）：值绑定在**当前执行上下文**（context）上，每个请求/任务各自持有。关键特性是 **`asyncio.create_task` 会复制创建时的上下文快照**——这正是我们利用的传播机制。

### 4.2 传播链路（重点理解）

```python
# 路由层（deerflow_chat.py，节选）
resolved_params = await _resolve_custom_params(...)
ctx_token = set_custom_params(resolved_params)   # ① 在当前 context set
auth_tokens = _set_run_auth_contexts(resolved_params)
try:
    record, _task = _spawn_run(...)              # ② 内部 asyncio.create_task
finally:
    _reset_run_auth_contexts(auth_tokens)        # ③ 当前 context reset
    reset_custom_params(ctx_token)               #    不影响②已创建的任务
```

理解要点：

1. **①set 只影响当前 context**——路由层 set 的值，工具中间件所在的 run 任务最初是读不到的；
2. **②create_task 复制快照**——spawn 后台任务时，当前 context 连同 custom_params 一起被复制进新任务，run 任务内的所有 `get_custom_params()` 都能读到；
3. **③reset 不回溯子任务**——路由层 set/reset 成对出现，防止污染同一协程内后续的请求处理，而已经创建的子任务持有自己的快照，不受影响。

### 4.3 ContextVar 的两个经典坑

**坑一：可变默认值共享。** `ContextVar("custom_params", default={})` 的 `{}` 是**所有未 set 的 context 共享的同一个对象**。因此在未 set 的 context 里 `get_custom_params()["x"] = 1` 会污染所有其他 context 的默认值。本项目约定：**只读不写**（消费点一律 `params.get(...)`），写入只发生在路由层 set 之后。

**坑二：线程池不传回。** `asyncio.to_thread` / 裸 `threading.Thread` 不会自动继承 ContextVar。如果将来某消费点被放到线程池执行，需要显式把参数传进去（当前所有消费点均在 async 上下文，无此问题）。

## 5. 核心机制二：Redis 持久化与回退

对齐 deer-flow 的 `_save_custom_params` / `_load_custom_params`，但存储从 workspace 文件改为**会话级 Redis**（2026-08-20 用户改选）：

```
请求带 custom_params ──► _resolve_custom_params ──► save 到 Redis ──► 采用请求值
请求不带 custom_params ──► 从 Redis 回退 load ──► 有记录用记录值 / 无记录用 {}
```

- **存储**：`bocomadp/deerflow/_session_store.py`，纯 Redis、无自建清扫任务。
  - key：`bocomadp:session:{session_id}:custom_params`
  - hash 字段：`params`（custom_params JSON）/ `auth`（ResolvedAuth JSON）
  - TTL：Redis 原生 `EXPIRE` 4h 自动过期，条目**同生共死**（auth 与 custom_params 一起失效）
- **客户端**：懒加载 `redis.asyncio.Redis`（参数来自 AppConfig，连接超时 2s）；**多 worker / 多实例共享同一 Redis**，回退加载不 miss，无进程内 dict 的 worker 隔离问题。
- **关键设计——非致命降级（fail-open）**：Redis 不可用时 save 仅 `logger.warning` 不阻断 run、load 返回 None。与 `pool_config.py` 一致；生产消息总线已是 RedisMessageBus，同设施可用性一致。
- **覆盖语义**：每次带值请求整体覆盖旧记录（HSET 更新，不是合并）。

```python
# custom_params.py 核心接口（Redis 化后）
def set_custom_params(params) -> Token      # ContextVar.set
def reset_custom_params(token) -> None      # ContextVar.reset
def get_custom_params() -> dict[str, Any]   # ContextVar.get（消费点用）

async def save_custom_params(session_id, params) -> None  # 委托 _session_store，非致命
async def load_custom_params(session_id) -> dict | None   # 委托 _session_store，非致命
```

鉴权快照同样按会话写 Redis（`save_auth` / `load_auth`，见 `auth_context.py`），与
custom_params 同 key 同 TTL，HITL 续跑等场景一并回退恢复。

## 6. 各消费点详解

### 6.1 空间码强制覆盖（cross_search.py）

**目的**：模型有时会"擅自修改"或"遗忘"空间码。请求方（前端）知道用户真实的空间范围，必须**强制纠正**，不能信任模型传参。

**实现**：`FunctionTool` 挂载工具级中间件（AgentScope 的 `ToolMiddlewareBase` 洋葱模型），每次 tool call 前对覆盖键直接赋值：

```python
_OVERRIDE_KEYS = (
    "space_code_list", "team_space_code_list", "psnl_space_code_id",
    "user_code", "search_type", "customized_tag_list", "psnl_category_id_list",
)

class _SpacecodeOverrideMiddleware(ToolMiddlewareBase):
    async def on_tool_call(self, tool, input_kwargs, next_handler):
        params = get_custom_params()
        for key in _OVERRIDE_KEYS:
            value = params.get(key)
            if value is not None:
                input_kwargs[key] = value          # 强制覆盖，模型传错的也纠正
        async for chunk in next_handler(**input_kwargs):
            yield chunk

cross_search_tool = FunctionTool(
    _cross_search_tool_impl,
    name="cross_search",                           # 对齐 deer-flow 检索工具语义（函数名必须为 ASCII）
    is_read_only=True,
    middlewares=[_SpacecodeOverrideMiddleware()],
)
```

**覆盖语义细节**：`value is not None` 才覆盖。这意味着**没有配置的 key 完全放行模型传参**（回退到 config.yaml 默认值），只有请求方显式给了值才纠正。

### 6.2 自定义提示词（middleware/custom_prompt.py）

**目的**：对齐 deer-flow 的 `custom_prompt`——前端可以随请求下发专属提示词，**整体覆盖** config.yaml 的 agent 级 system_prompt。

**实现**：AgentScope 提供 ``on_system_prompt`` transformer 钩子——框架每次模型调用前经 ``Agent._get_system_prompt`` 组装 system 提示词（config 提示词 + skill 指令 + workspace 指令拼接），然后**依次应用**实现了该钩子的中间件，返回值为最终提示词。custom_prompt 非空时直接返回它，即整体覆盖；未携带/空串则原样透传，零影响：

```python
class CustomPromptMiddleware(MiddlewareBase):
    async def on_system_prompt(self, agent, current_prompt: str) -> str:
        prompt = str(get_custom_params().get("custom_prompt") or "")
        if prompt:
            if prompt != current_prompt:
                logger.info(
                    "CustomPromptMiddleware: custom_prompt overrides "
                    "system prompt (was %d chars, now %d chars)",
                    len(current_prompt),
                    len(prompt),
                )
            return prompt
        return current_prompt
```

**覆盖语义要点**：

- transformer 模式天然幂等——ReAct 多轮迭代每轮都返回同一个 custom_prompt，无需去重；
- 整体覆盖范围包含 config 的 agent 级 system_prompt **及** skill/workspace 指令（deer-flow 等价语义）；
- 中间件经 ``is_implemented("on_system_prompt")`` 被 agent 构造时自动识别，无需额外注册。

**历史大坑（本实现踩过两次）**：

1. 早期用 ``on_reply`` 做“消息级注入”——但该钩子的 ``input_kwargs`` 仅含 ``inputs`` / ``structured_schema``（消息在 ``_reply_impl`` 内组装），``input_kwargs.get("messages")`` 永远拿不到东西，**注入从未生效**；``on_system_prompt`` 才是提示词覆盖的正确落点。
2. ``Msg.content`` 是 ContentBlock 列表而非字符串（该坑随 on_reply 方案废弃同步消失，但读消息文本时仍需从 block 提取 text）。

### 6.3 检索开关（enterprise.py + cross_search.py）

对齐 deer-flow 的三个开关（**注意默认值与判断方向的非对称性**）：

| 开关 | deer-flow 默认 | bocomadp 语义 | 生效点 |
|---|---|---|---|
| `vector_search_switch` | True | 显式 `False` → 不挂 vector_search 工具（cross_search 始终挂载） | `build_enterprise_tools` |
| `online_search_switch` | False | 显式 `True` → 挂 online_search 联网搜索（默认不挂） | `build_enterprise_tools` |
| `personal_search_switch` | False | 显式 `True` 且空间参数齐备 → 挂 personal_search 工具 | `build_enterprise_tools` |

```python
# enterprise.py（工具挂载开关，2026-08-20 起 cross_search 不受 vector 开关控制）
params = get_custom_params()
tools.append(cross_search_tool)              # cross_search 始终挂载

vector_switch = params.get("vector_search_switch")
if vector_switch is False:
    logger.info("vector_search disabled by vector_search_switch=false")
else:
    tools.append(vector_search_tool)          # 未传默认挂载（对齐 deer-flow 默认 True）

if params.get("online_search_switch") is True:
    tools.append(online_search_tool)          # 显式 True 才挂联网搜索

pks = (params.get("tools_param") or {}).get("personalKnowledgeSearch") or {}
if (
    params.get("personal_search_switch") is True
    and pks.get("psnlSpaceCodeId")
    and pks.get("psnlCategoryIdList")
):
    tools.append(personal_search_tool)        # 空间参数齐备才挂独立工具
```

为什么 personal 开关在**工具层**而不是参数层？2026-08-20 起 bocomadp 引入了独立的
personal_search 工具（行内搜索之外的"个人知识库搜索"维度），空间参数
（`psnlSpaceCodeId` / `psnlCategoryIdList`）来自 custom_params 的
``tools_param.personalKnowledgeSearch``，由
:class:`PersonalSpacecodeOverrideMiddleware` 强制覆盖模型传参；开关为 True
且空间参数齐备才挂载该工具。

### 6.4 认证参数（auth_context.py + 路由联动）

对齐 deer-flow 的 `_resolve_auth_params`，把 custom_params 中的认证字段解析为 `ResolvedAuth`，供工具后端读取（当前为预留消费点，解析与注入链路已就绪）：

```python
@dataclass
class ResolvedAuth:
    auth_mode: Literal["guwp-token", "jrt-auth-code", "okic-token", "muwp-user", "none"]
    guwp_token: str = ""
    jrt_auth_code: str = ""
    okic_token: str = ""
    okic_type: str = ""
    muwp_user: dict[str, Any] = field(default_factory=dict)

def resolve_auth_params(custom_params) -> ResolvedAuth:
    # 优先级：guwp-token > jrt-auth-code > okic-token > muwp-user > none
    # 任一方案凭据为空则跳过，全部缺失返回 none
```

**guwp 联动**：`guwp_token` 除了进入 `ResolvedAuth`，还同时 set 到 agent-factory 的 `_current_token` ContextVar——run 任务内 `_resolve_session_token` 读取它并持久化到 session token store，技能下载等工具直接可用。

## 7. 支持参数总表

| key | 类型 | 消费点 | 语义 |
|---|---|---|---|
| `space_code_list` | list[str] | 覆盖中间件 | 场景知识空间代码列表，强制覆盖 |
| `team_space_code_list` | list[str] | 覆盖中间件 | 团队知识空间代码列表，强制覆盖 |
| `psnl_space_code_id` | str | 覆盖中间件 | 个人知识空间 ID，强制覆盖 |
| `user_code` | str | 覆盖中间件 | 用户编码，强制覆盖 |
| `search_type` | str | 覆盖中间件 | 检索类型（0 混合 / 1 全文 / 2 向量） |
| `customized_tag_list` | list[str] | 覆盖中间件 | 自定义标签过滤，强制覆盖 |
| `psnl_category_id_list` | list[str] | 覆盖中间件 | 个人知识分类 ID，强制覆盖 |
| `custom_prompt` | str | CustomPromptMiddleware | 请求级自定义提示词（整体覆盖 system 提示词） |
| `vector_search_switch` | bool | build_enterprise_tools | 显式 False 卸载 vector_search（默认挂载；cross_search 不受控） |
| `online_search_switch` | bool | build_enterprise_tools | 显式 True 挂 online_search（默认不挂） |
| `personal_search_switch` | bool | build_enterprise_tools | 显式 True 且空间参数齐备 → 挂 personal_search |
| `tools_param.personalKnowledgeSearch` | dict | PersonalSpacecodeOverrideMiddleware | 个人空间参数（psnlSpaceCodeId / psnlCategoryIdList）强制覆盖 |
| `tools_param.source_param` | dict | vector_search 后端 | sourceType / repository / aggRepositories / HNSSParam |
| `guwp_token` / `jrt_auth_code` / `okic_token` / `okic_type` / `muwp_user` | str / dict | resolve_auth_params | 认证方案（优先级 guwp > jrt > okic > muwp） |

未列出的 key 会被保存（Redis）但**静默忽略**（无消费点）。

## 8. 教学实践：新增一个消费点（step-by-step）

以"新增 `max_results` 参数，限制检索返回条数"为例，演示完整接入流程：

**Step 1：消费点读取参数（唯一必需步骤）**

```python
# 在某个工具/中间件里
from bocomadp.deerflow.custom_params import get_custom_params

params = get_custom_params()
max_results = params.get("max_results")
if max_results is not None:
    ...  # 你的业务逻辑
```

**Step 2（可选）：如需强制覆盖模型传参**，把 key 加进 `_OVERRIDE_KEYS`：

```python
_OVERRIDE_KEYS = (..., "max_results")   # cross_search.py
```

**Step 3（可选）：如需在中间件/工厂装配时生效**，在对应工厂函数里读取（参见 6.3 的 `vector_search_switch` 模式）。

**Step 4（可选）：如需持久化语义**——已自动获得：带值请求自动写入 Redis、不带值请求自动回退，无需额外代码。

**Step 5：验证**。写最小验证脚本（模式见第 10 节）：set_custom_params → 触发消费点 → 断言行为 → reset。

**接入原则**：

1. 消费点**只读** `get_custom_params()`，绝不写入；
2. 判断方向对齐 deer-flow 默认值（`is False` / `is True`，不要用 `not params.get(...)` 一锅端）；
3. 覆盖/注入操作都要有 `logger.info` 日志（生产排障看 `SpacecodeOverride:` / `CustomPromptMiddleware:` 前缀）。

## 9. 常见坑清单

| 坑 | 现象 | 规避 |
|---|---|---|
| `Msg.content` 传字符串 | pydantic ValidationError: Input should be a valid list | 传 `[{"type": "text", "text": ...}]`；读取用 block 提取 |
| ContextVar 默认 dict 被写入 | 跨请求串台（污染共享默认值） | 消费点只读；写入只走 set |
| 在 `create_task` 之后才 set | run 任务读不到参数 | 必须在 `_spawn_run` **之前** set |
| 忘记 reset | 当前协程后续请求被污染 | set/reset 用 try/finally 成对出现 |
| Redis 不可用阻断对话 | 存储故障导致 run 失败 | save/load 一律 fail-open 非致命降级 |
| on_reply 里找 messages | 永远拿不到（input_kwargs 仅 inputs/structured_schema） | 提示词覆盖用 `on_system_prompt`（transformer 模式） |
| ReAct 多轮重复注入提示词 | 每轮 system 消息翻倍 | transformer 模式天然幂等，每轮返回同一 custom_prompt |
| `vector_search_switch` 用 `not` 判断 | 未传时误判为关闭 | 显式 `is False` 才卸载（默认挂载） |
| 在 `create_task` 之后 set 认证 | run 任务读不到 ResolvedAuth | 必须在 `_spawn_run` 之前 set |

## 10. 如何验证

**静态验证**：

```bash
cd agentscope && .venv/bin/python -m py_compile \
  examples/agent_service/bocomadp/deerflow/*.py \
  examples/agent_service/bocomadp/middleware/*.py \
  examples/agent_service/bocomadp/tools/*.py
```

**行为验证**（本轮已通过的 27 项断言，脚本模式）：

```python
# 1) 认证优先级
resolve_auth_params({"jrt_auth_code": "J", "okic_token": "O"}).auth_mode  # 'jrt-auth-code'

# 2) 提示词注入/去重（Msg 实例消息）
msg_objs = [Msg(name="user", role="user", content=[{"type": "text", "text": "hi"}])]
CustomPromptMiddleware._ensure_system_message(msg_objs, "PROMPT")   # True（插入）
CustomPromptMiddleware._ensure_system_message(msg_objs, "PROMPT")   # False（去重）

# 3) 中间件覆盖（set_custom_params 后调用 on_tool_call，断言 input_kwargs 被纠正）
# 4) personal_search_switch=true + 空间参数齐备 → build_enterprise_tools 含 personal_search
# 5) vector_search_switch=False → build_enterprise_tools 不含 vector_search（cross_search 仍含）
```

**端到端验证**（运行时）：启动 bocomadp 服务后，`POST /threads/{id}/runs/stream` 携带 custom_params，观察日志：

- `SpacecodeOverride: space_code_list ['WRONG'] -> ['S1']`（覆盖生效）
- `CustomPromptMiddleware: custom_prompt overrides system prompt (was N chars, now M chars)`（提示词整体覆盖）
- 再次请求不带 custom_params 时，覆盖日志仍出现（Redis 回退加载生效）

## 11. 与 deer-flow 的语义对照

| 能力 | deer-flow | bocomadp | 差异说明 |
|---|---|---|---|
| 空间码注入 | SpacecodeOverrideMiddleware 读落盘文件 | 工具中间件读 ContextVar | 数据源不同（文件 vs 内存），覆盖语义一致 |
| custom_prompt | 构建时整体替换 system_prompt | `on_system_prompt` 整体覆盖 | 语义一致（无差异） |
| 检索开关 | 构建时过滤工具列表 | 工具工厂挂载开关 | vector/online/personal 均以开关决定挂载 |
| 认证解析 | _resolve_auth_params | resolve_auth_params | 优先级、字段、降级逻辑逐一对齐 |
| 持久化 | threads/{thread_id}/custom_params.json | Redis key `bocomadp:session:{sid}:custom_params` | 文件 → Redis（TTL 4h），auth 同 key 同 TTL |
| 多实例隔离 | 每进程一份（文件/内存） | 多 worker 共享同一 Redis | 回退加载跨实例不 miss |

## 12. curl 验证手册（端到端）

### 12.0 前置：启动服务

```bash
cd /home/llm/zhangle/agentscope-workspace/agentscope/examples/agent_service
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 另开终端，健康检查（liveness）
curl -s http://localhost:8000/healthz
```

**接口约定**（写 curl 前先了解）：

- 路径：`POST /api/threads/{thread_id}/runs/stream`（SSE 流式）；`POST /api/threads/{thread_id}/runs/wait`（阻塞至完成）。
- **thread_id == session_id**（同一资源），首次请求自动建会话（`_prepare_session_for_run`），无需预先创建。
- 鉴权：`X-User-ID` 请求头**可选**，缺省 `"default"`（单租户本地部署）。
- `input` 兼容 LangGraph SDK 形态：`{"type": "human", "content": "..."}` 或 `{"messages": [...]}`。
- `custom_params` 放在请求体顶层，为任意 JSON 对象。
- **观察方式**：SSE 输出看 curl 终端；注入/覆盖日志看 **uvicorn 服务端终端**。

### 12.1 首次带 custom_params 请求（Redis 写入 + 提示词注入）

```bash
curl -N -X POST http://localhost:8000/api/threads/t-verify-1/runs/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
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

**服务端日志预期**：

```text
CustomPromptMiddleware: custom_prompt overrides system prompt (was N chars, now M chars)   ← 提示词整体覆盖
```

> `SpacecodeOverride:` 覆盖日志**仅在模型实际调用 cross_search（行内搜索）工具时出现**——先随便聊一轮确认服务连通，再用 12.3 的提问触发检索工具。

### 12.2 同一 thread 不带 custom_params（回退加载）

```bash
curl -N -X POST http://localhost:8000/api/threads/t-verify-1/runs/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "再介绍一下你自己"}
  }'
```

**预期**：请求体没有 custom_params，但 `CustomPromptMiddleware: custom_prompt overrides system prompt` 仍出现——证明参数从 Redis（按 session_id）回退加载成功。

### 12.3 触发检索工具验证空间码覆盖

提问方向明确指向知识检索（引导模型调用 cross_search 工具）：

```bash
curl -N -X POST http://localhost:8000/api/threads/t-verify-2/runs/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "请用 cross_search 工具检索“新员工入职流程”，并告诉我结果"},
    "custom_params": {
      "space_code_list": ["SP0000001"],
      "user_code": "U001"
    }
  }'
```

**服务端日志预期**（模型调用工具时逐条打印）：

```text
SpacecodeOverride: space_code_list [...] -> ['SP0000001']
SpacecodeOverride: user_code ... -> 'U001'
```

无论模型传什么值，都会被请求方指定的空间码纠正。

### 12.4 新值覆盖旧值（Redis 覆盖语义）

对**同一个 thread**（t-verify-2）换 `user_code` 再请求，然后回到 12.2 观察回退值：

```bash
curl -s -X POST http://localhost:8000/api/threads/t-verify-2/runs/wait \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "你好"},
    "custom_params": {"user_code": "U002", "space_code_list": ["SP0000002"]}
  }'
```

**预期**：Redis 记录被整体覆盖（HSET 更新，非合并）；此后不带 custom_params 的请求回退到的就是 `U002 / SP0000002`。

### 12.5 检索开关：vector_search_switch=false

```bash
curl -N -X POST http://localhost:8000/api/threads/t-verify-3/runs/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "请用 cross_search 工具检索“差旅报销流程”"},
    "custom_params": {"vector_search_switch": false}
  }'
```

**服务端日志预期**：

```text
enterprise tools: vector_search disabled by vector_search_switch=false (session=t-verify-3)
```

`vector_search_switch=false` 仅卸载行内搜索工具（vector_search）；cross_search 仍始终挂载。

### 12.6 检索开关：personal_search_switch=true + 空间参数齐备

```bash
curl -N -X POST http://localhost:8000/api/threads/t-verify-4/runs/stream \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: tester' \
  -d '{
    "assistant_id": "lead_agent",
    "input": {"type": "human", "content": "请在个人知识库中检索“组织架构”"},
    "custom_params": {
      "personal_search_switch": true,
      "tools_param": {
        "personalKnowledgeSearch": {
          "psnlSpaceCodeId": "PSNL-XYZ",
          "psnlCategoryIdList": ["CATE1"]
        }
      }
    }
  }'
```

**预期**：`personal_search_switch` 显式 `True` 且空间参数齐备 → 挂载 personal_search
工具；模型调用时由 `PersonalSpacecodeOverrideMiddleware` 强制覆盖空间参数（日志前缀
`PersonalSpacecodeOverride:`）。

> 仅开关为 True 但空间参数缺失（无 psnlSpaceCodeId 或 psnlCategoryIdList）时不挂载
> 该工具（见 `build_enterprise_tools`）。

### 12.7 查看 Redis 存储（持久化证据）

```bash
# 连接 AppConfig.redis 对应实例（默认 localhost:6379）
redis-cli keys 'bocomadp:session:*'
redis-cli hgetall 'bocomadp:session:t-verify-2:custom_params'
# 预期 params 字段为 12.4 覆盖后的值：
# {"user_code": "U002", "space_code_list": ["SP0000002"]}
redis-cli ttl 'bocomadp:session:t-verify-2:custom_params'   # 剩余 TTL（<4h）
```

> key 语义：`bocomadp:session:{session_id}:custom_params`，hash 字段 `params`（custom_params）
> 与 `auth`（ResolvedAuth）同 key 同 TTL（4h 原生过期）。Redis 不可用时 save/load 均
> fail-open 降级，不阻断 run。

### 12.8 验证 checklist

| # | 场景 | curl | 通过标准 |
|---|---|---|---|
| 1 | 带 params 首次请求 | 12.1 | `custom_prompt overrides system prompt` 日志出现 |
| 2 | 不带 params 回退 | 12.2 | `custom_prompt overrides system prompt` 仍出现（Redis 回退） |
| 3 | 空间码覆盖 | 12.3 | 日志 `SpacecodeOverride: space_code_list ... -> ['SP0000001']` |
| 4 | Redis 覆盖语义 | 12.4 | Redis `params` 字段变为 U002 / SP0000002 |
| 5 | 行内检索开关 | 12.5 | 日志 `vector_search disabled by vector_search_switch=false` |
| 6 | 个人检索挂载 | 12.6 | `personal_search_switch=true` + 空间参数齐备 → 挂 personal_search |
| 7 | Redis 持久化 | 12.7 | `redis-cli hgetall` 取到记录且 TTL<4h |
