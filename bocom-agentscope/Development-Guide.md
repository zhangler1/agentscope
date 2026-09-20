# bocom-agentscope 智能体开发指引
> agentscope==2.0.5
> **读者对象**：基于 `bocom-agentscope`（行内能力扩展包，当前含行内模型平台
> 对接，已打包行内 AgentScope 2.0 SDK）开发智能体的研发人员。
>
> **阅读方式**：**(3)~(8) 讲核心模块的源码开发方式**（直接实例化 SDK
> 对象组装智能体），**(9) 单独讲起 Service**（服务化部署）。两种模式共用
> 同一套模块，区别只在于"对象怎么创建、谁来驱动"。
>
> **参考文档**：AgentScope 2.0 官方教程（`https://doc.agentscope.io/`
> Tutorial 下的 *Agent*、*Model*、*Tool*、*Agent Skill*、*Middleware*、
> *Long-Term Memory*、*MCP*、*State/Session Management* 章节）。
> 本文只讲"行内差异 + 落地用法"，官方能力不重复展开。

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
| key 刷新 | `fetch_ellm_key` + `set_api_key` 手动注入 | `EllmKeyRefreshMiddleware` 全自动 |
| 适用场景 | 脚本、批处理、嵌入既有系统、单元测试 | 生产服务、多租户、前端对接 |

### 1.3 一次对话的完整链路（源码视角）

```
await agent.reply(Msg("user", ...))
  └─ Agent 推理循环（reasoning-acting）
       ├─ 模型调用 EllmChatModel.__call__
       │    ├─ formatter.format(messages)          # DeepSeekChatFormatter
       │    ├─ _call_api → ELLM 网关 /v1/chat/completions（OpenAI 兼容）
       │    │    ├─ 参数下发：max_tokens / temperature / top_p /
       │    │    │   enable_thinking(chat_template_kwargs) / reasoning_effort
       │    │    ├─ 401 invalid_api_key → 刷新 key 重试一次（需注入回调）
       │    │    └─ 流式解析：reasoning_content → thinking 块
       │    │        inject_think_tag=True 时首文本增量前加 <think>
       │    └─ 返回 ChatResponse（含 usage）
       ├─ 工具调用：Toolkit 按 ToolCallBlock 分发（洋葱式工具中间件可拦截）
       └─ 技能提示词已自动拼进 system prompt（get_skill_instructions）
```

服务模式下，这条链路整体包在 FastAPI 里，中间件工厂在每轮 run 前把
`EllmKeyRefreshMiddleware` 装到模型调用链上（见 (9.3)）。

---

## (2) 环境准备与安装

### 2.1 依赖

- Python ≥ 3.12、pip；
- 数据库：**服务化模式必需**（应用主存储：会话、凭证、agent 等全部业务数据），
  OceanBase（兼容 MySQL 协议）/ MySQL，驱动 `aiomysql`，经
  `AsyncSQLAlchemyStorage`（SQLAlchemy 2.0 async）落库；
  **源码开发模式无需**（对象在内存组装，会话状态内置 Agent state）；
- Redis：仅**多进程/多副本部署**时可选，作消息总线（瞬时协调：inbox /
  唤醒 / 分布式锁），单进程用 `InMemoryMessageBus` 即可；
- 行内 pip 源（SDK 源码随发行版分发，行内无外网）。

### 2.2 安装

```bash
# 从行内依赖库直接安装（依赖自动解析；发行版包名 bocom-agentscope）
pip install bocom-agentscope
```

需要同时改 `bocom-agentscope/` 源码联调时，用源码可编辑安装：

```bash
cd bocom-agentscope
pip install -e . --no-deps
```

---

## (3) 模型模块

### 3.1 ELLM 凭证：ELLMCredential

自研 ELLM 平台（模型由 DeepSeek 提供，候选卡见 (3.3)）暴露
OpenAI 兼容端点（`/v1`）。凭证 `type="bocom_ellm_credential"`，**导入
`providers.credential` 即完成 `CredentialFactory` 注册**（幂等）。
直接实例化：

```python
from providers.credential import ELLMCredential  # 导入即注册 CredentialFactory

credential = ELLMCredential(
    api_key="sk-xxx",                 # SecretStr，可省略（默认占位值，运行时被刷新器注入真实 key）
    base_url="http://ellm-gateway.example/v1",   # OpenAI 兼容端点（以 /v1 结尾）
    scene_code="P2024146",            # 场景编码（api_key 自动刷新必填，业务字段）
    api_key_url=(                     # key 申请地址（createSceneApiKey.do 端点）
        "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do"
    ),
    # apikey_expires_at：key 过期时间戳（仅服务模式 EllmKeyRefresher 读写；源码模式无需）
)
```

> 凭证**不绑定模型**：运行时模型名来自会话 `chat_model_config.model`
> （见 (9.6)(c)）；`<think>` 注入由模型构造参数
> `inject_think_tag` 决定（见 (3.6)）。

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `api_key` | 否 | `SecretStr`，省略时默认占位值 `sk-xxx`；运行时由刷新器注入真实 key |
| `base_url` | ✅ | OpenAI 兼容端点（**以 `/v1` 结尾**） |
| `scene_code` | ✅ | 场景编码（业务字段；**api_key 自动刷新的必填参数**，向网关 `createSceneApiKey.do` 申请新 key） |
| `api_key_url` | ✅ | key 申请地址（`createSceneApiKey.do` 端点） |
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
    model="deepseek-v4-flash",
    context_size=1000000,     # 必填：模型上下文窗口（供上下文压缩），按模型卡片取值
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
    inject_think_tag=False,    # <think> 注入开关，构造时定死（见 (3.6)）
    # formatter=None 时默认 DeepSeekChatFormatter
    # client_kwargs 转发给 openai.AsyncClient（timeout/default_headers 等）
)
```

生成参数（`Parameters`）：

| 参数 | 说明 |
|---|---|
| `max_tokens` | 最大输出 token（上限由模型卡片 `output_size` 决定） |
| `temperature` / `top_p` | 采样参数 |
| `enable_thinking` | 开启思考模式，经 `extra_body.chat_template_kwargs.enable_thinking` 下发 |
| `reasoning_effort` | 推理强度提示（low/medium/high），顶层 `reasoning_effort` 字段 |

### 3.3 模型候选：list_models 与模型卡片

候选模型来自**模型卡片 yaml**：`EllmChatModel.list_models()` 默认读取
模型类源码旁的 `_models` 目录（分发包不含卡片 → 默认返回空列表）；
**用 `custom_yaml_dir` 参数指定自己的卡片目录**即可被识别（每次调用
实时扫描，无需重启）。卡片格式可参考官方 SDK
`agentscope/model/_deepseek/_models/` 下的 DeepSeek 模型卡
（含 `deepseek-v4-flash` / `deepseek-v4-pro`）。

卡片示例（`deepseek-v4-flash.yaml`）：

```yaml
name: deepseek-v4-flash
label: DeepSeek V4 Flash
status: active
input_types: [text/plain, application/x-thinking]
output_types: [text/plain, application/x-thinking]
context_size: 1000000    # 供上下文压缩，构造模型时按此传入
output_size: 384000      # 决定 max_tokens 上限
parameter_overrides:
  max_tokens: {maximum: 384000}
```

```python
cards = EllmChatModel.list_models(custom_yaml_dir="/path/to/my/models")
# 返回 ModelCard 列表：name/label/input_types/output_types/
# context_size/output_size/parameter_schema
```

- 卡片由调用方经 `custom_yaml_dir` 指定目录（每次调用实时扫描）；
- 服务模式下候选模型经官方 `GET /model/?provider=bocom_ellm_credential`
  查询。

### 3.4 直接调用模型

```python
import asyncio

from agentscope.message import Msg, TextBlock

async def main() -> None:
    reply = await model([
        Msg(name="user", content=[TextBlock(text="你好，介绍一下你自己")], role="user"),
    ])
    # 流式：返回增量序列，逐个消费（is_last=True 收尾）
    text = ""
    async for chunk in reply:
        for block in chunk.content:
            if isinstance(block, TextBlock):
                text += block.text
    print(text)

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
)
# ttl_ms 归一化为"从现在起剩余毫秒数"（网关返回 TTL 时长或绝对毫秒时间戳均可）
model.set_api_key(key)   # 注入后续请求的 Authorization: Bearer <key>
```

**带状态的刷新器 `EllmKeyRefresher`**（按 `credential_id` 惰性刷新存储
里的凭证 key，需要 `StorageBase` + `MessageBus`；完整用法见 (5.3)）：

```python
from providers.ellm_key import EllmKeyRefresher

refresher = EllmKeyRefresher(
    storage,           # 凭证读写后端
    message_bus,       # 分布式锁传输（acquire_lock）
    user_id="test-user",
    refresh_ahead_secs=300.0,  # 默认值：key 过期前 300s 提前刷新
)
key, record = await refresher.ensure_fresh_key(credential_id)   # 惰性刷新
new_key = await refresher.force_refresh_key(credential_id)      # 401 强制刷新
await refresher.invalidate_key(credential_id)                   # 标记过期
```

- `ensure_fresh_key`：快路径（未过期直接复用存储 key，无锁无网络）→
  慢路径（`ellm:refresh:{id}` 锁内二次校验新鲜度，并发只打网关一次）；
- `force_refresh_key`：跳过本地过期判断强取新 key；失败沿用旧 key；
- `invalidate_key`：清掉 `apikey_expires_at`，下次调用走惰性刷新；
- 写回凭证时落到凭证**真实 owner** 名下（跨用户共享场景）。

### 3.6 think-tag 注入开关

`<think>` 注入由模型**构造参数** `inject_think_tag` 决定（构造时定死）：

```python
model = EllmChatModel(..., inject_think_tag=True)   # 构造时开启
model.inject_think_tag = False                      # 运行期直接改属性也生效
```

- 开启后：流式响应首个非空文本增量前注入 `<think>` 前缀（空增量跳过）；
- 是否收到 `reasoning_content`（thinking 块）由网关/模型自身行为决定，
  与该开关无关。

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

- 技能经 `Toolkit` 构造参数 `skills_or_loaders` 注册（目录字符串、
  `Skill`、`SkillLoaderBase` 均可），提示词模板可定制：
  `Toolkit(skill_instruction_template=...)`；
- Toolkit 内置 `SkillViewer` 工具，agent 用它读取 `SKILL.md` 正文
  （技能本身不可直接调用）。

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

要点：**使用技能时，agent 通过内置 `SkillViewer` 工具读取 `SKILL.md`
正文**（技能提示词已声明这一点）。

### 4.4 开发一个技能的最小步骤

1. 建目录 `<skill-name>/`，写 `SKILL.md`（frontmatter 必须含 `name`、
   `description`）；
2. 附上脚本/资源（`scripts/`、`references/` 等，按需）；
3. `Toolkit(skills_or_loaders=["<skill-name>"])` 注册；
4. 验证技能提示词已进 system prompt（agent 经内置 `SkillViewer` 读正文）。

> 服务模式下技能经 `skill_hubs` 供 UI 浏览安装、或由工作区技能加载器按
> 同一 SKILL.md 规范加载（见 (9.2)），底层机制与本节完全一致。

---

## (5) 中间件模块

### 5.1 体系概览

AgentScope 2.0 的中间件体系分两级：

| 级别 | API | 挂载点 |
|---|---|---|
| 工具级 | `agentscope.tool.ToolMiddlewareBase` | 工具调用前/后（洋葱模型），`FunctionTool(middlewares=[...])` 挂载 |
| Agent 级 | `agentscope.middleware.MiddlewareBase` | 推理循环钩子（如 `on_model_call`） |

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
    def __init__(self, storage, message_bus, user_id, refresh_ahead_secs=300.0):
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

> 本中间件只负责 api_key；`<think>` 注入由模型构造参数
> `inject_think_tag` 决定（见 (3.6)）。

配合 `EllmChatModel._request_with_retry_on_auth`：网关返回
`invalid_api_key` 401 时强制刷新 key 并重试当前调用一次；刷新失败标记
凭证过期，下次调用走惰性刷新恢复。调试日志关键字：
`injected refreshed ELLM key`、`ELLM 401: refreshed key and retrying once`。

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

> 服务模式下 Agent 级中间件经 **AgentMiddlewareFactory** 按会话实例化
> 挂载（见 (9.3)）；源码模式直接挂到 `Agent(middlewares=[...])`
> （见 (8)），或用能力原语（`fetch_ellm_key` + `set_api_key`，见 (3.5)）。

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

- 工具函数直接返回 `str`（或 `ToolChunk`），框架自动包装；
- 内置工具（Bash、Read 等）与业务自定义工具统一经 `Toolkit` 注入
  agent；工具执行可被 (5.2) 的工具级中间件拦截。

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
工作区注入默认 MCP（见 (9.2)）。

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
    ),
)
```

### 7.2 长程记忆

`AgenticMemoryMiddleware` 把长期记忆落为工作区内的 Markdown 文件（随
workspace 后端持久化）：

```python
from agentscope.middleware import AgenticMemoryMiddleware

memory_mw = AgenticMemoryMiddleware(
    workdir=workspace.workdir,          # 工作区目录
    backend=workspace.get_backend(),    # 执行后端（本地/Docker 等）
)
```

配合 `PER_AGENT` 工作区隔离，记忆可跨会话存活（官方 *Long-Term Memory*
教程）。知识库/RAG 能力见官方 *RAG* 教程。

---

## (8) 端到端源码开发示例

把 (3)~(7) 组装成一个可跑的智能体（源码开发模式的快速示例）：

```python
# -*- coding: utf-8 -*-
"""bocom-agentscope 源码开发最小示例：ELLM 模型 + 技能 + 工具 + 中间件。"""
import asyncio
import logging

from agentscope.agent import Agent
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import Msg, TextBlock
from agentscope.tool import FunctionTool, Toolkit

from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel
from providers.middleware import EllmKeyRefreshMiddleware

logging.basicConfig(level=logging.INFO)


async def search_tool(query: str) -> str:   # 见 (6.1)
    """A simple search tool."""
    return f"Search results for '{query}'"


async def main() -> None:
    # 1) 存储 + 消息总线（key 刷新中间件的依赖，见 (3.5)/(5.3)）
    #    SQL 存储必须 `async with` 进入（懒建 engine + 建表）
    async with AsyncSQLAlchemyStorage(
        url="mysql+aiomysql://agentscope:agentscope@localhost:3306/agentscope",
        create_tables=True,
        engine_kwargs={"pool_pre_ping": True, "pool_recycle": 1800},
    ) as storage:
        message_bus = InMemoryMessageBus()   # 单进程用进程内锁即可

        # 2) 行内凭证（先落库：中间件按 credential_id 读它刷新 key）+ 模型
        credential = ELLMCredential(
            api_key="sk-xxx",
            base_url="http://ellm-gateway.example/v1",
            scene_code="P2024146",
            api_key_url="http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do",
        )
        await storage.upsert_credential("test-user", credential)
        model = EllmChatModel(
            credential=credential,
            model="deepseek-v4-flash",
            context_size=1000000,
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

        # 5) 驱动对话
        reply = await agent.reply(
            Msg(name="user", content=[TextBlock(text="你好，介绍一下你自己")], role="user"),
        )
        print(reply.get_text_content())


if __name__ == "__main__":
    asyncio.run(main())
```

要点回顾：

- bocom-agentscope 只出现在**第 1、2、4 步**（凭证/模型/key 刷新中间件），
  其余全是原生 API；
- 不用中间件时，第 1 步的 storage/message_bus 可省略，key 改用能力原语
  手动注入（`fetch_ellm_key` + `set_api_key`，见 (3.5)）；
- 服务化时第 1~4 步的对象由 `create_app` 按会话配置动态构建、中间件经
  工厂装配，业务代码形态不变（见 (9)）。

---

## (9) 起 Service（服务化部署）

源码模式下组装好的能力，全部经 `agentscope.app.create_app` 装配成一个
FastAPI 服务。`bocom-starter/main.py` 即服务化模式的快速开发示例，本
部分按真实结构逐段讲解。

### 9.1 create_app 骨架

```python
# -*- coding: utf-8 -*-
"""The example script to start the agent service."""
import os
import uvicorn

from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

# -- 行内模型平台（bocom-agentscope 发行版：providers）-----------------------------
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.credential_model import credential_model_router

default_mcps = [MCPClient(...)]      # 见 (6.2)，每个工作区注入默认 MCP

# 主存储：数据库（AsyncSQLAlchemyStorage；OceanBase 兼容 MySQL 协议，
# 本地/测试可用 MySQL 模拟，切真 OB 只改 url 地址）。会话/凭证/Agent
# 等业务数据全部落库；create_tables 启动时自动建表。
storage = AsyncSQLAlchemyStorage(
    url="mysql+aiomysql://agentscope:agentscope@localhost:3306/agentscope",
    create_tables=True,
    engine_kwargs={"pool_pre_ping": True, "pool_recycle": 1800},
)
# 与 create_app 共享同一实例（行内模型 key 刷新中间件复用）。
message_bus = InMemoryMessageBus()   # 多进程部署换 RedisMessageBus（见 9.2）

app = create_app(
    storage=storage,
    message_bus=message_bus,
    workspace_manager=LocalWorkspaceManager(
        basedir=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "workspaces"),
        default_mcps=default_mcps,     # 见 (6.2)，每个工作区注入默认 MCP
    ),
    # ... 其余参数见 9.2
    extra_agent_middlewares=_combined_agent_middlewares,   # 见 9.3
)

# 行内模型平台路由（见 9.4）
app.include_router(credential_model_router)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,          # 本地开发热重载开关（默认关）
    )
```

### 9.2 create_app 关键参数

| 参数 | 作用 | 行内注意点 |
|---|---|---|
| `storage` | 主存储（会话/凭证/agent 等全部业务数据） | `AsyncSQLAlchemyStorage`（OceanBase 兼容 MySQL 协议）；`create_app` 必填参数（SDK 另有 `RedisStorage` 可选） |
| `message_bus` | 进程间消息总线 + 分布式锁 | **必须与 ELLM 刷新中间件共享同一实例**；单进程 `InMemoryMessageBus`，多进程部署换 `RedisMessageBus`（仅瞬时协调，非业务存储） |
| `workspace_manager` | 工作区与默认 MCP 管理 | `LocalWorkspaceManager` + `default_mcps`（见 (6.2)） |
| `knowledge_base_manager` / `knowledge_chunkers` | RAG 知识库 | 可选（官方 *RAG* 教程） |
| `mcp_hubs` / `skill_hubs` | UI 浏览的 MCP/技能市场 | 可选，如 `skill_hubs=[ClawSkillHub(api_token=...)]` |
| `custom_subagent_templates` | 自定义子代理模板（Team 协作） | 可选 |
| `extra_agent_middlewares` | **Agent 级中间件工厂**，`async (user_id, agent_id, session_id, workspace) -> list[MiddlewareBase]` | bocom-agentscope key 刷新中间件从这里挂载（见 9.3） |
| `extra_middlewares` | FastAPI 原生中间件 | CORS 等 |
| `channels` | 外部渠道（钉钉/Discord/飞书） | 可选 |

### 9.3 服务模式下的中间件工厂

源码模式下手动调用的中间件能力，服务模式下经
**AgentMiddlewareFactory** 按会话实例化（每个会话独立实例，天然隔离
会话级状态）。`build_ellm_refresh_middleware` 返回的就是工厂：

```python
_ellm_refresh_factory = build_ellm_refresh_middleware(
    storage,                                     # 与 create_app 共享
    message_bus,                                 # 与 create_app 共享
    # refresh_ahead_secs 非必填，默认 300.0（key 过期前提前刷新窗口）
)

app = create_app(..., extra_agent_middlewares=_ellm_refresh_factory)
```

`create_app` 只接受**一个**工厂，多个中间件在工厂内部合并（starter
模式，长程记忆 + ELLM key 刷新）：

```python
async def _combined_agent_middlewares(user_id, agent_id, session_id, workspace):
    mws = await longterm_memory_factory(user_id, agent_id, session_id, workspace)
    mws.extend(await _ellm_refresh_factory(user_id, agent_id, session_id))
    return mws

app = create_app(..., extra_agent_middlewares=_combined_agent_middlewares)
```

### 9.4 挂载 bocom-agentscope 路由

| 路由 | 功能 |
|---|---|
| `credential_model_router` | `/model/credential` 按凭证查模型 + 凭证部分更新（唯一保留的行内路由） |

> 模型候选由 `custom_yaml_dir` 指定的模型卡片提供（见 (3.3)），think-tag 由模型构造参数
> 决定（见 (3.6)）。

```bash
# 按凭证查配置（凭证不绑定模型）
curl -H 'x-user-id: test-user' \
  'http://localhost:8000/model/credential?credential_id=<id>'

# 候选模型列表（官方端点，见 (3.3)）
curl -H 'x-user-id: test-user' \
  'http://localhost:8000/model/?provider=bocom_ellm_credential'

# 部分更新凭证：只覆盖传入字段（api_key 刷新也写回同一记录）
curl -X PATCH http://localhost:8000/model/credential/<id> \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{"data": {"api_key": "sk-new"}}'
```

### 9.5 启动与验证

```bash
# 本地开发（热重载）
cd bocom-starter
uvicorn main:app --reload
# 或 python main.py（reload 手动传参，默认关闭）

# 生产部署：代码合入行内仓库，走行内 CICD 构建镜像并部署
```

- 验证：`http://localhost:8000/docs`（Swagger UI）；日志经行内平台查看
  （关键字见 5.3）；
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
      "model": "deepseek-v4-flash",
      "parameters": {}
    }
  }'
# → {"session_id": "sess-xxx"}
```

会话也可先建后补模型：`PATCH /sessions/{session_id}` 更新
`chat_model_config`。

**(d) chat（SSE 流式）**：

`POST /chat/` 是 **fire-and-forget**：响应体只返回
`{"status": "started", "session_id": ...}`，**回复内容在
`GET /sessions/{session_id}/stream` 的 SSE 事件流**。需要两个终端：

```bash
# 终端 A：订阅会话事件流（先回放缓冲事件再实时推送）
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
| SSE 里 `MODEL_CALL_END` 带 error | 模型调用失败（连接/鉴权），看服务端日志 |

**(e) 常用 API 速查**：

| API | 功能 |
|---|---|
| `GET /credential/schemas` | 确认 `bocom_ellm_credential` 已注册 |
| `POST/GET/PATCH/DELETE /credential/` | 凭证增查改删 |
| `GET /model/credential?credential_id=...` | 按凭证查配置 |
| `PATCH /model/credential/{id}` | 凭证部分更新 |
| `GET /model/?provider=bocom_ellm_credential` | 候选模型列表（模型卡片，见 (3.3)） |
| `POST /agent/`、`POST /sessions/`、`PATCH /sessions/{id}` | agent/会话管理 |
| `POST /chat/` + `GET /sessions/{id}/stream` | 对话（fire-and-forget + SSE） |

---

## (10) 常见问题

**Q1：模型列表为空 / 不更新？**
模型候选来自 yaml 卡片：`EllmChatModel.list_models(custom_yaml_dir=...)`
指定自己的卡片目录（分发包不带卡片，默认返回空列表）。

**Q2：chat 报 401 / `MODEL_CALL_END` 带 error？**
检查凭证 `scene_code`、`api_key_url` 是否填对（key 自动刷新必填）；
看服务端日志关键字 `ELLM 401: refreshed key and retrying once`（正常自愈）
与 `EllmKeyRefresher: key refresh failed`（网关不可达，沿用旧 key）。

**Q3：`<think>` 标签没出现？**
`<think>` 注入由模型构造参数 `inject_think_tag` 决定（见 (3.6)），构造
或运行期显式置 `True` 才会在流式首个文本增量前注入。是否收到
thinking 块由网关/模型自身行为决定。

**Q4：跨用户凭证刷新失败？**
凭证跨用户放开时，刷新写回必须落到凭证真实 owner 名下（框架 upsert
按 `(id, user_id)` 命中），跨 owner 查询走全局兜底（仅 SQL 主存储支持）。

**Q5：多进程部署时 key 刷新并发串号？**
`message_bus` 需换 `RedisMessageBus` 并**与中间件共享同一实例**（分布式
锁 `ellm:refresh:{id}` 依赖它）；401 回调闭包绑定 `credential_id`，防并发
串号。
