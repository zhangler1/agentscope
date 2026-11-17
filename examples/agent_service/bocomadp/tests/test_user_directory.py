# -*- coding: utf-8 -*-
"""OA 用户信息查询（ap_user）：存储层过滤/字段映射 + 路由 handler 测试。

覆盖：
- 存储层 ``query_users_by_login_name``：命中返回 5 个字段、失效/非生效
  用户（USER_STATUS<>'1'）被过滤、同登录名多 USER_ID 返回多行、
  未命中返回空列表；
- ``_to_int``：USER_ID / ORG_ID 是 decimal(10,0)，统一转 int；
- 路由 handler ``query_user_info``：响应结构、查询失败转 500。

用 SQLite 内存库跑真实 SQL（aiosqlite），不连任何外部库；
pytest-asyncio 未安装：异步逻辑用 ``asyncio.run()`` 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from bocomadp import user_directory
from bocomadp.routers import user_info as user_info_module

# 只建被查询的列 + 状态列（单元测试用精简表；生产 DDL 以 OA 侧为准）
_CREATE_TABLE_SQL = (
    "CREATE TABLE ap_user ("
    "USER_ID DECIMAL(10,0) NOT NULL, "
    "ORG_ID DECIMAL(10,0) DEFAULT NULL, "
    "USER_CODE CHAR(7) DEFAULT NULL, "
    "LOGIN_NAME VARCHAR(30) DEFAULT NULL, "
    "USER_NAME VARCHAR(100) DEFAULT NULL, "
    "USER_STATUS VARCHAR(4) DEFAULT NULL, "
    "PRIMARY KEY (USER_ID)"
    ")"
)

_INSERT_SQL = (
    "INSERT INTO ap_user "
    "(USER_ID, ORG_ID, USER_CODE, LOGIN_NAME, USER_NAME, USER_STATUS) "
    "VALUES (:USER_ID, :ORG_ID, :USER_CODE, :LOGIN_NAME, :USER_NAME, "
    ":USER_STATUS)"
)

#: 种子数据：lrm 生效且有两条（不同 USER_ID）；zhangsan 失效(0)；
#: lisi 生效(1)；wangwu 休假(3) 用于验证非生效被过滤
_SEED_ROWS: list[dict[str, Any]] = [
    {
        "USER_ID": 10001,
        "ORG_ID": 900,
        "USER_CODE": "E0001",
        "LOGIN_NAME": "lrm",
        "USER_NAME": "林若敏",
        "USER_STATUS": "1",
    },
    {
        "USER_ID": 10002,
        "ORG_ID": 902,
        "USER_CODE": "E0002",
        "LOGIN_NAME": "zhangsan",
        "USER_NAME": "张三",
        "USER_STATUS": "0",
    },
    {
        "USER_ID": 20001,
        "ORG_ID": 901,
        "USER_CODE": "E0001",
        "LOGIN_NAME": "lrm",
        "USER_NAME": "林若敏",
        "USER_STATUS": "1",
    },
    {
        "USER_ID": 10003,
        "ORG_ID": 903,
        "USER_CODE": "E0003",
        "LOGIN_NAME": "lisi",
        "USER_NAME": "李四",
        "USER_STATUS": "1",
    },
    {
        "USER_ID": 10004,
        "ORG_ID": 904,
        "USER_CODE": "E0004",
        "LOGIN_NAME": "wangwu",
        "USER_NAME": "王五",
        "USER_STATUS": "3",
    },
]


def _run(coro):
    return asyncio.run(coro)


async def _seed_and_query(
    login_name: str,
    seed_rows: list[dict[str, Any]],
) -> list[user_directory.UserInfoRow]:
    """建表 + 灌数据 + 查询，全程在同一个事件循环里（aiosqlite 绑定 loop）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(user_directory, "_engine", engine)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(_CREATE_TABLE_SQL))
                for row in seed_rows:
                    await conn.execute(text(_INSERT_SQL), row)
            return await user_directory.query_users_by_login_name(login_name)
        finally:
            await engine.dispose()


# ── 存储层 ──────────────────────────────────────────────────────────


def test_query_returns_five_fields():
    """命中时返回 USER_ID / ORG_ID / USER_CODE / LOGIN_NAME / USER_NAME。"""
    rows = _run(_seed_and_query("lrm", _SEED_ROWS))
    assert len(rows) == 2
    first = rows[0].model_dump()
    assert first == {
        "USER_ID": 10001,
        "ORG_ID": 900,
        "USER_CODE": "E0001",
        "LOGIN_NAME": "lrm",
        "USER_NAME": "林若敏",
    }
    # 同名不同 APP_ID 的第二条也在结果里
    assert rows[1].USER_ID == 20001
    assert rows[1].ORG_ID == 901


def test_inactive_user_is_filtered():
    """CELL_PHONE='0'（失效）的用户不出现在结果里。"""
    rows = _run(_seed_and_query("zhangsan", _SEED_ROWS))
    assert rows == []


def test_not_found_returns_empty_list():
    """查不到人返回空列表（不是异常）。"""
    rows = _run(_seed_and_query("nobody", _SEED_ROWS))
    assert rows == []


def test_non_active_status_filtered():
    """USER_STATUS='0'（失效）过滤掉；其余状态（如休假 3）仍返回。"""
    rows = _run(_seed_and_query("zhangsan", _SEED_ROWS))
    assert rows == []
    rows = _run(_seed_and_query("wangwu", _SEED_ROWS))
    assert len(rows) == 1
    assert rows[0].USER_ID == 10004


def test_login_name_is_case_sensitive():
    """OA 值精准匹配（区分大小写）：库里是 'lrm'，查 'LRM' 不命中。"""
    rows = _run(_seed_and_query("LRM", _SEED_ROWS))
    assert rows == []


def test_to_int_handles_decimal_and_none():
    """USER_ID / ORG_ID 是 decimal，统一转 int；空值保持 None。"""
    assert user_directory._to_int(Decimal("10001")) == 10001
    assert user_directory._to_int(10001) == 10001
    assert user_directory._to_int("10001") == 10001
    assert user_directory._to_int(None) is None
    assert user_directory._to_int("abc") is None


# ── 路由 handler ────────────────────────────────────────────────────


def test_router_returns_items(monkeypatch):
    """路由返回 {total, items[]}，字段名与表列名一致。"""

    async def _query(login_name, app_id=None):
        assert login_name == "lrm"
        assert app_id is None
        return [
            user_directory.UserInfoRow(
                USER_ID=10001,
                ORG_ID=900,
                USER_CODE="E0001",
                LOGIN_NAME="lrm",
                USER_NAME="林若敏",
            ),
        ]

    monkeypatch.setattr(
        user_info_module,
        "query_users_by_login_name",
        _query,
    )
    req = SimpleNamespace(query_params={})
    out = _run(
        user_info_module.query_user_info(
            request=req,
            oa="lrm",
            OA=None,

            user_id="u1",
        ),
    )
    assert out["total"] == 1
    assert out["items"] == [
        {
            "USER_ID": 10001,
            "ORG_ID": 900,
            "USER_CODE": "E0001",
            "LOGIN_NAME": "lrm",
            "USER_NAME": "林若敏",
        },
    ]


def test_router_empty_result(monkeypatch):
    """查不到人返回 200 + total=0 + items=[]。"""

    async def _query(login_name, app_id=None):
        return []

    monkeypatch.setattr(
        user_info_module,
        "query_users_by_login_name",
        _query,
    )
    out = _run(
        user_info_module.query_user_info(
            request=SimpleNamespace(query_params={}),
            oa="lrm",
            OA=None,

            user_id="u1",
        ),
    )
    assert out == {"total": 0, "items": []}


def test_router_returns_500_on_db_error(monkeypatch):
    """库异常（表不存在等）转成 500，不把底层错误抛给前端。"""

    async def _query(login_name, app_id=None):
        raise SQLAlchemyError("Table 'ap_user' doesn't exist")

    monkeypatch.setattr(
        user_info_module,
        "query_users_by_login_name",
        _query,
    )
    with pytest.raises(HTTPException) as exc:
        _run(
            user_info_module.query_user_info(
                request=SimpleNamespace(query_params={}),
                oa="lrm",
                OA=None,
    
                user_id="u1",
            ),
        )
    assert exc.value.status_code == 500
    assert "用户信息查询失败" in str(exc.value.detail)


# ── 连接复用：绑定框架 storage 后走同一连接池 ──────────────────────


def test_bind_storage_prefers_framework_engine():
    """绑定框架 storage 后查询用 storage 的引擎（不另开连接池）。"""
    fake_engine = object()

    async def _scenario():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(user_directory, "_engine", None)
            mp.setattr(user_directory, "_fallback_engine", None)
            user_directory.bind_storage(SimpleNamespace(_engine=fake_engine))
            return await user_directory._get_engine()

    assert _run(_scenario()) is fake_engine


def test_bind_storage_without_engine_keeps_previous():
    """storage 没有 _engine 时不覆盖已有绑定（保持原引擎可用）。"""

    async def _scenario():
        with pytest.MonkeyPatch.context() as mp:
            fake_engine = object()
            mp.setattr(user_directory, "_engine", fake_engine)
            user_directory.bind_storage(SimpleNamespace(_engine=None))
            return await user_directory._get_engine()

    assert _run(_scenario()) is not None


# ── 参数名大小写兼容 ────────────────────────────────────────────────


def test_router_accepts_uppercase_oa(monkeypatch):
    """大写 OA 参数与 oa 等价。"""
    captured: dict[str, Any] = {}

    async def _query(login_name, app_id=None):
        captured["login_name"] = login_name
        return [user_directory.UserInfoRow(USER_ID=1, LOGIN_NAME=login_name)]

    monkeypatch.setattr(
        user_info_module,
        "query_users_by_login_name",
        _query,
    )
    out = _run(
        user_info_module.query_user_info(
            request=SimpleNamespace(query_params={}),
            oa=None,
            OA="lrm",

            user_id="u1",
        ),
    )
    assert captured["login_name"] == "lrm"
    assert out["total"] == 1


def test_router_accepts_any_case_variant(monkeypatch):
    """查询参数名任意大小写变体（Oa / oA ...）都能识别。"""

    async def _query(login_name, app_id=None):
        return [user_directory.UserInfoRow(USER_ID=1, LOGIN_NAME=login_name)]

    monkeypatch.setattr(
        user_info_module,
        "query_users_by_login_name",
        _query,
    )
    out = _run(
        user_info_module.query_user_info(
            request=SimpleNamespace(query_params={"Oa": "lrm"}),
            oa=None,
            OA=None,

            user_id="u1",
        ),
    )
    assert out["total"] == 1


def test_router_requires_login_name():
    """oa / OA 都没传 → 422。"""
    with pytest.raises(HTTPException) as exc:
        _run(
            user_info_module.query_user_info(
                request=SimpleNamespace(query_params={}),
                oa=None,
                OA=None,
    
                user_id="u1",
            ),
        )
    assert exc.value.status_code == 422


def test_router_rejects_too_long_login_name():
    """登录名超长 → 422。"""
    with pytest.raises(HTTPException) as exc:
        _run(
            user_info_module.query_user_info(
                request=SimpleNamespace(query_params={}),
                oa="x" * 31,
                OA=None,
    
                user_id="u1",
            ),
        )
    assert exc.value.status_code == 422
