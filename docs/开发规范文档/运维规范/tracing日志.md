# tracing 日志

> 目标：接入全链路追踪与结构化日志——OpenTelemetry 三层 span、事件流日志与日志分级。

## 全链路追踪：TracingMiddleware

内置 `TracingMiddleware` 基于 OpenTelemetry，产生**三层 span**：

```text
agent.reply（整轮回复）
 └── agent.reasoning（推理步骤）
      └── model.call（模型调用，含 usage 属性）
```

### 接入步骤

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agentscope.middleware import TracingMiddleware

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer("my-agent-app")

agent = Agent(
    ...,
    middlewares=[TracingMiddleware(tracer=tracer)],
)
```

> **提示**：中间件列表中**第一个在最外层**——把 `TracingMiddleware` 放首位，保证追踪覆盖整轮回复（含后续中间件的行为）。

### 导出到后端

`TracerProvider` 配置 exporter 后，span 可送入 Jaeger / Tempo / SigNoz 等兼容 OTel 的观测后端：

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="otel-collector:4317"))
)
trace.set_tracer_provider(provider)
```

### 自定义 span 属性

可在中间件上追加业务属性（如用户 ID、会话 ID、任务类型），便于在追踪系统中按业务维度过滤：

- `model.call` span 自带 **usage 属性**（token 消耗）——成本分析直接在 tracing 系统中做；
- 结合 `on_reply_start` 等钩子注入的 `state.metadata`，可实现按租户/会话聚合的追踪视图。

## 日志渲染：ConsoleRenderer

开发期日志的主力是事件流渲染器。渲染器是**被动**的：事件如何产生、确认如何处理由调用方决定，渲染器只负责打印：

```python
from agentscope.console import ConsoleRenderer

renderer = ConsoleRenderer()

async for event in agent.reply_stream(UserMsg("user", "你好！")):
    renderer.render(event)

# 渲染器按 reply_id 归属事件，累积出完整回复
final_msg = renderer.last_msg
```

多个智能体顺序发言时可复用同一实例（上一个智能体的回复作为下一个的输入）：

```python
msg = UserMsg("user", "写一段产品介绍")
for agent in [writer, reviewer]:
    async for event in agent.reply_stream(msg):
        renderer.render(event)
    msg = renderer.last_msg
```

### 渲染规则

- 回复文本与思考过程**实时流式打印**（思考以暗色显示）；
- 工具调用与结果在结束事件到达时**整块打印**（并发结果不交错）；
- 提示块以带边框面板展示；
- 二进制数据以占位符显示（如 `[data: image/png, ~34KB]`）；
- 每次模型调用后打印一行 token 用量。

### verbosity 三档

| 档位      | 输出内容                                                       | 适用场景           |
| --------- | -------------------------------------------------------------- | ------------------ |
| `quiet`   | 仅回复文本与错误                                               | 生产精简输出       |
| `default` | 增加思考、工具、提示块、用量、确认提醒                         | 日常开发（默认）   |
| `debug`   | 再增加生命周期事件与工具结果元数据                             | 排查问题           |

> **提示**：渲染器静默跳过未知事件类型——事件协议扩展不会破坏既有渲染，自定义事件可安全混入流中。

## 工具调用日志

工具执行环节的审计日志通过**工具中间件**实现（洋葱模型，包裹每次工具调用）：

```python
class LoggingMiddleware(ToolMiddlewareBase):
    """记录每次工具调用的名称、参数摘要与耗时。"""

    async def on_call(self, tool, tool_input, call_next):
        import time
        t0 = time.monotonic()
        try:
            result = await call_next(tool, tool_input)
            logger.info("tool=%s ok elapsed=%.0fms", tool.name,
                        (time.monotonic() - t0) * 1000)
            return result
        except Exception:
            logger.exception("tool=%s failed", tool.name)
            raise
```

价值场景：安全审计（谁在什么时候执行了什么命令）、慢工具定位、失败率统计。

## 服务化场景的日志约定

| 层次         | 建议                                                             |
| ------------ | ---------------------------------------------------------------- |
| API 访问日志 | uvicorn/FastAPI 标准访问日志，按 `user_id` 过滤排查              |
| 智能体运行   | TracingMiddleware 三层 span + `on_reply_start` 业务日志          |
| 工具执行     | 工具中间件审计日志（含参数摘要与结果状态）                       |
| RAG 索引     | 文档状态机的 `error` 字段（单行错误，前端可见）                  |
| 渠道         | `ChannelStatus.state` 流转 + 平台 SDK 自身日志                   |

> **注意**：日志中不要输出凭证与完整密钥（见 [数据识别和脱敏](../开发规范/06-安全/数据识别和脱敏.md)）；工具参数摘要注意截断与脱敏后再落日志。

## 常见问题（FAQ）

**Q：AgentScope 2.0 与 1.0 兼容吗？**
不兼容。2.0 重新设计了 agent 抽象，新增事件系统、workspace、权限系统等大量特性，API 与 1.0 不兼容且无自动迁移。新项目直接用 2.0。

**Q：支持沙箱化执行吗？**
支持。Workspace 是执行环境抽象，内置 Local/Docker/E2B 等多种实现共享同一接口，同一份智能体代码可无差别运行。多租户场景配合 WorkspaceManager。

**Q：有配套前端吗？**
分两层：TypeScript SDK（类型与 Python 端对齐，直接消费流式输出）；面向智能体服务的开箱即用 Web UI（`examples/web_ui`）。

**Q：除 Python 还有其他语言版本吗？**
三种独立仓库的实现：Python（`agentscope-ai/agentscope`）、TypeScript（`agentscope-ai/agentscope-typescript`）、Java（`agentscope-ai/agentscope-java`）。

**Q：RAG 和长期记忆在 2.0 中可用吗？**
可用。均已按 2.0 架构提供，并持续迭代新版本能力。

**Q：tracing 对性能有影响吗？**
`BatchSpanProcessor` 异步批量导出，开销可控；`quiet` 档渲染器输出最少。压测环境可对比有/无 TracingMiddleware 的回复耗时差异。

运行状态与业务指标的监控面见 [监控](监控.md)；部署形态见 [CICD 集成](../部署规范/CICD集成.md)。
