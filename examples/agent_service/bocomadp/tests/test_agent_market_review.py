# -*- coding: utf-8 -*-
"""智能体市场发布审批流 + 审批人白名单 集成测试。

覆盖（走真实 HTTP / 真 SQLite storage，模式与 test_agent_market 一致；
pytest-asyncio 未安装，异步逻辑用 asyncio.run() 包裹）：

1. 审批人白名单（JSON 文件 + 接口管理，生效 = 文件 ∪ {锚点 sunw_94}）：
   - GET 公开（含锚点 source=builtin）；写操作非名单内 403；
   - POST 批量新增幂等（重复/锚点/空白项跳过）；PUT 全量覆盖；
   - DELETE：锚点 409、不存在 404、正常 204；
2. 发布审批流（agent_market_review，一智能体一记录 upsert）：
   - publish → pending（市场不可见）；重复 publish 幂等；
   - reject（理由必填 1~200）→ owned / 列表可见 review_reason；
   - 重新 publish 清空旧结论回 pending；
   - approve → 插名单行上架（市场可见）；已办结再审批 409；
   - 审批侧不校验白名单：普通用户也能过审批闸机；
     白名单管理接口仍仅名单内用户（403）；
3. GET /agent/owned 附带 publish_status / review_reason；
4. 删除智能体级联清审批记录（防幽灵待办条目）。

白名单文件通过环境变量 ``BOCOMADP_MARKET_REVIEWERS_FILE`` 指向
tmp_path（测试隔离；锚点是代码常量不受文件影响）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import create_app
from agentscope.app._router._agent import (
    agent_router as _framework_agent_router,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AgentData, AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import market_review_store, market_store, team_store
from bocomadp import market_reviewers
from bocomadp.routers.agent import agent_router
from bocomadp.routers.market import market_router

HDR_USER = {"X-User-ID": "test-user"}      # 普通用户（不在审批人名单）
HDR_ALICE = {"X-User-ID": "alice"}         # 另一个普通用户
HDR_REVIEWER = {"X-User-ID": "sunw_94"}    # 锚点账号（审批权+管理权恒在）


def _remove_framework_agent_routes(app) -> None:
    """摘除框架内置 /agent 路由（main.py / test_agent_market 同款）。"""
    fw_paths = {
        r.path
        for r in _framework_agent_router.routes
        if getattr(r, "path", "").startswith("/agent")
    }

    def _is_fw(r) -> bool:
        original = getattr(r, "original_router", None)
        if original is not None:
            return original is _framework_agent_router
        return getattr(r, "path", "") in fw_paths

    app.router.routes[:] = [r for r in app.router.routes if not _is_fw(r)]


def _run(coro):
    return asyncio.run(coro)


def _agent_data(name: str) -> AgentData:
    return AgentData(
        name=name,
        context_config=ContextConfig(),
        react_config=ReActConfig(),
    )


def _create(client, headers, name: str) -> str:
    resp = client.post("/agent/", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """sqlite 存储 + 三张 bocomadp 自建表 + 两个 router + 白名单指向 tmp。"""
    monkeypatch.setenv(
        "BOCOMADP_MARKET_REVIEWERS_FILE",
        str(tmp_path / "reviewers.json"),
    )
    # 重置进程内白名单状态（清空 + 加载本次测试的空文件 → 只剩锚点）
    market_reviewers.load_whitelist()

    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'review.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)
            await market_store.ensure_market_tables(storage)
            await market_review_store.ensure_review_tables(storage)

    _run(_provision())

    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    _remove_framework_agent_routes(app)
    app.include_router(agent_router)
    app.include_router(market_router)
    with TestClient(app) as test_client:
        yield test_client


#: 发布弹窗表单默认值（publish 必带；tag 即业务条线）
_PUB_BODY = {
    "department": "网络金融部",
    "system_name": "智能体平台",
    "tag": "智能研发",
    "description": "用于测试的智能体说明",
}


def _publish(client, headers, agent_id: str, **overrides) -> dict:
    body = {**_PUB_BODY, **overrides}
    resp = client.post(
        f"/agent/market/{agent_id}/publish", json=body, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1) 审批人白名单：查询 / 新增 / 覆盖 / 删除（生效名单 = 纯 JSON 文件
#    内容；种子名单仅文件缺失时首启播种；
#    种子与普通用户同权可删；末位保护 409）
# ---------------------------------------------------------------------------


def test_whitelist_get_public_and_seed_visible(client):
    """GET 公开：文件缺失 → 种子名单落盘生效（无 source 字段）。"""
    resp = client.get("/agent/market/reviewers", headers=HDR_USER)
    assert resp.status_code == 200
    assert resp.json() == {
        "reviewers": [
            {"user_id": "jiangchengwei"},
            {"user_id": "sunw_94"},
        ],
    }


def test_whitelist_write_requires_membership(client):
    """写操作仅名单内用户：普通用户 403（种子在名单内不受影响）。"""
    assert client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["r1"]},
        headers=HDR_USER,
    ).status_code == 403
    assert client.put(
        "/agent/market/reviewers",
        json={"user_ids": ["r1"]},
        headers=HDR_USER,
    ).status_code == 403
    assert client.delete(
        "/agent/market/reviewers/r1", headers=HDR_USER,
    ).status_code == 403
    # 名单没被动过
    assert [
        r["user_id"]
        for r in client.get(
            "/agent/market/reviewers", headers=HDR_USER,
        ).json()["reviewers"]
    ] == ["jiangchengwei", "sunw_94"]


def test_whitelist_add_idempotent(client):
    """POST 批量新增：幂等（重复/空白项跳过），新人立即获得管理权。"""
    resp = client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["lrm", " lrm ", ""]},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200
    assert [
        r["user_id"] for r in resp.json()["reviewers"]
    ] == ["jiangchengwei", "lrm", "sunw_94"]

    # 再加一次：幂等（不报错、不重复）
    resp = client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["lrm"]},
        headers=HDR_REVIEWER,
    )
    assert [
        r["user_id"] for r in resp.json()["reviewers"]
    ] == ["jiangchengwei", "lrm", "sunw_94"]

    # 新增的审批人立即获得管理权
    assert client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["r2"]},
        headers={"X-User-ID": "lrm"},
    ).status_code == 200


def test_whitelist_delete_seed_and_last_one_protection(client):
    """DELETE：不在名单 404、种子可删（与普通用户同权）、末位 409。"""
    # 不在名单（从未加过）：sunw_94 还在名单内，操作合法
    assert client.delete(
        "/agent/market/reviewers/nobody", headers=HDR_REVIEWER,
    ).status_code == 404
    # 种子 sunw_94 可删（还有 jiangchengwei 在，不触发末位保护）
    assert client.delete(
        "/agent/market/reviewers/sunw_94", headers=HDR_REVIEWER,
    ).status_code == 204
    # 删到只剩一人（操作者换成 jiangchengwei——sunw_94 已无管理权）
    hdr = {"X-User-ID": "jiangchengwei"}
    assert client.put(
        "/agent/market/reviewers",
        json={"user_ids": ["jiangchengwei"]},
        headers=hdr,
    ).status_code == 200
    # 删最后一个审批人 → 409（末位保护）
    resp = client.delete("/agent/market/reviewers/jiangchengwei", headers=hdr)
    assert resp.status_code == 409
    assert "至少保留一个审批人" in resp.json()["detail"]
    assert [
        r["user_id"]
        for r in client.get(
            "/agent/market/reviewers", headers=hdr,
        ).json()["reviewers"]
    ] == ["jiangchengwei"]


def test_whitelist_put_overwrite_empty_409(client):
    """PUT 全量覆盖：传空 → 409 末位保护；正常覆盖生效。"""
    resp = client.put(
        "/agent/market/reviewers", json={"user_ids": []}, headers=HDR_REVIEWER,
    )
    assert resp.status_code == 409
    assert "至少保留一个审批人" in resp.json()["detail"]
    # 名单原封不动
    assert [
        r["user_id"]
        for r in client.get(
            "/agent/market/reviewers", headers=HDR_REVIEWER,
        ).json()["reviewers"]
    ] == ["jiangchengwei", "sunw_94"]

    # 正常覆盖
    resp = client.put(
        "/agent/market/reviewers", json={"user_ids": ["lrm"]}, headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200
    assert [
        r["user_id"] for r in resp.json()["reviewers"]
    ] == ["lrm"]


# ---------------------------------------------------------------------------
# 2) 发布审批流
# ---------------------------------------------------------------------------


def test_publish_reject_republish_approve_flow(client):
    """核心状态机：pending → rejected（带理由）→ 重新 publish（清旧
    结论）→ approved（上架市场）。"""
    agent_id = _create(client, HDR_ALICE, "周报生成器")

    # 发布 → pending，市场不可见；发布档案随申请落库（回显）
    body = _publish(client, HDR_ALICE, agent_id)
    assert body["status"] == "pending"
    assert body["department"] == "网络金融部"
    assert body["system_name"] == "智能体平台"
    assert body["tag"] == "智能研发"
    assert body["description"] == "用于测试的智能体说明"
    assert agent_id not in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }

    # 白名单已临时放开：普通用户也能过审批闸机（空理由 422 在改
    # 状态之前触发，验证闸机放行且不污染后续状态机）
    assert client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={"reason": ""},
        headers=HDR_USER,
    ).status_code == 422

    # 拒绝理由必填（422）与限长（422）
    assert client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={},
        headers=HDR_REVIEWER,
    ).status_code == 422
    assert client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={"reason": ""},
        headers=HDR_REVIEWER,
    ).status_code == 422
    assert client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={"reason": "x" * 201},
        headers=HDR_REVIEWER,
    ).status_code == 422

    # 拒绝 → rejected + 理由落库
    resp = client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={"reason": "提示词涉及未授权数据源"},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "提示词涉及未授权数据源"
    assert body["reviewer"] == "sunw_94"
    assert body["reviewed_at"] is not None

    # 列表可见拒绝理由与审批人（owner 视角走 GET /agent/owned）
    owned = client.get("/agent/owned", headers=HDR_ALICE).json()
    item = next(a for a in owned["agents"] if a["id"] == agent_id)
    assert item["publish_status"] == "rejected"
    assert item["review_reason"] == "提示词涉及未授权数据源"

    # 重新发布：清空旧结论回 pending
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json=_PUB_BODY,
        headers=HDR_ALICE,
    )
    body = resp.json()
    assert body["status"] == "pending"
    assert body["reason"] == ""
    assert body["reviewer"] == ""
    assert body["reviewed_at"] is None

    # 通过 → 上架，市场可见；列表 publish_status 恒为 approved
    resp = client.post(
        f"/agent/market/reviews/{agent_id}/approve", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert agent_id in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }
    listed = client.get("/agent/", headers=HDR_ALICE).json()
    item = next(a for a in listed["agents"] if a["id"] == agent_id)
    assert item["publish_status"] == "approved"
    assert item["reviewer"] == "sunw_94"
    assert item["reviewed_at"] is not None
    # review_reason = 审批结论：已办结就有值。本用例 approve 没传 body
    # （意见选填），所以是空串；前端按 publish_status 决定显示什么文案
    assert item["review_reason"] == ""

    # 已上架再 publish：幂等 approved（无"变更重审"概念）
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json=_PUB_BODY,
        headers=HDR_ALICE,
    )
    assert resp.json()["status"] == "approved"

    # 已办结再审批 → 409
    assert client.post(
        f"/agent/market/reviews/{agent_id}/approve", headers=HDR_REVIEWER,
    ).status_code == 409


def test_publish_status_not_submitted_and_review_404(client):
    """从未提交：列表 publish_status 为 not_submitted；审批接口 404。"""
    agent_id = _create(client, HDR_ALICE, "从未发布")

    listed = client.get("/agent/", headers=HDR_ALICE).json()
    item = next(a for a in listed["agents"] if a["id"] == agent_id)
    assert item["publish_status"] == "not_submitted"
    assert item["review_reason"] is None
    assert item["reviewer"] == ""

    assert client.post(
        f"/agent/market/reviews/{agent_id}/approve", headers=HDR_REVIEWER,
    ).status_code == 404
    assert client.post(
        f"/agent/market/reviews/{agent_id}/reject",
        json={"reason": "x"},
        headers=HDR_REVIEWER,
    ).status_code == 404


def test_publish_form_profile_lifecycle(client):
    """发布档案（部门/系统/业务条线/说明）：必填校验 + 全链路透传。

    publish 落库 → 审批列表可见 → approve 复制进市场行 → 市场列表
    展示；rejected 重新 publish 覆盖档案（旧档案靠列表回显，不自动沿用）。
    """
    # 必填校验：缺部门 / 缺说明 → 422（pydantic 层拦截）
    agent_id = _create(client, HDR_ALICE, "档案全流程")
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json={k: v for k, v in _PUB_BODY.items() if k != "department"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json={k: v for k, v in _PUB_BODY.items() if k != "description"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 422

    # 缺系统同样 422
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json={k: v for k, v in _PUB_BODY.items() if k != "system_name"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 422

    # tag（业务条线）必填：不传 / 空串 / 纯空白 → 422
    resp = client.post(
        f"/agent/market/{agent_id}/publish",
        json={k: v for k, v in _PUB_BODY.items() if k != "tag"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 422
    for blank in ("", "   "):
        resp = client.post(
            f"/agent/market/{agent_id}/publish",
            json={**_PUB_BODY, "tag": blank},
            headers=HDR_ALICE,
        )
        assert resp.status_code == 422

    # 重新提交带业务条线：pending 状态下档案被覆盖为最新表单值
    body = _publish(client, HDR_ALICE, agent_id, tag="风险防控")
    assert body["status"] == "pending"
    assert body["tag"] == "风险防控"

    # 审批列表条目带档案（审批人据此判断批不批）
    resp = client.get("/agent/market/reviews", headers=HDR_REVIEWER)
    item = next(
        r for r in resp.json()["reviews"] if r["agent_id"] == agent_id
    )
    assert item["department"] == "网络金融部"
    assert item["system_name"] == "智能体平台"
    assert item["tag"] == "风险防控"
    assert item["description"] == "用于测试的智能体说明"

    # approve 上架 → 档案复制进市场行，市场列表直接展示（tag 亦然）
    assert client.post(
        f"/agent/market/reviews/{agent_id}/approve", headers=HDR_REVIEWER,
    ).status_code == 200
    resp = client.get("/agent/market")
    agent = next(a for a in resp.json()["agents"] if a["id"] == agent_id)
    assert agent["department"] == "网络金融部"
    assert agent["system_name"] == "智能体平台"
    assert agent["tag"] == "风险防控"
    assert agent["description"] == "用于测试的智能体说明"

    # 业务条线即市场标签：现有打标/撕标接口直接可用
    resp = client.put(
        f"/agent/market/{agent_id}", json={"tag": "智慧办公"}, headers=HDR_USER,
    )
    assert resp.status_code == 200
    resp = client.get("/agent/market", params={"tag": "智慧办公"})
    assert [a["id"] for a in resp.json()["agents"]] == [agent_id]


def test_reviewer_can_review_own_application(client):
    """审批人审自己的申请：允许（审计留痕兜底）。"""
    agent_id = _create(client, HDR_REVIEWER, "锚点自建")
    _publish(client, HDR_REVIEWER, agent_id)
    resp = client.post(
        f"/agent/market/reviews/{agent_id}/approve", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200
    assert agent_id in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }


def test_unpublish_pending_withdraws_application(client):
    """pending 状态 unpublish = 撤回申请（审批行删除，回未提交）。"""
    agent_id = _create(client, HDR_ALICE, "撤回申请")
    _publish(client, HDR_ALICE, agent_id)

    assert client.post(
        f"/agent/market/{agent_id}/unpublish", headers=HDR_ALICE,
    ).status_code == 204
    review = _run(
        market_review_store.get_review_record(client.app.state.storage, agent_id),
    )
    assert review is None
    listed = client.get("/agent/", headers=HDR_ALICE).json()
    item = next(a for a in listed["agents"] if a["id"] == agent_id)
    assert item["publish_status"] == "not_submitted"
    assert item["review_reason"] is None


# ---------------------------------------------------------------------------
# 3) 审批列表（GET /agent/market/reviews）
# ---------------------------------------------------------------------------


def test_reviews_list_filters_and_content(client):
    """默认待办（申请时间正序）；status 查已办；带智能体名称/提示词。"""
    a1 = _create(client, HDR_ALICE, "智能体一")
    a2 = _create(client, HDR_USER, "智能体二")
    _publish(client, HDR_ALICE, a1)
    _publish(client, HDR_USER, a2)

    # 白名单已临时放开：普通用户也能看审批列表
    assert client.get("/agent/market/reviews", headers=HDR_USER).status_code == 200

    # 待办：两条都在，按申请时间正序（先到先审）
    resp = client.get("/agent/market/reviews", headers=HDR_REVIEWER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    # 两条申请可能落在同一秒，排序先后不稳定，按集合断言
    assert {r["agent_id"] for r in body["reviews"]} == {a1, a2}
    first = next(r for r in body["reviews"] if r["agent_id"] == a1)
    assert first["name"] == "智能体一"
    assert first["applicant"] == "alice"
    assert first["status"] == "pending"
    assert first["reviewer"] == ""

    # 拒绝一条后：待办剩一条；rejected 已办带理由与审批人
    assert client.post(
        f"/agent/market/reviews/{a1}/reject",
        json={"reason": "内容待改"},
        headers=HDR_REVIEWER,
    ).status_code == 200
    # status 不传 = 全部：驳回的那条仍在（total 2），只是不再是待办
    assert client.get(
        "/agent/market/reviews", headers=HDR_REVIEWER,
    ).json()["total"] == 2
    assert client.get(
        "/agent/market/reviews",
        params={"status": "pending"},
        headers=HDR_REVIEWER,
    ).json()["total"] == 1
    resp = client.get(
        "/agent/market/reviews",
        params={"status": "rejected"},
        headers=HDR_REVIEWER,
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["reviews"][0]["agent_id"] == a1
    assert body["reviews"][0]["reason"] == "内容待改"
    assert body["reviews"][0]["reviewer"] == "sunw_94"

    # 非法 status → 422
    assert client.get(
        "/agent/market/reviews",
        params={"status": "bogus"},
        headers=HDR_REVIEWER,
    ).status_code == 422


# ---------------------------------------------------------------------------
# 4) GET /agent/owned 附带发布状态
# ---------------------------------------------------------------------------


def test_pending_edit_refreshes_name_but_freezes_archive(client):
    """待审期间编辑智能体：名称/提示词实时变，发布档案定格不变。

    名称与提示词不落审批表，每次查询从 agents 表现取（审核人看到的
    永远是提交后的最新内容）；发布档案存在审批表里，编辑智能体不会
    改它——只有重新点发布（提交新表单）才覆盖。
    """
    agent_id = _create(client, HDR_ALICE, "原名")
    _publish(client, HDR_ALICE, agent_id)

    assert client.patch(
        f"/agent/{agent_id}",
        json={"name": "改名后", "system_prompt": "改过的提示词"},
        headers=HDR_ALICE,
    ).status_code == 200

    # 审核侧：名称 / 提示词实时
    detail = client.get(
        f"/agent/market/reviews/{agent_id}", headers=HDR_REVIEWER,
    ).json()
    assert detail["name"] == "改名后"
    assert detail["system_prompt"] == "改过的提示词"
    # 发布档案定格：仍是提交时填的表单原值
    assert detail["department"] == _PUB_BODY["department"]
    assert detail["system_name"] == _PUB_BODY["system_name"]
    assert detail["tag"] == _PUB_BODY["tag"]
    assert detail["description"] == _PUB_BODY["description"]

    # 智能体列表同样：publish_info 不受编辑影响，状态仍是 pending
    listed = client.get("/agent/", headers=HDR_ALICE).json()
    item = next(a for a in listed["agents"] if a["id"] == agent_id)
    assert item["publish_status"] == "pending"
    assert item["publish_info"]["department"] == _PUB_BODY["department"]
    assert item["publish_info"]["tag"] == _PUB_BODY["tag"]


def test_owned_carries_publish_status(client):
    """owned 列表每项带 publish_status；rejected 附 review_reason。"""
    pending_id = _create(client, HDR_ALICE, "待审中")
    rejected_id = _create(client, HDR_ALICE, "被拒了")
    untouched_id = _create(client, HDR_ALICE, "没提交过")
    _publish(client, HDR_ALICE, pending_id)
    _publish(client, HDR_ALICE, rejected_id)
    assert client.post(
        f"/agent/market/reviews/{rejected_id}/reject",
        json={"reason": "重写提示词"},
        headers=HDR_REVIEWER,
    ).status_code == 200

    resp = client.get("/agent/owned", headers=HDR_ALICE)
    assert resp.status_code == 200
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[pending_id]["publish_status"] == "pending"
    assert by_id[pending_id]["review_reason"] is None
    assert by_id[pending_id]["reviewer"] == ""
    assert by_id[pending_id]["reviewed_at"] is None
    # 待审条目也回显发布档案（驳回后重新发布可回填弹窗）
    assert by_id[pending_id]["publish_info"]["department"] == "网络金融部"
    assert by_id[pending_id]["publish_info"]["tag"] == "智能研发"
    assert by_id[rejected_id]["publish_status"] == "rejected"
    assert by_id[rejected_id]["review_reason"] == "重写提示词"
    assert by_id[rejected_id]["reviewer"] == "sunw_94"
    assert by_id[rejected_id]["reviewed_at"] is not None
    assert by_id[untouched_id]["publish_status"] == "not_submitted"
    assert by_id[untouched_id]["publish_info"]["department"] == ""
    assert by_id[untouched_id]["reviewer"] == ""

    # 平台内置手动上架的智能体（在市场但无审批记录）→ approved
    platform_id = _create(client, HDR_REVIEWER, "平台内置")
    assert _run(
        market_store.insert_market_entry(client.app.state.storage, platform_id),
    ) is True
    resp = client.get("/agent/owned", headers=HDR_REVIEWER)
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[platform_id]["publish_status"] == "approved"


# ---------------------------------------------------------------------------
# 5) 删除智能体级联清审批记录
# ---------------------------------------------------------------------------


def test_delete_agent_cascades_review_record(client):
    """删智能体 → pending 审批行跟着删（审批列表不出现幽灵条目）。"""
    agent_id = _create(client, HDR_ALICE, "待删")
    _publish(client, HDR_ALICE, agent_id)
    assert client.get(
        "/agent/market/reviews", headers=HDR_REVIEWER,
    ).json()["total"] == 1

    assert client.delete(f"/agent/{agent_id}", headers=HDR_ALICE).status_code == 204

    review = _run(
        market_review_store.get_review_record(client.app.state.storage, agent_id),
    )
    assert review is None
    assert client.get(
        "/agent/market/reviews", headers=HDR_REVIEWER,
    ).json()["total"] == 0
