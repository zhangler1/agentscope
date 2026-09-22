# bocom-agentscope 智能体开发指引

> **读者对象**：基于 `bocom-agentscope`（行内能力扩展包，当前含行内模型平台
> 对接）开发智能体的研发人员。
>
> **阅读方式**：**(3)~(8) 讲核心模块的源码开发方式**（直接实例化 SDK
> 对象组装智能体），**(9) 单独讲起 Service**（服务化部署）。两种模式共用
> 同一套模块，区别只在于"对象怎么创建、谁来驱动"。
>
> **参考文档**：AgentScope 2.0 官方教程（`https://doc.agentscope.io/`
> Tutorial 下的 *Agent*、*Model*、*Tool*、*Agent Skill*、*Middleware*、
> *Long-Term Memory*、*MCP*、*State/Session Management* 章节）。
> 本文只讲"行内差异 + 落地用法"，官方能力不重复展开。
>
> **可运行示例**：源码模式见 `examples/sdk_demo_memory.py` /
> `examples/sdk_demo_redis.py`；服务模式见 `bocom-starter/main_memory.py` /
> `bocom-starter/main_redis.py`。本文 (8)(9) 即按这些文件讲解。

---

## (1) 总体架构

### 1.1 bocom-agentscope 的定位

`bocom-agentscope` 是行内对 AgentScope 2.0 的扩展包，定位为**行内能力的对接层**：
当前只提供行内模型平台（ELLM）对接（凭证、模型、api_key 刷新、配置），
后续可按同样方式扩展其他行内能力对接（行内工具、行内记忆等）；
AgentScope 原生能力（技能、工具、记忆、多智能体、服务化）开箱即用，
不在本包重复实现。

一句话：**bocom-agentscope 负责"行内能力对接"（当前为模型平台），业务代码负责
"把行内能力 + 原生能力组装成智能体"，bocom-starter 则提供上述两种开发
模式的快速开发代码示例（源码开发见 (8)、服务化开发见 (9)）。**

### 1.2 两种开发模式

| | 源码开发（(3)~(8)） | 服务化开发（(9)） |
|---|---|---|
| 驱动方式 | 业务代码 `await agent.reply(...)` | HTTP API（`POST /chat/` + SSE） |
| 对象创建 | 直接 `ELLMCredential(...)`、`EllmChatModel(...)` | `create_app` 按会话配置动态构建 |
| 中间件 | 直接挂载（`Agent` / `FunctionTool` 的 `middlewares`） | `AgentMiddlewareFactory` 按会话实例化 |
| key 刷新 | 配置 `EllmKeyRefreshMiddleware`后全自动| `EllmKeyRefreshMiddleware` 全自动 |
| 适用场景 | 脚本、批处理、嵌入既有系统、单元测试 | 生产服务、多租户、前端对接 |

> 两种模式的**模型一致**：都用 `ELLMCredential` + `EllmChatModel` +
> `EllmKeyRefreshMiddleware`（key 惰性刷新 + 401 强刷重试一次）。因此两种模式
> 都需要主存储与 message_bus——中间件要按 `credential_id` 读写凭证记录来缓存
> `api_key` / `apikey_expires_at`；差别只在"对象谁来创建、中间件谁来挂"。

### 1.3 一次对话的完整链路（源码视角）

```
await agent.reply(Msg("user", ...))
  └─ Agent 推理循环（reasoning-acting）
       ├─ 模型调用 EllmChatModel.__call__ → _call_api
       │    ├─ formatter.format(messages)          # 默认 DeepSeekChatFormatter
       │    ├─ 请求 ELLM 网关 /chat/completions（OpenAI 兼容）
       │    │    ├─ 参数下发：max_tokens / temperature / top_p /
       │    │    │   enable_thinking(extra_body.chat_template_kwargs) / reasoning_effort
       │    │    ├─ 401 invalid_api_key → 需注入回调：强刷 key 并重试一次
       │    │    └─ 流式解析：reasoning_content → thinking 块；
       │    │        inject_think_tag=True 时首个非空文本增量前加 <think>
       │    └─ 返回 ChatResponse（含 usage）
       ├─ 工具调用：Toolkit 按 ToolCallBlock 分发（洋葱式工具中间件可拦截）
       └─ 技能提示词已自动拼进 system prompt（get_skill_instructions）
```

服务模式下，这条链路整体包在 FastAPI 里；Agent 级中间件工厂在每轮 run 前把
`EllmKeyRefreshMiddleware` 实例装到模型调用链上（见 (9.3)）。源码模式下则是你
自己把它挂到 `Agent(middlewares=[...])`（见 (8)）——两种模式都以它为前提。

---

## (2) 环境准备与安装

### 2.1 依赖

- Python ≥ 3.12、pip；
- **主存储：两种模式的默认形态都必需**，
  - 源码开发默认（`ELLMCredential` + `EllmChatModel` +
    `EllmKeyRefreshMiddleware`，见 (8)）**必需**：`EllmKeyRefresher` 要按
    `credential_id` 读写凭证记录来缓存 `api_key` / `apikey_expires_at`，
    凭证首次落库也走它；
  - 服务化（`create_app`，见 (9)）**必填**：会话/凭证/agent 等业务数据全部落库；
  实现二选一：`AsyncSQLAlchemyStorage`（`sqlalchemy[asyncio]` + `aiomysql`，
  MySQL / OceanBase MySQL 模式；库需先建好，`create_tables=True` 会自动建表；
- Redis：**仅 `main_redis.py` 的 `RedisMessageBus` 需要**（收件箱 / 唤醒 /
  分布式锁等瞬时协调，非业务存储），连接参数 `REDIS_HOST` / `REDIS_PORT` /
  `REDIS_DB` / `REDIS_PASSWORD`；`main_memory.py` 用
  `InMemoryMessageBus`，单进程零外部依赖；
- 行内 pip 源（行内无外网时用 `requirements.txt` 头部的镜像源）。

### 2.2 安装

```bash
# 从行内依赖库直接安装（依赖自动解析；发行版包名 bocom-agentscope）
pip install bocom-agentscope
```
---

## (3) 模型模块

### 3.1 ELLM 凭证：ELLMCredential

自研 ELLM 平台对外暴露 OpenAI 兼容端点（`/v1`）。凭证
`type="bocom_ellm_credential"`，**导入 `providers.credential` 即完成
`CredentialFactory` 注册**（内部做幂等检查，与同环境已有注册共存）。
直接实例化：

```python
from providers.credential import ELLMCredential  # 导入即注册 CredentialFactory

credential = ELLMCredential(
    base_url="http://ellm-gateway.example/v1",   # OpenAI 兼容端点（以 /v1 结尾）
    scene_code="P2024146",            # 场景编码（api_key 自动刷新必填，业务字段）
    api_key_url=(                     # key 申请地址（createSceneApiKey.do 端点）
        "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do"
    ),
    # api_key 省略即默认占位值 sk-xxx（挂 key 刷新中间件时由刷新器注入真实
    # key；不挂中间件的源码直连必须自己传真实 key）
    # organization / apikey_expires_at 均可选
)
```

> 凭证**不绑定模型**：运行时模型名来自会话 `chat_model_config.model`
> （见 (9.6)(c)）；`<think>` 注入由模型构造参数或模型卡
> `inject_think_tag` 控制（见 (3.6)）。

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 否 | 固定 `bocom_ellm_credential`（Pydantic discriminator） |
| `api_key` | 否 | `SecretStr`，省略时默认占位值 `sk-xxx`；运行时由刷新器注入真实 key |
| `base_url` | ✅ | OpenAI 兼容端点（**以 `/v1` 结尾**） |
| `scene_code` | ✅ | 场景编码（业务字段；**api_key 自动刷新的必填参数**，向网关 `createSceneApiKey.do` 申请新 key） |
| `api_key_url` | ✅ | key 申请地址（`createSceneApiKey.do` 端点） |
| `organization` | 否 | 组织 ID（官方 `OpenAIChatModel` 构造会读取该字段，故保留） |
| `apikey_expires_at` | 否 | key 过期时间戳；不填视为已过期（仅 `EllmKeyRefresher` 读写，源码开发模式无需） |

### 3.2 ELLM 模型实现：EllmChatModel

`EllmChatModel` 继承 `ChatModelBase`，适配 ELLM 网关的两个协议差异：

- **流式响应不带 `<think>` 包裹**：`inject_think_tag` 开启时，首个非空
  文本增量前注入 `<think>` 前缀（空增量跳过）；
- **生成长度上限必须用 `max_tokens` 字段名**（适配器拒绝
  `max_completion_tokens`）。

直接实例化：

```python
from providers.ellm_chat_model import EllmChatModel

model = EllmChatModel(
    credential=credential,                       # 上面的 ELLMCredential
    model="deepseek-flash",                      # 模型名（须能匹配模型卡，见 (3.3)）
    # context_size 可省（前提：已 set_models_dir，见 (3.3)）：省略时按模型名
    # 从模型卡解析，无匹配卡回落 32768
    parameters=EllmChatModel.Parameters(
        max_tokens=4096,       # 上限由模型卡片 output_size 决定
        temperature=0.7,
        top_p=None,
        enable_thinking=None,  # 经 extra_body.chat_template_kwargs.enable_thinking 下发
        reasoning_effort=None, # low/medium/high，顶层 reasoning_effort 字段
    ),
    stream=True,               # 默认流式
    max_retries=3,             # ELLM API 重试
    retry_delay=1.0,
    inject_think_tag=False,    # <think> 注入开关（构造参数，见 (3.6)）
    # formatter=None 时默认 DeepSeekChatFormatter
    # client_kwargs 转发给 openai.AsyncClient（timeout/default_headers 等）
)
```

生成参数（`Parameters`，均可选、`None` 表示不下发该字段）：

| 参数 | 说明 |
|---|---|
| `max_tokens` | 最大输出 token（上限由模型卡片 `output_size` 决定） |
| `temperature` / `top_p` | 采样参数 |
| `enable_thinking` | 开启思考模式，经 `extra_body.chat_template_kwargs.enable_thinking` 下发 |
| `reasoning_effort` | 推理强度提示（low/medium/high），顶层 `reasoning_effort` 字段 |

> 服务模式下这些参数来自会话 `chat_model_config.parameters`（原样透传给
> `Parameters(**...)`，见 `agentscope/app/_service/_model.py`）。

### 3.3 模型候选：list_models 与模型卡

候选模型的**唯一来源是模型卡目录**：

- 目录由**启动程序在起服务前一次性指定**：
  `EllmChatModel.set_models_dir(<dir>)`（`bocom-starter` 指向同目录 `models/`）；
- 未配置目录时 `list_models` / 模型构造会**直接抛错**（配置错误必须启动期
  暴露，而不是静默返回空候选）；
- `EllmChatModel.list_models(custom_yaml_dir=...)` 可临时覆盖目录（优先级
  最高，适合测试）；
- 目录不存在 → `FileNotFoundError`；模型名找不到对应卡 → `context_size` 回落
  `_DEFAULT_CONTEXT_SIZE = 32768` 并打 warning（`inject_think_tag` 同样回落
  `False`，见 (3.6)）。

卡片示例（`bocom-starter/models/deepseek-flash.yaml`，当前仓库仅此一张）：

```yaml
name: deepseek-flash
label: DeepSeek Flash
status: active
context_size: 100000     # 供上下文压缩（构造模型时未传 context_size 时取此值）
output_size: 38400       # 决定 max_tokens 上限
# inject_think_tag: true # 本包扩展键：控制 <think> 注入（见 (3.6)），省略即 False
```

卡片字段与官方 `agentscope.model.ModelCard` 一致（`name` / `label` /
`status` / `input_types` / `output_types` / `context_size` / `output_size` /
`parameter_schema` / `parameters_overrides`），亦兼容官方 SDK
`agentscope/model/_deepseek/_models/` 下的 DeepSeek 模型卡。

> 本包额外识别一个扩展键 `inject_think_tag`（构造模型时未显式传参时的
> `<think>` 注入开关，见 (3.6)）。它不在官方 `ModelCard` 字段里，因此：
> 官方 `ModelCard.from_yaml` 会忽略它（不报错），本包直接读原始 yaml 取值，
> 前端 `GET /model/` 返回的卡片也不包含它。

```python
cards = EllmChatModel.list_models(custom_yaml_dir="/path/to/my/models")
# 返回 ModelCard 列表
```

- **纯源码模式不配模型卡目录时**：构造 `EllmChatModel` 必须显式传
  `context_size`（省略就会走卡目录解析、直接抛 `RuntimeError`，见 (8.1)）；
  `inject_think_tag` 不受此限——同样情形下它回落 `False` 而不抛（见 (3.6)）；
- 服务模式下前端经官方 `GET /model/?provider=bocom_ellm_credential` 查询：
  该路由取凭证类 → `get_chat_model_class().list_models()`，因此读到的就是
  `set_models_dir` 指定的同一批卡片（候选列表与运行时 `context_size`
  同源，不会不一致）。**新增模型 = 往 `models/` 放一张卡片 yaml。**

### 3.4 直接调用模型

```python
import asyncio

from agentscope.message import Msg, TextBlock

async def main() -> None:
    reply = await model([
        Msg(name="user", role="user", content=[TextBlock(text="你好，介绍一下你自己")]),
    ])
    # 流式：返回增量序列，逐个消费（is_last=True 收尾）
    text = ""
    async for chunk in reply:
        for block in chunk.content:
            if isinstance(block, TextBlock):
                text += block.text
    print(text)
    await model.aclose()   # 释放 openai client 连接池（可选，但推荐）

asyncio.run(main())
```

- 返回 `ChatResponse`（流式为增量序列，`is_last=True` 收尾）；
- `usage` 携带 input/output token 与耗时；`reasoning_content` 解析为
  thinking 块。

### 3.5 api_key 生命周期

网关签发 key 有效期约 25 分钟。两个原语：

**纯函数取新 key**（无状态、无锁，网络错误原样抛出由调用方降级）：

```python
from providers.ellm_key import fetch_ellm_key

key, ttl_ms = fetch_ellm_key(
    scene_code="P2024146",
    api_key_url="http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do",
    # timeout=30  # 默认 30s
)
# ttl_ms 归一化为"从现在起剩余毫秒数"（网关返回 TTL 时长或绝对毫秒时间戳均可）
model.set_api_key(key)   # 注入后续请求的 Authorization: Bearer <key>
```

**带状态的刷新器 `EllmKeyRefresher`**（按 `credential_id` 惰性刷新存储
里的凭证 key，需要 `StorageBase` + `MessageBus`）：

```python
from providers.ellm_key import EllmKeyRefresher

refresher = EllmKeyRefresher(
    storage,           # 凭证读写后端
    message_bus,       # 分布式锁传输（acquire_lock）
    user_id="test-user",
    refresh_ahead_secs=300.0,  # 提前刷新窗口（默认 300.0，即过期前 5 分钟刷）
)
key, record = await refresher.ensure_fresh_key(credential_id)   # 惰性刷新
new_key = await refresher.force_refresh_key(credential_id)      # 401 强制刷新
await refresher.invalidate_key(credential_id)                   # 标记过期
```

- `ensure_fresh_key`：快路径（未过期直接复用存储 key，无锁无网络）→
  慢路径（`ellm:refresh:{id}` 锁，锁 TTL 30s，锁内二次校验新鲜度，
  并发只打网关一次）；
- `_is_expired`：`api_key` 为空 → 立即过期；`apikey_expires_at` 有效时
  `now > apikey_expires_at - refresh_ahead_secs` 即过期；无有效时间戳
  → 视为已过期（刷新后自动写回，记录自动收敛）；
- `force_refresh_key`：跳过本地过期判断强取新 key；网关失败沿用旧 key；
- `invalidate_key`：清掉 `apikey_expires_at`，下次调用走惰性刷新；
- 写回凭证时落到凭证**真实 owner** 名下（跨用户共享场景）。

### 3.6 think-tag 注入开关

`<think>` 注入由 **`EllmChatModel` 的 `inject_think_tag`** 决定。取值优先级
为 **构造参数 > 模型卡 `inject_think_tag` > `False`**；构造后也可直接改属性：

```python
model = EllmChatModel(..., inject_think_tag=True)   # 显式传参，优先级最高
model.inject_think_tag = True                       # 等价：构造后改属性
```

- 开启后：流式响应首个非空文本增量前注入 `<think>` 前缀（空增量跳过）；
- 是否收到 `reasoning_content`（thinking 块）由网关/模型自身行为决定，
  与该开关无关；
- **构造参数默认 `None`（未传）时的回落顺序**：
  1. 卡目录已配置（`set_models_dir`）且 `model` 命中某张卡 → 取该卡的
     `inject_think_tag`，缺该键则为 `False`；
  2. 卡目录未配置（纯源码直连没调 `set_models_dir`）→ `False`，
     **不报错**（`context_size` 在同样情形下会抛 `RuntimeError`，见 (3.3)）；
- 服务模式下 `create_app` 的模型构建走
  `agentscope/app/_service/_model.py::get_model`，只传
  `credential / model / parameters`（该参数必为 `None`）→ **自动读取模型卡**，
  即**改卡片即可切换开关，下一轮对话生效**（模型每轮 run 重建）；
- 该键**不在官方 `ModelCard` 字段白名单内**，本包直接读卡片原始 yaml 取值，
  因此 `GET /model/` 返回的卡片不含它（前端不可见，也不需要）；
- 本包**不再有** Redis 模型表（`bocomadp:model:think_tag`）或会话级
  think-tag 覆盖路由；`EllmKeyRefreshMiddleware` 也只管 key，不碰该开关。

---

## (4) 技能模块

技能是 AgentScope 原生能力（Anthropic 提出的 Agent Skill 规范），
bocom-agentscope 不重复实现，直接使用官方 API。

### 4.1 SKILL.md 规范

技能目录必须包含 `SKILL.md`（YAML frontmatter + 指令正文）：

```
sample_skill/
└── SKILL.md
```

```markdown
---
name: sample_skill
description: A sample agent skill for demonstration.
---

# Sample Skill
...
```

### 4.2 注册与使用（官方 API）

```python
from agentscope.tool import Toolkit

toolkit = Toolkit(skills_or_loaders=["sample_skill"])   # 注册技能目录（构造参数）
prompt = await toolkit.get_skill_instructions()         # 生成技能提示词（异步）

# 输出形如：
# <agent-skills>
# Skills are a collection of instructions, scripts, and resources...
# <skill>
# <name>sample_skill</name>
# <description>A sample agent skill for demonstration.</description>
# <dir>sample_skill</dir>
# </skill>
# </agent-skills>
```

- `Toolkit` 构造参数：`tools` / `skills_or_loaders` / `mcps` / `tool_groups` /
  `meta_tool_response_template` / `skill_instruction_template`（**无
  `middlewares` 参数**，工具级中间件挂在工具对象上，见 (5.2)）；
- 技能经 `skills_or_loaders` 注册（目录字符串、`Skill`、`SkillLoaderBase`
  均可）；
- Toolkit 内置技能查看工具（`SkillViewer`，工具名 `Skill`），agent 用它读取
  `SKILL.md` 正文（技能本身不可直接调用）。

### 4.3 与 Agent 集成

`Agent` 会把技能提示词**自动拼进 system prompt**（`get_skill_instructions`）：

```python
from agentscope.agent import Agent

agent = Agent(
    name="ellm-assistant",
    system_prompt="You are a helpful assistant.",
    model=model,                       # (3) 中的 EllmChatModel
    toolkit=toolkit,
)
# agent 组装时技能提示词已附进 system prompt
```

### 4.4 开发一个技能的最小步骤

1. 建目录 `<skill-name>/`，写 `SKILL.md`（frontmatter 必须含 `name`、
   `description`）；
2. 附上脚本/资源（`scripts/`、`references/` 等，按需）；
3. `Toolkit(skills_or_loaders=["<skill-name>"])` 注册；
4. 验证技能提示词已进 system prompt（agent 经内置技能查看工具读正文）。

> 服务模式下技能由工作区加载器按同一 SKILL.md 规范加载：给
> `LocalWorkspaceManager(skill_paths=[...])` 传技能目录即可
> （每个工作区实例都带上该路径）；也可用 `create_app(skill_hubs=...)`
> 挂技能源（本 fork 保留该参数，见 (9.2)）。底层机制与本节完全一致。

---

## (5) 中间件模块

### 5.1 体系概览

AgentScope 2.0 的中间件体系分两级：

| 级别 | API | 挂载点 |
|---|---|---|
| 工具级 | `agentscope.tool.ToolMiddlewareBase` | 工具调用前/后（洋葱模型），`FunctionTool(func, middlewares=[...])` 挂载 |
| Agent 级 | `agentscope.middleware.MiddlewareBase` | 推理循环钩子（`on_reply` / `on_reasoning` / `on_acting` / `on_model_call` / `on_system_prompt` / `on_compress_context` / `on_check_permission`），`Agent(middlewares=[...])` 挂载 |

- 洋葱模型：前置处理按注册顺序、后置处理逆序执行；
- 中间件可改输入/输出、短路跳过、追加逻辑，职责单一互不干扰。

### 5.2 工具级中间件（官方 API）

签名：`async def on_tool_call(self, tool, input_kwargs, next_handler) ->
AsyncGenerator[ToolChunk, None]`（继承 `ToolMiddlewareBase`；流式/非流式
统一——`next_handler` 恒为 async generator）：

```python
from agentscope.tool import FunctionTool, ToolMiddlewareBase, ToolChunk
from agentscope.message import TextBlock

class AuditMiddleware(ToolMiddlewareBase):
    """工具调用前审计 + 未授权短路。"""

    async def on_tool_call(self, tool, input_kwargs, next_handler):
        if tool.name not in {"search_tool"}:
            yield ToolChunk(content=[
                TextBlock(text=f"Error: Tool '{tool.name}' is not authorized"),
            ])
            return                     # 不调用 next_handler = 短路跳过
        async for chunk in next_handler(**input_kwargs):
            yield chunk                # 正常透传

tool = FunctionTool(search_tool, middlewares=[AuditMiddleware()])
toolkit = Toolkit(tools=[tool])
```

典型用途：审计日志、权限控制、限流、缓存、错误重试、输入校验、指标采集。

### 5.3 Agent 级中间件：bocom-agentscope 的 ELLM key 刷新中间件

`EllmKeyRefreshMiddleware` 继承 `MiddlewareBase`，实现 `on_model_call`
钩子（签名 `(agent, input_kwargs, next_handler)`），**每次模型调用前**：

```python
class EllmKeyRefreshMiddleware(MiddlewareBase):
    def __init__(self, storage, message_bus, user_id,
                 refresh_ahead_secs=300.0):
        self._refresher = EllmKeyRefresher(          # (3.5) 的带状态刷新器
            storage, message_bus, user_id,
            refresh_ahead_secs=refresh_ahead_secs,
        )

    async def on_model_call(self, agent, input_kwargs, next_handler):
        current_model = input_kwargs.get("current_model")
        if isinstance(current_model, EllmChatModel):  # 非 ELLM 模型透传
            credential_id = current_model.credential.id
            key, _ = await self._refresher.ensure_fresh_key(credential_id)  # 惰性刷新
            current_model.set_api_key(key)            # 注入请求头，不重建 client
            # 401 回调（闭包绑定本次 credential_id，防并发串号）
            current_model.set_refresh_key_callback(
                lambda: self._refresher.force_refresh_key(credential_id),
            )
            current_model.set_auth_invalidate_callback(
                lambda: self._refresher.invalidate_key(credential_id),
            )
        return await next_handler(**input_kwargs)
```

> 本中间件**只负责 key**：不设置 `inject_think_tag`、不查询任何 Redis
> 配置（`<think>` 开关见 (3.6)）；构造参数也没有 `session_id`。

配合 `EllmChatModel` 的 401 处理：网关返回 `invalid_api_key` 401 时调用
注入的刷新回调强制取新 key 并重试当前调用一次；强制刷新失败回调把凭证
标记过期，下次调用走惰性刷新恢复。日志关键字：
`injected refreshed ELLM key`、`EllmKeyRefresher: key refresh failed`。

### 5.4 自定义 Agent 级中间件

```python
from agentscope.middleware import MiddlewareBase

class AuditMiddleware(MiddlewareBase):
    """每次模型调用前记录审计日志（参考 EllmKeyRefreshMiddleware 模式）。"""

    def __init__(self, user_id: str, session_id: str) -> None:
        self._user_id = user_id
        self._session_id = session_id

    async def on_model_call(self, agent, input_kwargs, next_handler):
        current_model = input_kwargs.get("current_model")
        logger.info(
            "audit: model call user=%s session=%s model=%s",
            self._user_id, self._session_id,
            getattr(current_model, "model", "?"),
        )
        return await next_handler(**input_kwargs)   # 继续链路（可拦截短路）
```

> 服务模式下 Agent 级中间件经 `create_app(extra_agent_middlewares=...)`
> 的**工厂**在**每轮 run** 时按 `(user_id, agent_id, session_id)` 新建并
> 挂载（见 (9.3)）；源码模式自己挂到
> `Agent(middlewares=[...])`。

---

## (6) 工具模块

### 6.1 自定义工具

```python
from agentscope.tool import FunctionTool, Toolkit

async def search_tool(query: str) -> str:
    """A simple search tool.

    Args:
        query (`str`):
            The search query.

    Returns:
        `str`:
            The search result.
    """
    return f"Search results for '{query}'"

toolkit = Toolkit(tools=[FunctionTool(search_tool)])
```

- 工具函数直接返回 `str`（或 `ToolChunk`），框架自动包装；函数
  `docstring`（含 Args/Returns）会生成工具说明，务必写清；
- 内置工具（Bash、Grep、Read 等）与业务自定义工具统一经 `Toolkit` 注入
  agent；工具执行可被 (5.2) 的工具级中间件拦截；
- `FunctionTool` 可选参数：`name` / `description` / `is_concurrency_safe` /
  `is_read_only` / `is_state_injected` / `middlewares`。

### 6.2 MCP 接入

```python
import os

from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig

stdio_client = MCPClient(
    name="browser-use",
    mcp_config=StdioMCPConfig(command="npx", args=["@playwright/mcp@latest"]),
    is_stateful=True,     # 有状态 MCP：随会话保持
)
http_client = MCPClient(
    name="amap",
    mcp_config=HttpMCPConfig(
        url=f"https://mcp.amap.com/mcp?key={os.environ['AMAP_API_KEY']}",
    ),
    is_stateful=False,
)
```

MCP 工具经 `Toolkit(mcps=[...])` 注册后与普通工具同权使用（官方 *MCP*
教程）；服务模式下通过 `LocalWorkspaceManager(default_mcps=...)` 给每个
工作区注入默认 MCP（见 (9.2)），也可用 `create_app(mcp_hubs=...)` 挂 MCP 源。

---

## (7) 记忆模块

### 7.1 会话记忆

AgentScope 2.0 无独立 memory 模块：会话消息**内置在 Agent 的 state 中**
（`agentscope.state.AgentState`，`Agent(..., state=...)` 可注入自定义
状态），长对话经 `ContextConfig` 触发自动上下文压缩：

```python
from agentscope.agent import Agent, ContextConfig

agent = Agent(
    name="ellm-assistant",
    system_prompt="You are a helpful assistant.",
    model=model,
    context_config=ContextConfig(
        trigger_ratio=0.8,      # 上下文占用超 80% 触发压缩（默认 0.8）
        reserve_ratio=0.1,      # 压缩后保留 10% 空间（默认 0.1）
        # tool_result_limit / compression_prompt / summary_* 亦可调
    ),
)
```

### 7.2 长程记忆

`AgenticMemoryMiddleware` 把长期记忆落为工作区内的 Markdown 文件（随
workspace 后端持久化），构造参数为关键字参数：

```python
from agentscope.middleware import AgenticMemoryMiddleware

memory_mw = AgenticMemoryMiddleware(
    workdir=workspace.workdir,          # 必填：工作区目录
    memory_dir="Memory",                # 记忆文件目录（默认 Memory）
    backend=workspace.get_backend(),    # 执行后端（本地/Docker 等，默认本地）
)
```

配合 `PER_AGENT` 工作区隔离，记忆可跨会话存活（官方 *Long-Term Memory*
教程）。除 `AgenticMemoryMiddleware` 外，本 fork 还提供
`Mem0Middleware` / `ReMeMiddleware`；知识库/RAG 能力见官方 *RAG* 教程。

---

## (8) 端到端源码开发示例

把 (3)~(7) 组装成一个可跑的智能体（可对照 `examples/sdk_demo_memory.py`）：

```python
# -*- coding: utf-8 -*-
"""bocom-agentscope 源码开发最小示例：ELLM 模型 + 技能 + 工具 + 中间件。"""
import asyncio
import logging
import os

from agentscope.agent import Agent
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import Msg, TextBlock
from agentscope.tool import FunctionTool, Toolkit

from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import EllmKeyRefreshMiddleware

logging.basicConfig(level=logging.INFO)

MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"


async def search_tool(query: str) -> str:   # 见 (6.1)
    """A simple search tool."""
    return f"Search results for '{query}'"


async def main() -> None:
    # 1) 存储 + 消息总线（key 刷新中间件的依赖，见 (3.5)/(5.3)）
    #    主存储是 SQL（MySQL/OB）；Redis 只在多进程消息总线时才需要。
    #    本步只在挂了 key 刷新中间件时必需；不挂中间件的纯源码直连可整块删除，
    #    改用真实 api_key 直接调模型（见 (8.1)）。
    #    存储必须 `async with` 进入（懒建连接池）
    async with AsyncSQLAlchemyStorage(
        MYSQL_URL,
        create_tables=True,             # 示例用；生产置 False 走 alembic
        engine_kwargs={"pool_pre_ping": True, "pool_recycle": 3600},
    ) as storage:
        message_bus = InMemoryMessageBus()   # 多进程部署换 RedisMessageBus

        # 2) 行内凭证（先落库：中间件按 credential_id 读它刷新 key）+ 模型
        credential = ELLMCredential(
            base_url="http://ellm-gateway.example/v1",
            scene_code="P2024146",
            api_key_url="http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do",
        )
        await storage.upsert_credential("test-user", credential)
        model = EllmChatModel(
            credential=credential,
            model="deepseek-flash",
            context_size=100_000,     # 可省：省略则按模型卡解析（见 (3.3)）
            parameters=EllmChatModel.Parameters(max_tokens=4096, temperature=0.7),
        )

        # 3) 工具 + 技能（构造参数注册，见 (4.2)/(5.2)/(6.1)）
        toolkit = Toolkit(
            tools=[FunctionTool(search_tool)],
            skills_or_loaders=["sample_skill"],
        )

        # 4) 组装智能体（挂 key 刷新中间件：每次模型调用前自动刷新/注入
        #    key，见 (5.3)；会话记忆内置在 Agent state，见 (7.1)）
        agent = Agent(
            name="ellm-assistant",
            system_prompt="You are a helpful assistant.",
            model=model,
            toolkit=toolkit,
            middlewares=[
                EllmKeyRefreshMiddleware(
                    storage, message_bus, user_id="test-user",
                ),
            ],
        )

        try:
            # 5) 驱动对话
            reply = await agent.reply(
                Msg(name="user", role="user",
                    content=[TextBlock(text="你好，介绍一下你自己")]),
            )
            print(reply.get_text_content())
        finally:
            await model.aclose()      # 释放 openai client 连接池


if __name__ == "__main__":
    asyncio.run(main())
```

要点回顾：

- bocom-agentscope 只出现在**第 1、2、4 步**（凭证/模型/key 刷新中间件），
  其余全是原生 API；
- **默认形态（推荐）**：源码模式同样挂 `EllmKeyRefreshMiddleware`（第 4 步），
  所以第 1 步的 storage + message_bus 都需要——storage 供中间件按
  `credential_id` 读写凭证记录，message_bus 供其取并发锁；这与 (9) 的服务化
  形态一致，只是对象创建与中间件挂载都由你自己做；
- **降级形态**：删掉第 1 步整块（storage + message_bus）与第 4 步的中间件，
  `ELLMCredential` 必须传真实 `api_key`，key 改用 `fetch_ellm_key` +
  `set_api_key` 手动兜（见 (3.5)/(8.1)）——省掉存储，代价是没有 401 自愈；
- 技能需先把 `sample_skill/` 目录（含 SKILL.md）放在工作目录或传入绝对
  路径，否则 `Toolkit` 注册会失败；
- 服务化时第 1~4 步的对象由 `create_app` 在**每轮 run** 时按会话配置动态
  构建、中间件经工厂新建装配，业务代码形态不变（见 (9)）。

---

## (9) 起 Service（服务化部署）

源码模式下组装好的能力，全部经 `agentscope.app.create_app` 装配成一个
FastAPI 服务。`bocom-starter/` 提供**两个入口示例**，除消息总线外完全相同：

| 入口 | 消息总线 | 适用 |
|---|---|---|
| `main_memory.py` | `InMemoryMessageBus` | 单进程 / 本地开发，零 Redis 依赖 |
| `main_redis.py` | `RedisMessageBus` | 多进程 / 多副本部署 |

本部分按 `main_redis.py` 的真实结构逐段讲解。

### 9.1 create_app 骨架

```python
# -*- coding: utf-8 -*-
"""The example script to start the agent service (Redis-backed message bus)."""
import os

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope import setup_logger
from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig
from agentscope.permission import PermissionContext, PermissionMode

# -- 行内模型平台（bocom_agentscope 发行版：providers）----------------------
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.credential_model import credential_model_router

# 模型卡目录：本入口文件同级的 models/（随 bocom-starter 交付，whl 内不含）
EllmChatModel.set_models_dir(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
)
setup_logger("INFO")

default_mcps = [
    MCPClient(
        name="browser-use",
        mcp_config=StdioMCPConfig(command="npx", args=["@playwright/mcp@latest"]),
        is_stateful=True,
    ),
]
if os.getenv("AMAP_API_KEY"):       # 按需追加高德 MCP
    default_mcps.append(
        MCPClient(
            name="amap",
            mcp_config=HttpMCPConfig(
                url=f"https://mcp.amap.com/mcp?key={os.environ['AMAP_API_KEY']}",
            ),
            is_stateful=False,
        ),
    )

# 主存储：SQLAlchemy 异步 URL（MySQL / OB 兼容 MySQL 协议均可）
MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"
storage = AsyncSQLAlchemyStorage(
    MYSQL_URL,
    create_tables=True,          # 示例/开发用；生产置 False，改跑 alembic upgrade head
    engine_kwargs={"pool_pre_ping": True, "pool_recycle": 3600},
)

# 消息总线：收件箱 / 唤醒 / 分布式锁都落在 Redis 上，多进程共享；
# 与 create_app 共享同一实例（行内模型 key 刷新中间件复用）。
message_bus = RedisMessageBus(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=int(os.getenv("REDIS_DB", "0")),
    password=os.getenv("REDIS_PASSWORD") or None,
)

# 行内模型平台：ELLM api key 刷新中间件工厂（惰性预刷 + 401 强制刷新重试）
_ellm_refresh_factory = build_ellm_refresh_middleware(storage, message_bus)

app = create_app(
    storage=storage,
    message_bus=message_bus,
    workspace_manager=LocalWorkspaceManager(
        basedir=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "workspaces",
        ),
        default_mcps=default_mcps,     # 每个工作区注入默认 MCP，见 (6.2)
        # skill_paths=["/path/to/skills"],   # 工作区技能目录，见 (4.4)
    ),
    custom_subagent_templates=[
        SubAgentTemplate(
            type="explorer",
            description="Read-only agents specialized in exploration tasks ...",
            system_prompt_template="You are {member_name}, an explorer agent ...",
            permission_context=PermissionContext(mode=PermissionMode.EXPLORE),
        ),
    ],
    extra_agent_middlewares=_ellm_refresh_factory,   # 见 (9.3)
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
        ),
    ],
)

# 行内模型平台路由：/model/credential 凭证配置查询（GET）+ 部分更新（PATCH）
app.include_router(credential_model_router)

if __name__ == "__main__":
    uvicorn.run(
        "main_redis:app",       # 模块名必须与文件名一致
        host="0.0.0.0",
        port=8000,
        log_level="INFO",
        reload=os.getenv("UVICORN_RELOAD", "false").lower() in ("1", "true", "yes"),
    )
```

`main_memory.py` 的差异只有两行：

```python
from agentscope.app.message_bus import InMemoryMessageBus
message_bus = InMemoryMessageBus()
```

### 9.2 create_app 关键参数

`create_app(storage, message_bus, workspace_manager, ...)` 前三个是必填
位置参数，其余关键字参数如下：

| 参数 | 作用 | 行内注意点 |
|---|---|---|
| `storage` | 主存储（会话/凭证/agent 等全部业务数据） | 行内用 `AsyncSQLAlchemyStorage`（MySQL/OB）；另有 `RedisStorage` 实现可选 |
| `message_bus` | 进程间消息总线 + 分布式锁 | **必须与 ELLM 刷新中间件共享同一实例**；单进程 `InMemoryMessageBus`，多进程 `RedisMessageBus` |
| `workspace_manager` | 工作区与默认 MCP/技能管理 | `LocalWorkspaceManager(basedir, isolation=PER_AGENT, default_mcps=..., skill_paths=..., ttl=3600.0)` |
| `extra_agent_middlewares` | **Agent 级中间件工厂**，`async (user_id, agent_id, session_id) -> list[MiddlewareBase]`，**每轮 run 调用一次** | ELLM key 刷新中间件从这里挂载（见 9.3）；工厂签名**无 workspace 参数**，返回的实例不跨轮复用 |
| `extra_middlewares` | FastAPI 原生中间件 | CORS 等 |

> 2.0.5 无 `mcp_hubs` / `skill_hubs` / `channels` 参数（2.0.7 新增），
> 传入会直接报错；技能由工作区 `skill_paths` 加载（见 (4.4)）。

### 9.3 服务模式下的中间件工厂

源码模式下手动调用的中间件能力，服务模式下经
**AgentMiddlewareFactory** 按会话实例化（每个会话独立实例，天然隔离
会话级状态）。`build_ellm_refresh_middleware` 返回的就是工厂：

```python
_ellm_refresh_factory = build_ellm_refresh_middleware(
    storage,          # 与 create_app 共享
    message_bus,      # 与 create_app 共享
    # refresh_ahead_secs 非必填，默认 300.0（key 过期前提前刷新窗口）
)

app = create_app(..., extra_agent_middlewares=_ellm_refresh_factory)
```

`create_app` 只接受**一个**工厂；要挂多个中间件就在工厂内部合并：

```python
async def _combined_agent_middlewares(user_id, agent_id, session_id):
    mws = await longterm_memory_factory(user_id, agent_id, session_id)
    mws.extend(await _ellm_refresh_factory(user_id, agent_id, session_id))
    return mws

app = create_app(..., extra_agent_middlewares=_combined_agent_middlewares)
```

> **工厂签名是 `(user_id, agent_id, session_id)` 三个位置参数，不含
> workspace**。需要工作区（如长程记忆的 `workdir`）时在工厂内部自己解析：
> `storage.get_session(...)` 取 `session_record.config.workspace_id`，再
> `workspace_manager.get_workspace(...)`；或换个思路——长程记忆直接按
> `workspace.workdir` 组装（见 (7.2)）。
>
> `bocom-starter` 两个入口目前**只挂 ELLM key 刷新工厂**，没有挂长程记忆
> 中间件；需要时按上面的合并写法自行加。

### 9.4 挂载 bocom-agentscope 路由

本包只提供**一个**路由（`providers.routers.credential_model`）：

| 路由 | 功能 |
|---|---|
| `GET /model/credential?credential_id=...` | 返回该凭证实际存储的配置（归属/共享校验，不可见 → 404） |
| `PATCH /model/credential/{id}` | 凭证**部分**更新：只覆盖传入字段（`{"data": {...}}`），其余保持原值；非 `bocom_ellm_credential` → 400，合并后校验失败 → 422 |

> `PATCH` 与官方 `PATCH /credential/{id}` 的区别：官方是整体替换，本路由是
> 合并式更新；`id` / `type` 永远保持原值不可覆盖。api_key 刷新也写回同一
> 凭证记录。


```bash
# 按凭证查配置（凭证不绑定模型）
curl -H 'x-user-id: test-user' \
  'http://localhost:8000/model/credential?credential_id=<id>'

# 部分更新凭证：只覆盖传入字段（api_key 刷新也写回同一记录）
curl -X PATCH http://localhost:8000/model/credential/<id> \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{"data": {"api_key": "sk-new"}}'

# 候选模型列表（官方端点，读 set_models_dir 指定目录的模型卡，见 (3.3)）
curl -H 'x-user-id: test-user' \
  'http://localhost:8000/model/?provider=bocom_ellm_credential'
```

### 9.5 启动与验证

```bash
cd bocom-starter

# 本地开发（InMemoryMessageBus：无需 Redis，但需要 MySQL）
python main_memory.py
uvicorn main_memory:app --reload

# 多进程 / 多副本（RedisMessageBus：需要 Redis）
python main_redis.py
uvicorn main_redis:app --reload
```

| 入口 | 消息总线 | 日志级别 | 监听地址 |
|---|---|---|---|
| `main_memory.py` | `InMemoryMessageBus` | `INFO` | `0.0.0.0:8000` |
| `main_redis.py` | `RedisMessageBus` | `INFO` | `0.0.0.0:8000` |

- **启动前**：MySQL/OB 库需已建好（`create_tables=True` 自动建表）；
  `main_redis.py` 另需 Redis 可达（`REDIS_HOST` / `REDIS_PORT` /
  `REDIS_DB` / `REDIS_PASSWORD`，默认 `localhost:6379/0`）；
- **模型卡**：`models/` 目录必须存在且含 `*.yaml`，否则
  `set_models_dir` 启动即抛 `FileNotFoundError`；
- 热重载：`UVICORN_RELOAD=true` 环境变量控制（默认 `false`，生产镜像
  自包含部署不 reload）；
- 验证：`http://localhost:8000/docs`（Swagger UI）；日志经行内平台查看；
- 所有业务接口要求 `X-User-ID` 请求头（临时 header 身份），缺失返回
  422 `Field required`。

### 9.6 HTTP API 端到端示例

以下示例默认服务在 `http://localhost:8000`，统一 `X-User-ID: test-user`。

**(a) 创建行内凭证**（同 (3.1) 字段，服务模式经 API 提交）：

```bash
curl -X POST http://localhost:8000/credential/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "data": {
      "type": "bocom_ellm_credential",
      "api_key": "sk-xxx",
      "base_url": "http://ellm-gateway.example/v1",
      "scene_code": "P2024146",
      "api_key_url": "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do"
    }
  }'
# → {"credential_id": "cred-xxx"}
# 凭证不绑定模型；模型名在
# 创建会话时经 chat_model_config.model 指定（见 (c)）。
```

查询/管理：`GET /credential/` 列表、`GET /credential/schemas` 确认注册、
`GET /model/credential?credential_id=...`、`PATCH /model/credential/{id}`。

**(b) 创建 agent**：

```bash
curl -X POST http://localhost:8000/agent/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{"name": "ellm-assistant"}'
# → {"agent_id": "agent-xxx"}
```

**(c) 创建会话并绑定行内模型**（`chat_model_config.type` 用凭证类型）：

```bash
curl -X POST http://localhost:8000/sessions/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "agent_id": "agent-xxx",
    "chat_model_config": {
      "type": "bocom_ellm_credential",
      "credential_id": "cred-xxx",
      "model": "deepseek-flash",
      "parameters": {}
    }
  }'
# → {"session_id": "sess-xxx"}
```

- `model` 必须是 `models/` 里**已有卡片**的模型名（否则回落默认
  context_size 并打 warning，见 (3.3)）；
- `parameters` 原样透传给 `EllmChatModel.Parameters`（`max_tokens` /
  `temperature` / `top_p` / `enable_thinking` / `reasoning_effort`，见 (3.2)）；
- 会话也可先建后补模型：`PATCH /sessions/{session_id}` 更新
  `chat_model_config`。

**(d) chat（SSE 流式）**：

`POST /chat/` 是 **fire-and-forget**：响应体只返回
`{"status": "started", "session_id": ...}`，**回复内容在
`GET /sessions/{session_id}/stream` 的 SSE 事件流**。需要两个终端：

```bash
# 终端 A：订阅会话事件流（先回放缓冲事件再实时推送；30s 心跳注释帧）
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

**`input` 形态**：`Msg`（`name` + `role` + 块列表 `content`）、
`list[Msg]`（历史回灌）、`UserConfirmResultEvent` /
`ExternalExecutionResultEvent`（恢复 HITL/外部执行，携带 `reply_id`）、
`null`（从当前状态继续）。

**终端 A 事件流**（每帧 JSON，`type` 区分）：`REPLY_START` →
`MODEL_CALL_START` → `TEXT_BLOCK_*`（模型返回 `reasoning_content` 时先
`THINKING_BLOCK_*` 三连）→ `REPLY_END`；工具调用见 `TOOL_CALL_*` …
`TOOL_RESULT_END`；HITL 暂停见 `REQUIRE_USER_CONFIRM`。

**常见错误**：

| 现象 | 原因 |
|---|---|
| 422 `x-user-id` 缺失 | 所有业务接口要求 `X-User-ID` 头 |
| 422 `input` 校验失败 | `Msg` 缺 `role` 或 `content` 不是块列表（非字符串） |
| 409 冲突 | 同会话已有 run 在飞（等上一轮 `REPLY_END` 再触发） |
| 404 | session/agent 不存在或不属于该用户 |
| 500/启动失败 `FileNotFoundError: ELLM model cards directory` | `models/` 目录不存在或为空（见 9.5） |
| SSE 里 `MODEL_CALL_END` 带 error | 模型调用失败（连接/鉴权），看服务端日志 |

**(e) 常用 API 速查**：

| API | 功能 |
|---|---|
| `GET /credential/schemas` | 确认 `bocom_ellm_credential` 已注册 |
| `POST/GET/PATCH/DELETE /credential/` | 凭证增查改删（官方路由） |
| `GET /model/credential?credential_id=...` | 按凭证查配置（本包路由） |
| `PATCH /model/credential/{id}` | 凭证部分更新（本包路由，合并式） |
| `GET /model/?provider=bocom_ellm_credential` | 候选模型列表（读模型卡，见 (3.3)） |
| `POST /agent/`、`POST /sessions/`、`PATCH /sessions/{id}` | agent/会话管理 |
| `POST /chat/` + `GET /sessions/{id}/stream` | 对话（fire-and-forget + SSE） |

---

## (10) 常见问题

**Q1：模型列表为空 / 不更新？**
候选模型只来自**模型卡目录**：确认启动程序调用了
`EllmChatModel.set_models_dir(<dir>)` 且该目录存在、含 `*.yaml` 卡片。
新增模型 = 往目录放一张卡片 yaml（字段见 (3.3)）

**Q2：chat 报 401 / `MODEL_CALL_END` 带 error？**
检查凭证 `scene_code`、`api_key_url` 是否填对（key 自动刷新必填）；
确认中间件已挂（`extra_agent_middlewares` / `Agent(middlewares=...)`）且与
`create_app` 共享同一 `message_bus` 实例。看服务端日志关键字
`injected refreshed ELLM key`（正常注入）与
`EllmKeyRefresher: key refresh failed`（网关不可达，沿用旧 key）。

**Q3：`<think>` 标签没出现？**
`<think>` 注入按"构造参数 > 模型卡 `inject_think_tag` > `False`"取值
（见 (3.6)）：源码模式检查是否显式传了 `inject_think_tag=True`，或卡片里
是否写了 `inject_think_tag: true`；服务模式走 `get_model`（不传该参数），
只认该 `model` 对应卡片里的配置，改卡片下一轮对话生效。注意：这与是否
收到 thinking 块无关——是否返回 `reasoning_content` 由网关/模型自身行为
决定（可用 `enable_thinking` 参数经 `parameters` 下发）。

**Q4：跨用户凭证刷新失败？**
凭证跨用户放开时，刷新写回必须落到凭证真实 owner 名下（框架 upsert
按 `(id, user_id)` 命中）。注意：跨 owner 查询的全局兜底仅 SQL 主存储
支持，Redis 主存储下会静默返回空 → 表现为"刷新写不进"，凭证共享场景
需自行保证调用方与凭证同 owner。

**Q5：多进程部署时 key 刷新并发串号？**
`message_bus` 需换 `RedisMessageBus` 并**与中间件共享同一实例**（分布式
锁 `ellm:refresh:{id}` 依赖它）；401 回调闭包绑定 `credential_id`，防并发
串号。
