# -*- coding: utf-8 -*-
"""跨用户知识空间引用：运行时分组合并 + 存储 CRUD + 路由 handler 测试。

设计见 docs/agent-cross-search-ref-design.md。覆盖：
- 运行时 ``search_cross_backend``：自己的空间 + 多个被引用者的空间按
  owner 分组各调一次网关、结果合并；
- 存储层 ``agent_cross_search_ref``：upsert（重复不报错）/ list（按 user+agent
  隔离）/ delete（按四列）/ sync（全量同步：upsert 入参 + 删多余）走内存替身，
  不依赖真实 MySQL；
- 路由 handler：GET 列表 / PUT 全量同步（增删改合一），以及智能体不存在 404。

pytest-asyncio 未安装：异步逻辑用 ``asyncio.run()`` 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from bocomadp import agent_cross_search_ref as ref_module
from bocomadp.agent_cross_search_config import AgentCrossSearchConfig
from bocomadp.agent_cross_search_ref import RefSpaceItem
from bocomadp.routers import agent_cross_search_refs as refs_module
from bocomadp.tools import cross_search


def _run(coro):
    return asyncio.run(coro)


async def _mock_resolve_ok(*a, **k):
    return object()


async def _mock_resolve_none(*a, **k):
    return None


# ── 存储层内存替身（模拟 agent_cross_search_refs 表）─────────────────


class _FakeResult:
    def __init__(self, rows: list[dict] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> "_FakeResult":
        return self

    def all(self) -> list[dict]:
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Table:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def upsert(self, p: dict) -> None:
        key = (
            p["user_id"],
            p["agent_id"],
            p["owner_user_code"],
            p["space_code"],
        )
        for r in self.rows:
            if (
                r["user_id"],
                r["agent_id"],
                r["owner_user_code"],
                r["space_code"],
            ) == key:
                r["created_at"] = p["ts"]
                r["space_name"] = p.get("space_name", "")
                return
        # INSERT 把 :ts 绑定到 created_at 列，替身按真实列名存
        self.rows.append(
            {
                "user_id": p["user_id"],
                "agent_id": p["agent_id"],
                "owner_user_code": p["owner_user_code"],
                "space_code": p["space_code"],
                "space_name": p.get("space_name", ""),
                "created_at": p["ts"],
            }
        )

    def select(self, p: dict) -> list[dict]:
        return [
            dict(r)
            for r in self.rows
            if r["user_id"] == p["user_id"] and r["agent_id"] == p["agent_id"]
        ]

    def delete(self, p: dict) -> int:
        before = len(self.rows)
        self.rows = [
            r
            for r in self.rows
            if not (
                r["user_id"] == p["user_id"]
                and r["agent_id"] == p["agent_id"]
                and r["owner_user_code"] == p["owner_user_code"]
                and r["space_code"] == p["space_code"]
            )
        ]
        return before - len(self.rows)

    def delete_extra(self, params: dict) -> int:
        keep = set()
        i = 0
        while f"o{i}" in params:
            keep.add((params[f"o{i}"], params[f"s{i}"]))
            i += 1
        before = len(self.rows)
        self.rows = [
            r
            for r in self.rows
            if not (
                r["user_id"] == params["user_id"]
                and r["agent_id"] == params["agent_id"]
                and (r["owner_user_code"], r["space_code"]) not in keep
            )
        ]
        return before - len(self.rows)

    def delete_all(self, params: dict) -> int:
        before = len(self.rows)
        self.rows = [
            r
            for r in self.rows
            if not (
                r["user_id"] == params["user_id"]
                and r["agent_id"] == params["agent_id"]
            )
        ]
        return before - len(self.rows)


class _FakeConn:
    def __init__(self, table: _Table) -> None:
        self._table = table

    async def execute(self, sql: Any, params: dict | None = None):
        params = params or {}
        sqltext = str(sql)
        if "INSERT INTO agent_cross_search_refs" in sqltext:
            self._table.upsert(params)
            return _FakeResult()
        if "DELETE FROM agent_cross_search_refs" in sqltext:
            if "o0" in params:
                return _FakeResult(rowcount=self._table.delete_extra(params))
            if "owner_user_code" in params:
                return _FakeResult(rowcount=self._table.delete(params))
            return _FakeResult(rowcount=self._table.delete_all(params))
        if "SELECT" in sqltext and "agent_cross_search_refs" in sqltext:
            return _FakeResult(rows=self._table.select(params))
        return _FakeResult()

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *a) -> bool:
        return False


class _FakeEngine:
    def __init__(self, table: _Table) -> None:
        self._conn = _FakeConn(table)

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


@pytest.fixture
def _fake_db(monkeypatch):
    table = _Table()
    engine = _FakeEngine(table)

    async def _get_engine():
        return engine

    monkeypatch.setattr(ref_module, "_get_engine", _get_engine)
    return table


# ── 运行时 mock httpx（不连真实网关 / config.yaml）─────────────────


_FAKE_CONFIG = SimpleNamespace(
    api_url="http://gw",
    caller="caller",
    timeout=5,
    headers={},
    user_role="",
    attach_flag="",
    rerank_flag="",
    rewrite_flag="",
    rerank_top_n="",
    rerank_rule_code="",
    qa_type="",
    vector_min_score=None,
    source_org_id_list=[],
    source_system_list=[],
    text_min_score=None,
    pub_time_start="",
    pub_time_end="",
    user_code="configUC",
    space_code_list=[],
    team_space_code_list=[],
    psnl_space_code_id="",
    psnl_category_id_list=[],
    search_type="0",
    customized_tag_list=[],
    text_top_n=None,
    vector_top_n=None,
)


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._p


class _CapturingClient:
    calls: list[tuple[str, list[str]]] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self) -> "_CapturingClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, url, headers=None, files=None):
        req = json.loads(files["REQ_MESSAGE"][1])
        param = req["REQ_BODY"]["param"]
        _CapturingClient.calls.append(
            (param["userCode"], param.get("spaceCodeList", []))
        )
        return _FakeResp(
            {
                "RSP_HEAD": {"TRAN_SUCCESS": "1"},
                "RSP_BODY": {
                    "result": {
                        "textGroupList": [
                            {
                                "content": f"uc={param['userCode']}|"
                                f"codes={param.get('spaceCodeList')}"
                            }
                        ]
                    }
                },
            }
        )


class _FakeHttpx:
    AsyncClient = _CapturingClient


def _install_fake_httpx(monkeypatch) -> None:
    monkeypatch.setattr(cross_search, "httpx", _FakeHttpx)
    monkeypatch.setattr(
        cross_search,
        "get_cross_search_config",
        lambda: _FAKE_CONFIG,
    )


# ── 存储层测试 ─────────────────────────────────────────────────────


def test_ref_upsert_dedup_and_list(_fake_db):
    uid, aid = "u1", "a1"
    _run(ref_module.cross_search_ref_upsert(uid, aid, "A", "SA", "场景A"))
    # 重复引用同一空间不报错（刷新 created_at / space_name）
    _run(ref_module.cross_search_ref_upsert(uid, aid, "A", "SA", "场景A改"))
    _run(ref_module.cross_search_ref_upsert(uid, aid, "B", "SB"))

    items = _run(ref_module.cross_search_ref_list(uid, aid))
    assert len(items) == 2
    assert sorted((i.owner_user_code, i.space_code) for i in items) == [
        ("A", "SA"),
        ("B", "SB"),
    ]
    by_key = {(i.owner_user_code, i.space_code): i for i in items}
    assert by_key[("A", "SA")].space_name == "场景A改"
    assert by_key[("B", "SB")].space_name == ""
    # 不同 user/agent 隔离
    assert _run(ref_module.cross_search_ref_list("uX", aid)) == []


def test_ref_sync_removes_extra(_fake_db):
    uid, aid = "u1", "a1"
    # 先有 A/B/C 三条
    for o, s in [("A", "SA"), ("B", "SB"), ("C", "SC")]:
        _run(ref_module.cross_search_ref_upsert(uid, aid, o, s))

    # 全量同步为 [A]：应删掉 B、C，保留 A（含空间名）
    _run(ref_module.cross_search_ref_sync(uid, aid, [("A", "SA", "场景A")]))
    items = _run(ref_module.cross_search_ref_list(uid, aid))
    assert [(i.owner_user_code, i.space_code, i.space_name) for i in items] == [
        ("A", "SA", "场景A")
    ]

    # 全量同步为空：清空
    _run(ref_module.cross_search_ref_sync(uid, aid, []))
    assert _run(ref_module.cross_search_ref_list(uid, aid)) == []


# ── 空间名称解析（网关 queryPageKnSpace.do）────────────────────────


class _SpaceQueryClient:
    """替身网关：校验报文 + 返回可配置的响应。"""

    calls: list[dict] = []
    payload: dict = {}
    raise_error: Exception | None = None

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self) -> "_SpaceQueryClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, url, headers=None, files=None):
        assert files["REQ_MESSAGE"][1]
        req = json.loads(files["REQ_MESSAGE"][1])
        _SpaceQueryClient.calls.append(req["REQ_BODY"]["param"])
        if _SpaceQueryClient.raise_error is not None:
            raise _SpaceQueryClient.raise_error
        return _FakeResp(_SpaceQueryClient.payload)


class _SpaceQueryHttpx:
    AsyncClient = _SpaceQueryClient


def _space_payload(pairs: list[tuple[str, str]]) -> dict:
    return {
        "RSP_HEAD": {"TRAN_SUCCESS": "1"},
        "RSP_BODY": {
            "result": {
                "total": len(pairs),
                "data": [
                    {
                        "roleInfo": None,
                        "spaceInfo": {
                            "id": 8000000 + i,
                            "spaceId": sid,
                            "spaceName": name,
                            "spaceCategory": 2,
                        },
                    }
                    for i, (sid, name) in enumerate(pairs)
                ],
            }
        },
    }


def test_fetch_space_names_parses_gateway(monkeypatch):
    monkeypatch.setattr(ref_module, "httpx", _SpaceQueryHttpx)
    _SpaceQueryClient.calls = []
    _SpaceQueryClient.payload = _space_payload(
        [("SP0999999", "虚拟空间转实体空间"), ("SP200144", "bge_m3模型空间")]
    )
    out = _run(ref_module.fetch_space_names("9875361"))
    assert out == {
        "SP0999999": "虚拟空间转实体空间",
        "SP200144": "bge_m3模型空间",
    }
    # 报文：按 owner 查、只查企业知识空间
    assert _SpaceQueryClient.calls[0]["userCode"] == "9875361"
    assert _SpaceQueryClient.calls[0]["spaceCategory"] == 2


def test_fetch_space_names_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(ref_module, "httpx", _SpaceQueryHttpx)
    _SpaceQueryClient.calls = []
    _SpaceQueryClient.raise_error = RuntimeError("network down")
    assert _run(ref_module.fetch_space_names("u1")) == {}

    _SpaceQueryClient.raise_error = None
    _SpaceQueryClient.payload = {"RSP_HEAD": {"TRAN_SUCCESS": "0"}}
    assert _run(ref_module.fetch_space_names("u1")) == {}

    _SpaceQueryClient.payload = {"RSP_HEAD": {"TRAN_SUCCESS": "1"}, "RSP_BODY": {}}
    assert _run(ref_module.fetch_space_names("u1")) == {}


def test_fetch_space_names_requires_api_url(monkeypatch):
    """网关地址未配置 → 抛错（配置缺失要暴露，不能静默留空名）。"""

    class _NoUrlConfig:
        api_url = ""
        timeout = 20.0

    monkeypatch.setattr(
        ref_module,
        "get_cross_search_space_config",
        lambda: _NoUrlConfig(),
    )
    with pytest.raises(ValueError):
        _run(ref_module.fetch_space_names("u1"))


# ── 运行时分组合并测试 ────────────────────────────────────────────


def test_search_groups_by_owner(monkeypatch):
    _install_fake_httpx(monkeypatch)
    _CapturingClient.calls = []
    ac = AgentCrossSearchConfig(user_code="selfUC", space_code_list=["S1"])
    refs = [
        RefSpaceItem(
            user_id="u",
            agent_id="a",
            owner_user_code="A",
            space_code="SA",
            created_at=datetime.now(),
        ),
        RefSpaceItem(
            user_id="u",
            agent_id="a",
            owner_user_code="B",
            space_code="SB",
            created_at=datetime.now(),
        ),
    ]
    out = _run(cross_search.search_cross_backend("kw", agent_config=ac, refs=refs))
    data = json.loads(out)
    # 自己的 + A 的 + B 的 = 三组，各调一次网关
    assert len(data) == 3
    assert _CapturingClient.calls[0] == ("selfUC", ["S1"])
    assert _CapturingClient.calls[1] == ("A", ["SA"])
    assert _CapturingClient.calls[2] == ("B", ["SB"])


def test_search_no_refs_single_call(monkeypatch):
    _install_fake_httpx(monkeypatch)
    _CapturingClient.calls = []
    ac = AgentCrossSearchConfig(user_code="selfUC", space_code_list=["S1"])
    _run(cross_search.search_cross_backend("kw", agent_config=ac))
    assert len(_CapturingClient.calls) == 1
    assert _CapturingClient.calls[0] == ("selfUC", ["S1"])


def test_search_refs_without_agent_config(monkeypatch):
    _install_fake_httpx(monkeypatch)
    _CapturingClient.calls = []
    refs = [
        RefSpaceItem(
            user_id="u",
            agent_id="a",
            owner_user_code="A",
            space_code="SA",
            created_at=datetime.now(),
        )
    ]
    _run(cross_search.search_cross_backend("kw", refs=refs))
    assert len(_CapturingClient.calls) == 1
    assert _CapturingClient.calls[0] == ("A", ["SA"])


# ── 路由 handler 测试 ─────────────────────────────────────────────


def test_router_list(_fake_db, monkeypatch):
    async def _list(uid, aid):
        return [
            RefSpaceItem(
                user_id=uid,
                agent_id=aid,
                owner_user_code="A",
                space_code="SA",
                space_name="场景A",
                created_at=datetime.now(),
            )
        ]

    monkeypatch.setattr(refs_module, "_resolve_agent", _mock_resolve_ok)
    monkeypatch.setattr(refs_module, "cross_search_ref_list", _list)
    req = SimpleNamespace()
    out = _run(refs_module.list_cross_search_refs("a1", request=req, user_id="u1"))
    assert len(out) == 1
    assert out[0]["owner_user_code"] == "A"
    assert out[0]["space_name"] == "场景A"
    assert "created_at" in out[0]


def test_router_put_sync(_fake_db, monkeypatch):
    captured = []

    async def _sync(uid, aid, refs):
        captured.append(list(refs))

    async def _list(uid, aid):
        return [
            RefSpaceItem(
                user_id=uid,
                agent_id=aid,
                owner_user_code="A",
                space_code="SA",
                space_name="场景A",
                created_at=datetime.now(),
            )
        ]

    async def _names(owner):
        return {"SA": "场景A"}

    monkeypatch.setattr(refs_module, "_resolve_agent", _mock_resolve_ok)
    monkeypatch.setattr(refs_module, "cross_search_ref_sync", _sync)
    monkeypatch.setattr(refs_module, "cross_search_ref_list", _list)
    monkeypatch.setattr(refs_module, "fetch_space_names", _names)
    req = SimpleNamespace()

    # 单条（数组长度 1）：网关命中 → 落库名字随行
    out = _run(
        refs_module.set_cross_search_refs(
            "a1",
            body=[refs_module.RefSpaceCreate(owner_user_code="A", space_code="SA")],
            request=req,
            user_id="u1",
        )
    )
    assert captured == [[("A", "SA", "场景A")]]
    # 返回同步后的全量 items（来自 mock list）
    assert out["items"][0]["owner_user_code"] == "A"
    assert out["items"][0]["space_name"] == "场景A"

    # 批量（数组长度 2）：SB 网关没查到 → 名字落空串
    _run(
        refs_module.set_cross_search_refs(
            "a1",
            body=[
                refs_module.RefSpaceCreate(owner_user_code="A", space_code="SA"),
                refs_module.RefSpaceCreate(owner_user_code="B", space_code="SB"),
            ],
            request=req,
            user_id="u1",
        )
    )
    assert captured[-1] == [("A", "SA", "场景A"), ("B", "SB", "")]

    # 空数组 = 清空（不触发网关查询）
    _run(
        refs_module.set_cross_search_refs(
            "a1", body=[], request=req, user_id="u1"
        )
    )
    assert captured[-1] == []


def test_router_404_when_agent_missing(monkeypatch):
    monkeypatch.setattr(refs_module, "_resolve_agent", _mock_resolve_none)
    req = SimpleNamespace()
    with pytest.raises(HTTPException) as exc:
        _run(refs_module.list_cross_search_refs("a1", request=req, user_id="u1"))
    assert exc.value.status_code == 404
