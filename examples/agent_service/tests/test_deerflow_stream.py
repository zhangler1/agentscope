"""create run 流式端点单测（deerflow_chat.py ``create_run_stream``）。

覆盖：SSE 首帧回显用户输入（LangGraph human messages 事件）。SDK 依赖
messages 事件把用户消息并入 ``values.messages``，前端 ``humanMessageCount``
增长后才会清理乐观消息——缺失时界面出现两条用户输入（"问题显示两次"，
回复插在第一条之后）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from agentscope.app._bus_ops import publish_session_event
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.message import Msg, TextBlock

from bocomadp.deerflow.bridge import BusBridge
from bocomadp.deerflow.routers.deerflow_chat import deerflow_router
from bocomadp.deerflow.runs import RunManager

THREAD_ID = "t1"
USER_ID = "user-1"
AGENT_ID = "test-agent"


class FakeStorage:
    """create_run_stream 的 storage 依赖最小实现（session 懒创建路径）。"""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], object] = {}
        self._messages: list[Msg] = []

    async def get_session(self, user_id: str, agent_id: str, session_id: str):
        return self._sessions.get((user_id, agent_id, session_id))

    async def upsert_session(
        self,
        user_id,
        agent_id,
        config,
        session_id,
        state=None,
    ):
        self._sessions[(user_id, agent_id, session_id)] = SimpleNamespace(
            config=config,
            state=state,
        )

    async def get_agent(self, user_id: str, agent_id: str):
        del user_id, agent_id
        # 与真实 AgentRecord 一致：带 data.name（HITL 挂起防护检测用）。
        return SimpleNamespace(
            data=SimpleNamespace(name="对比测试agent"),
        )

    async def upsert_agent(self, user_id: str, record) -> None:
        return None

    async def upsert_credential(self, user_id: str, credential) -> None:
        return None

    async def get_credential(self, user_id: str, credential_id: str):
        del user_id, credential_id
        return None

    async def list_credentials(self, user_id: str):
        del user_id
        return []

    async def list_messages(
        self,
        user_id: str,
        session_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> tuple[list[Msg], bool]:
        del user_id, session_id, before
        return self._messages[-limit:], False

    def seed(self, *messages: Msg) -> None:
        self._messages = list(messages)


class FakeWorkspaceManager:
    def assign_workspace_id(self, **kwargs) -> str:
        return "ws-test"


class FakeAccess:
    """resource_access_service 依赖最小实现（绑定凭证兑底解析）。"""

    async def resolve_credential(self, user_id: str, credential_id: str):
        del user_id, credential_id
        return None


class FakeChatService:
    """spawn 后以真实 run_id 发布 REPLY_START + REPLY_END。

    延迟 0.3s 保证 SSE 的 live 订阅先建立（InMemory 广播无缓冲，先发布
    则丢失——回放侧 run_id 随机生成无法预置对齐）。``usage`` 非 None 时
    在 REPLY_END 前追加 MODEL_CALL_END（token 用量下发通道）。
    """

    def __init__(
        self,
        bus: InMemoryMessageBus,
        usage: tuple[int, int] | None = None,
    ) -> None:
        self._bus = bus
        self._usage = usage

    async def run(self, user_id, session_id, agent_id, input_msg, run_id=None):
        del user_id, agent_id, input_msg
        await asyncio.sleep(0.3)
        await publish_session_event(
            self._bus,
            session_id,
            {
                "type": "REPLY_START",
                "session_id": session_id,
                "reply_id": "r1",
                "name": "agent_a",
                "role": "assistant",
            },
            run_id=run_id,
        )
        if self._usage is not None:
            await publish_session_event(
                self._bus,
                session_id,
                {
                    "type": "MODEL_CALL_END",
                    "session_id": session_id,
                    "input_tokens": self._usage[0],
                    "output_tokens": self._usage[1],
                    "finished_reason": "stop",
                },
                run_id=run_id,
            )
        await publish_session_event(
            self._bus,
            session_id,
            {
                "type": "REPLY_END",
                "session_id": session_id,
                "reply_id": "r1",
                "finished_reason": "COMPLETED",
            },
            run_id=run_id,
        )

    async def interrupt(self, user_id: str, session_id: str, agent_id: str):
        return None


class FakeChatRunRegistry:
    """spawn 时创建未完成 Future（run 进行中，流挂起）。

    spawn 前 ``get`` 返回 None，放行 RunManager 的并发 409 检查。
    """

    def __init__(self) -> None:
        self._task: asyncio.Future | None = None

    def get(self, session_id: str):
        return self._task

    def spawn(self, coro, session_id=None, name=None):
        self._task = asyncio.get_running_loop().create_task(coro)
        return self._task

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


def _make_app(
    run_manager: RunManager,
    bus: InMemoryMessageBus,
    registry: FakeChatRunRegistry,
    storage: FakeStorage,
    chat_service: FakeChatService | None = None,
) -> FastAPI:
    # 与 main.py 一致：router 挂到子应用，再 mount 到 /api（对外路径不变）；
    # state 必须设在子应用上（挂载后 request.app 是子应用）。
    api = FastAPI()
    api.state.run_manager = run_manager
    api.state.bus_bridge = BusBridge(bus)
    api.state.chat_service = chat_service or FakeChatService(bus)
    api.state.chat_run_registry = registry
    api.state.storage = storage
    api.state.workspace_manager = FakeWorkspaceManager()
    api.state.resource_access_service = FakeAccess()
    api.include_router(deerflow_router)
    app = FastAPI()
    app.mount("/api", api)
    return app


def _parse_sse(text: str) -> list[tuple[str, str]]:
    """解析 SSE 文本（event:/data: 行对）为 [(event, data)] 列表。"""
    events: list[tuple[str, str]] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current = line[len("event: "):]
        elif line.startswith("data: ") and current:
            events.append((current, line[len("data: "):]))
    return events


def test_create_run_stream_echoes_human_message_first(monkeypatch) -> None:
    """SSE 首帧为 human messages 事件（id 非空），先于 metadata/end。

    后台任务（FakeChatService.run）以端点生成的真实 run_id 发布
    REPLY_START + REPLY_END，live 订阅收到后 end 帧自然收尾：httpx
    ASGITransport 必须等响应体完整读完——Starlette StreamingResponse
    的 ``listen_for_disconnect`` 后台任务 await receive()，而 ASGITransport
    的 receive 等 response_complete，中途 break 会造成死锁（真实 uvicorn
    无此限制）。
    """
    # agent_credential 无绑定 → 回退旧链路（避免测试真查数据库）
    async def _no_binding(agent_id: str):
        del agent_id
        return None

    monkeypatch.setattr(
        "bocomadp.deerflow.routers.deerflow_chat.get_agent_credential_id",
        _no_binding,
    )

    mgr = RunManager()

    async def scenario() -> list[tuple[str, str]]:
        bus = InMemoryMessageBus()
        registry = FakeChatRunRegistry()
        app = _make_app(mgr, bus, registry, FakeStorage())
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test") as client:
                payload = {
                    "agent_id": AGENT_ID,
                    "session_id": THREAD_ID,
                    "input": {
                        "messages": [
                            {"type": "human", "content": "德国的历史是什么？"},
                        ],
                    },
                }
                resp = await client.post(
                    f"/api/bocomadp/v1/threads/{THREAD_ID}/runs/stream",
                    json=payload,
                    headers={"X-User-ID": USER_ID},
                )
                return _parse_sse(resp.text) if resp.status_code == 200 else []
        finally:
            registry.cancel()

    events = asyncio.run(scenario())

    # 帧 1：用户输入回显（先于一切总线事件）
    assert events[0][0] == "messages"
    chunk, metadata = json.loads(events[0][1])
    assert chunk["type"] == "human"
    assert chunk["id"]  # 与 storage 持久化一致，刷新后去重不重复
    assert chunk["content"] == [
        {"type": "text", "text": "德国的历史是什么？"},
    ]
    assert metadata == {"langgraph_node": "user"}

    # 帧 2：metadata（REPLY_START 翻译，run_id 为端点生成的真实值）
    assert events[1][0] == "metadata"
    meta = json.loads(events[1][1])
    assert meta["run_id"]
    assert meta["thread_id"] == THREAD_ID

    # 帧 3：run 已结束 → end 收尾
    assert events[2] == ("end", "null")


def test_create_run_stream_emits_usage_and_values(monkeypatch) -> None:
    """token 下发通道：MODEL_CALL_END 消化后不泄漏 custom 帧，
    改为 REPLY_END 末尾 messages usage 增量帧 + end 前 values 快照
    （最后一条 ai 消息带 usage_metadata，对齐原生形态）。
    """
    async def _no_binding(agent_id: str):
        del agent_id
        return None

    monkeypatch.setattr(
        "bocomadp.deerflow.routers.deerflow_chat.get_agent_credential_id",
        _no_binding,
    )

    mgr = RunManager()

    async def scenario() -> list[tuple[str, str]]:
        bus = InMemoryMessageBus()
        registry = FakeChatRunRegistry()
        storage = FakeStorage()
        # 会话已有一轮历史：human + ai（values 快照的全量数据源）
        storage.seed(
            Msg(
                name="user",
                role="user",  # type: ignore[arg-type]
                content=[TextBlock(text="德国的历史是什么？")],
                id="human-1",
            ),
            Msg(
                name="agent_a",
                role="assistant",  # type: ignore[arg-type]
                content=[TextBlock(text="德国历史很长。")],
                id="ai-1",
            ),
        )
        chat_service = FakeChatService(bus, usage=(120, 45))
        app = _make_app(mgr, bus, registry, storage, chat_service)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test") as client:
                payload = {
                    "agent_id": AGENT_ID,
                    "session_id": THREAD_ID,
                    "input": {
                        "messages": [
                            {"type": "human", "content": "继续讲"},
                        ],
                    },
                }
                resp = await client.post(
                    f"/api/bocomadp/v1/threads/{THREAD_ID}/runs/stream",
                    json=payload,
                    headers={"X-User-ID": USER_ID},
                )
                return _parse_sse(resp.text) if resp.status_code == 200 else []
        finally:
            registry.cancel()

    events = asyncio.run(scenario())
    events_by_name = [e[0] for e in events]
    # MODEL_CALL_END 不再以 model_call_end custom 帧泄漏
    assert not any(
        name == "custom" and "model_call_end" in data
        for name, data in events
    )
    # 帧序：human 回显 → metadata → messages usage 增量 → values → end
    assert events_by_name == [
        "messages",
        "metadata",
        "messages",
        "values",
        "end",
    ]
    expected_usage = {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }
    # messages usage 增量帧（对齐原生 messages-tuple 最后一块）
    usage_chunk, usage_meta = json.loads(events[2][1])
    assert usage_chunk["type"] == "AIMessageChunk"
    assert usage_chunk["content"] == ""
    assert usage_chunk["id"] == "r1"
    assert usage_chunk["usage_metadata"] == expected_usage
    assert usage_meta == {"langgraph_node": "model"}
    # values 快照：全量消息 + title + 最后一条 ai 附 usage_metadata
    # （title 取 storage 内最近一条 human；请求输入尚未落库）
    values = json.loads(events[3][1])
    assert values["title"] == "德国的历史是什么？"
    assert [m["type"] for m in values["messages"]] == ["human", "ai"]
    assert values["messages"][-1]["id"] == "ai-1"
    assert values["messages"][-1]["usage_metadata"] == expected_usage
    # end 哨兵收尾
    assert events[4] == ("end", "null")


def test_join_run_stream_echoes_human_messages() -> None:
    """join 已有 run：SSE 回显 storage 中的用户消息（断线重连清理乐观消息）。"""
    mgr = RunManager()

    async def scenario() -> list[tuple[str, str]]:
        bus = InMemoryMessageBus()
        storage = FakeStorage()
        human = Msg(
            name="user",
            role="user",  # type: ignore[arg-type]
            content=[TextBlock(text="德国的历史是什么？")],
            id="human-1",
        )
        storage.seed(human)
        registry = FakeChatRunRegistry()  # 无活跃任务 → join 回放后立即收尾
        app = _make_app(mgr, bus, registry, storage)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    f"/api/bocomadp/v1/threads/{THREAD_ID}/runs/ghost-run/stream",
                    headers={"X-User-ID": USER_ID},
                )
                return _parse_sse(resp.text) if resp.status_code == 200 else []
        finally:
            registry.cancel()

    events = asyncio.run(scenario())

    # 帧 1：storage 里的用户消息回显（先于 end）
    assert events[0][0] == "messages"
    chunk, _ = json.loads(events[0][1])
    assert chunk == {
        "type": "human",
        "id": "human-1",
        "content": [{"type": "text", "text": "德国的历史是什么？"}],
    }

    # 帧 2：values 快照（end 哨兵前补发，对齐原生主通道帧）；
    # 无模型调用 → 无 usage_metadata
    assert events[1][0] == "values"
    values = json.loads(events[1][1])
    assert values["title"] == "德国的历史是什么？"
    assert values["messages"] == [
        {
            "type": "human",
            "id": "human-1",
            "content": [{"type": "text", "text": "德国的历史是什么？"}],
        },
    ]

    # 帧 3：run 已结束 → end 收尾
    assert events[2] == ("end", "null")
