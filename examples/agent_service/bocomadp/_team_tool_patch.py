# -*- coding: utf-8 -*-
"""bocomadp 侧 TeamSay / AgentInvite 行为补丁（从 src 迁入）。

背景
----
src 侧 4 次 lrm 提交（e9e26f1/31f5276/31c4118/703bb38）里，
``_tool/_team_say.py`` 与 ``_tool/_agent_invite.py`` 内嵌了两段
"专家团"执行逻辑，现已按要求从 src 拆除（src 还原为 75d400d 原厂），
由本模块以**类级补丁**的方式迁入 bocomadp：

1. **TeamSay strict-workflow 白名单强制**（原 ``_team_say.py`` L304-339）：
   当 leader 的团队处于 ``collaboration_mode="workflow"`` 时，TeamSay
   只允许把消息发给 ``handoff_relations`` 配置的白名单成员，其余一律
   拦截并报错。
2. **AgentInvite legacy placeholder 迁移**（原 ``_agent_invite.py``
   L297-460）：历史 ``member_ids`` 数据经 ``_ensure_team_members``
   惰性迁移后会产生 ``role="created"`` 占位名册条目；邀请该 agent 时
   把占位替换为真正的 ``role="invited"`` 借用会话，并解绑占位 session
   的团队归属，避免旧 session 残留团队状态。

为什么是类级补丁
----------------
工具执行器（``agentscope/tool/_toolkit.py``）通过
``await tool_func(**kwargs)`` 调用工具，解析的是 **类** 上的
``__call__``（即 ``type(tool_func).__call__``）；在实例 ``__dict__``
里挂一个 ``__call__`` 属性不会生效，因此必须在类级别替换
``TeamSay.__call__`` 与 ``AgentInvite.__call__``。

兼容性
------
- **TeamSay**：用 ``getattr(self, "_allowed_handoff_targets", None)``
  读取白名单。只有 bocomadp 装配层赋值过该属性的实例（workflow 模式
  团队）才会触发强制逻辑；其余实例与 75d400d 原厂行为逐字一致。
- **AgentInvite**：正常场景（无 legacy ``role="created"`` 占位）与原厂
  行为一致；仅当名册存在占位条目时才走迁移路径。

安装幂等（``install_team_tool_patches`` 有模块级开关），由
``team_toolkit.patch_team_toolkit`` 在启动时调用。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agentscope._utils._common import _generate_id
from agentscope.app._bus_ops import enqueue_run_trigger
from agentscope.app._tool import AgentInvite, TeamSay
from agentscope.app._tool._agent_invite import (
    _display_handle,
    _display_name,
    _error,
    _resolve_target,
)
from agentscope.app._tool._constants import HANDLE_LEN
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import SessionConfig, TeamMember
from agentscope.app.storage._utils import _ensure_team_members
from agentscope.message import HintBlock, TextBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import ToolChunk

if TYPE_CHECKING:
    from agentscope.app._tool._team_tool_base import _TeamToolBase

logger = logging.getLogger("bocomadp.team_tool_patch")

__all__ = ["install_team_tool_patches"]

_installed: bool = False


async def patched_team_say_call(
    self: "_TeamToolBase",
    content: str,
    to: str | None = None,
) -> ToolChunk:
    """原厂 ``TeamSay.__call__`` + strict-workflow 白名单强制。

    迁移自 src ``_tool/_team_say.py``（HEAD 版 L122-394），唯一差异：
    白名单通过 ``getattr(self, "_allowed_handoff_targets", None)``
    读取，未赋值该属性的实例（非 workflow 模式）与原厂行为一致。
    """
    try:
        session = await self._storage.get_session(
            self._user_id,
            self._agent_id,
            self._session_id,
        )
        if session is None or session.team_id is None:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "TeamSay: this session is not in any "
                            "team — call TeamCreate first if you "
                            "want to start one."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        team = await self._storage.get_team(
            self._user_id,
            session.team_id,
        )
        if team is None:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"TeamSay: team {session.team_id} no longer "
                            f"exists."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        leader_session = await self._storage.get_session(
            self._user_id,
            "",
            team.session_id,
        )
        if leader_session is None:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"TeamSay: leader session "
                            f"{team.session_id} missing for team "
                            f"{team.id}."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        # 构建 (name -> (session_id, agent_id)) 目录：leader 恒用纯名字；
        # worker 来自名册（经 ``_ensure_team_members`` 惰性迁移 legacy
        # ``member_ids``）；invited 成员显示为 ``"<name>@<id[:8]>"``。
        leader_agent = await self._storage.get_agent(
            self._user_id,
            leader_session.agent_id,
        )
        leader_name = (
            leader_agent.data.name
            if leader_agent is not None
            else leader_session.agent_id
        )
        directory: dict[str, tuple[str, str]] = {
            leader_name: (leader_session.id, leader_session.agent_id),
        }
        members = await _ensure_team_members(
            self._storage,
            self._user_id,
            team,
        )
        for member in members:
            member_agent = await self._storage.get_agent(
                member.owner_id,
                member.agent_id,
            )
            if member_agent is None:
                continue
            if member.role == "invited":
                display = (
                    f"{member_agent.data.name}"
                    f"@{member.agent_id[:HANDLE_LEN]}"
                )
            else:
                display = member_agent.data.name
            directory[display] = (member.session_id, member.agent_id)

        own_session_ids = {sid for sid, _aid in directory.values()}
        if self._session_id not in own_session_ids:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"TeamSay: this session "
                            f"({self._session_id}) is not part of "
                            f"team {team.id}."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        if to is None:
            all_recipients = [
                (sid, aid)
                for sid, aid in directory.values()
                if sid != self._session_id
            ]
        else:
            resolved = directory.get(to)
            if resolved is None:
                known = sorted(directory.keys())
                return ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                f"TeamSay: no team member is named "
                                f"{to!r}. Known members: {known}."
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                )
            target_session_id, target_agent_id = resolved
            if target_session_id == self._session_id:
                return ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                "TeamSay: cannot send a message to "
                                "yourself; talk to yourself in your "
                                "own reasoning instead."
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                )
            all_recipients = [(target_session_id, target_agent_id)]

        # ------------------------------------------------------------------
        # Strict-workflow handoff enforcement（从 src _team_say.py 迁入）。
        # 当 ``_allowed_handoff_targets`` 非 None（workflow 模式团队），
        # 丢弃白名单之外的收件人；若全部被拦则报错并列出允许的成员名。
        # ------------------------------------------------------------------
        allowed_handoff_targets = getattr(
            self,
            "_allowed_handoff_targets",
            None,
        )
        recipients: list[tuple[str, str]]
        if allowed_handoff_targets is not None:
            recipients = [
                (sid, aid)
                for sid, aid in all_recipients
                if aid in allowed_handoff_targets
            ]
            if not recipients:
                allowed_names = [
                    n
                    for n, sid_aid in directory.items()
                    if sid_aid[1] in allowed_handoff_targets
                ]
                return ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                "TeamSay: strict workflow mode — you "
                                "may only communicate with these team "
                                f"members: {allowed_names}. "
                                "Use TeamSay to hand off to the next "
                                "member in the configured order."
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                )
        else:
            recipients = all_recipients

        # 解析发送者显示名，构造 HintBlock 并推送给每个收件人。
        sender_agent = await self._storage.get_agent(
            self._user_id,
            self._agent_id,
        )
        sender_name = (
            sender_agent.data.name
            if sender_agent is not None
            else self._agent_id
        )
        hint = HintBlock(
            hint=(
                f'<team-message from="{sender_name}">\n'
                f"{content}\n"
                f"</team-message>"
            ),
            source=json.dumps(
                {"label": "team", "sublabel": sender_name},
                ensure_ascii=False,
            ),
        )
        payload = hint.model_dump(mode="json")

        for sid, aid in recipients:
            await self._message_bus.queue_push(
                MessageBusKeys.inbox(sid),
                payload,
            )
            await enqueue_run_trigger(
                self._message_bus,
                user_id=self._user_id,
                session_id=sid,
                agent_id=aid,
            )

        count = len(recipients)
        target = "broadcast" if to is None else f"member {to!r}"
        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f"Delivered to {count} recipient(s) "
                        f"({target})."
                    ),
                ),
            ],
        )
    except Exception as e:  # pylint: disable=broad-except
        return ToolChunk(
            content=[TextBlock(text=f"TeamSay failed: {e}")],
            state=ToolResultState.ERROR,
        )


async def patched_agent_invite_call(
    self: "AgentInvite",
    target: str,
    prompt: str,
) -> ToolChunk:
    """原厂 ``AgentInvite.__call__`` + legacy placeholder 迁移。

    迁移自 src ``_tool/_agent_invite.py``（HEAD 版 L208-507）。与原厂
    （75d400d）的差异仅在 duplicate-borrow guard 与名册更新：当名册中
    存在 ``role="created"`` 占位条目（legacy ``member_ids`` 迁移产物）
    且 agent_id 匹配时，不拦截，而是把占位替换为真正的
    ``role="invited"`` 借用会话，并解绑占位 session 的团队归属。
    """
    try:
        invited, resolve_err = _resolve_target(
            self._pool_by_id,
            target,
        )
        if resolve_err is not None:
            return _error(resolve_err)
        assert invited is not None  # narrows for mypy

        session = await self._storage.get_session(
            self._user_id,
            self._agent_id,
            self._session_id,
        )
        if session is None or session.team_id is None:
            return _error(
                "AgentInvite: this session is not in any team — "
                "call TeamCreate first.",
            )
        team = await self._storage.get_team(
            self._user_id,
            session.team_id,
        )
        if team is None:
            return _error(
                f"AgentInvite: team {session.team_id} no longer "
                f"exists.",
            )
        if team.session_id != self._session_id:
            return _error(
                "AgentInvite: only the team leader can invite "
                "members; this session is a worker.",
            )

        # 重新拉取最新记录——快照可能因刚切换 invite 而过期。
        fresh = await self._storage.get_agent(
            self._user_id,
            invited.id,
        )
        if (
            fresh is None
            or not fresh.data.invite_config.invitable
            or not (
                fresh.data.invite_config.invite_description or ""
            ).strip()
        ):
            return _error(
                f"AgentInvite: agent {invited.data.name!r} is no "
                f"longer invitable.",
            )
        invited = fresh

        # ------------------------------------------------------------------
        # Duplicate-borrow guard（从 src _agent_invite.py 迁入）——
        # 一个团队一个 agent 只允许一个 LIVE 借用。``role="created"``
        # 名册条目是 legacy ``member_ids`` 迁移来的占位（指向成员 PRIMARY
        # session，可能残留旧团队状态），不算 LIVE 借用，下面会被新的
        # ``role="invited"`` 借用会话替换；只有已存在的 ``invited`` 条目
        # 才阻止重复邀请。
        # ------------------------------------------------------------------
        existing_members = await _ensure_team_members(
            self._storage,
            self._user_id,
            team,
        )
        placeholder = next(
            (
                m
                for m in existing_members
                if m.agent_id == invited.id and m.role == "created"
            ),
            None,
        )
        if any(
            m.agent_id == invited.id and m.role == "invited"
            for m in existing_members
        ):
            return _error(
                f"AgentInvite: agent {invited.data.name!r} is "
                f"already a member of team "
                f"{team.data.name!r}.",
            )

        # Leader session——chat-model / workspace 回退与初始消息署名需要。
        leader_session = await self._storage.get_session(
            self._user_id,
            "",
            team.session_id,
        )
        if leader_session is None:
            return _error(
                f"AgentInvite: leader session {team.session_id} "
                f"for team {team.id} is missing — team is in an "
                f"inconsistent state.",
            )
        leader_agent = await self._storage.get_agent(
            self._user_id,
            leader_session.agent_id,
        )
        leader_name = (
            leader_agent.data.name
            if leader_agent is not None
            else leader_session.agent_id
        )

        # 优先复用被邀请 agent 的主 session 的 workspace + chat-model；
        # 从未打开过的 agent 用新 workspace id + leader 的 chat-model
        # （workspace 由 workspace manager 首次对话时惰性创建）。
        invited_sessions = await self._storage.list_sessions(
            self._user_id,
            invited.id,
        )
        if invited_sessions:
            primary = invited_sessions[0]
            borrowed_workspace_id = primary.config.workspace_id
            borrowed_chat_model = (
                primary.config.chat_model_config
                or leader_session.config.chat_model_config
            )
            borrowed_fallback_model = (
                primary.config.fallback_chat_model_config
                or leader_session.config.fallback_chat_model_config
            )
        else:
            borrowed_workspace_id = (
                self._workspace_manager.assign_workspace_id(
                    user_id=self._user_id,
                    agent_id=invited.id,
                    session_id=_generate_id(),
                )
            )
            borrowed_chat_model = leader_session.config.chat_model_config
            borrowed_fallback_model = (
                leader_session.config.fallback_chat_model_config
            )

        # 权限上下文不继承 leader 的（working_directories / 规则锚定在
        # leader workspace，被邀请 agent 可能不共享）；也不继承被邀请
        # agent 主 session 的——团队会话是独立上下文。
        worker_state = AgentState()

        invited_display = _display_name(
            invited.data.name,
            invited.id,
        )
        invited_handle = _display_handle(invited.id)
        borrowed = await self._storage.upsert_session(
            user_id=self._user_id,
            agent_id=invited.id,
            config=SessionConfig(
                workspace_id=borrowed_workspace_id,
                name=f"team:{team.id}/invited:{invited_handle}",
                chat_model_config=borrowed_chat_model,
                fallback_chat_model_config=borrowed_fallback_model,
            ),
            state=worker_state,
        )
        await self._storage.set_session_team_id(
            self._user_id,
            borrowed.id,
            team.id,
        )

        if (
            placeholder is not None
            and placeholder.session_id != borrowed.id
        ):
            # 解绑占位的 PRIMARY session：它是作为 ``created`` 成员迁入、
            # 指向用户主会话的；被真正的 invited 会话取代后不能再绑在
            # 团队上，否则该会话仍会被解析为团队成员。
            await self._storage.set_session_team_id(
                self._user_id,
                placeholder.session_id,
                None,
            )

        team.data.members = [
            *[
                m
                for m in existing_members
                if not (
                    m.agent_id == invited.id and m.role == "created"
                )
            ],
            TeamMember(
                owner_id=self._user_id,
                agent_id=invited.id,
                session_id=borrowed.id,
                role="invited",
            ),
        ]
        await self._storage.upsert_team(self._user_id, team)

        hint = HintBlock(
            hint=(
                "<system-reminder>你已被邀请加入一个名为 "
                f"'{team.data.name}' 的团队，该团队由本会话中一个名为 "
                f"'{leader_name}' 的 agent 领导。所有团队成员"
                f"**只能**通过 `TeamSay` 工具进行沟通。"
                f"当你完成分配的任务，或想与领导或团队成员沟通时，"
                f"请使用 `TeamSay`。</system-reminder>\n"
                f'<team-message from="{leader_name}">\n'
                f"{prompt}\n"
                f"</team-message>"
            ),
            source=json.dumps(
                {
                    "label": "team_message",
                    "sublabel": leader_name,
                },
                ensure_ascii=False,
            ),
        )
        await self._message_bus.queue_push(
            MessageBusKeys.inbox(borrowed.id),
            hint.model_dump(mode="json"),
        )
        await enqueue_run_trigger(
            self._message_bus,
            user_id=self._user_id,
            session_id=borrowed.id,
            agent_id=invited.id,
        )

        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f"Invited {invited_display!r} into team "
                        f"{team.data.name!r}."
                    ),
                ),
            ],
        )
    except Exception as e:  # pylint: disable=broad-except
        return ToolChunk(
            content=[TextBlock(text=f"AgentInvite failed: {e}")],
            state=ToolResultState.ERROR,
        )


def install_team_tool_patches() -> None:
    """类级替换 ``TeamSay.__call__`` / ``AgentInvite.__call__``（幂等）。

    必须由 ``team_toolkit.patch_team_toolkit`` 在启动时调用；重复调用
    无副作用。
    """
    global _installed
    if _installed:
        return
    TeamSay.__call__ = patched_team_say_call  # type: ignore[method-assign]
    AgentInvite.__call__ = patched_agent_invite_call  # type: ignore[method-assign]
    _installed = True
    logger.info(
        "installed bocomadp TeamSay/AgentInvite __call__ patches "
        "(strict-workflow handoff + placeholder migration)",
    )
