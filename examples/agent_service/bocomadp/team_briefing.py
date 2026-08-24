# -*- coding: utf-8 -*-
"""Expert-team system-prompt briefing (moved from src/_service/_chat.py).

Original integration point: ``ChatService._run_impl`` wrapped the agent's
``system_prompt`` with ``_build_leader_system_prompt(...)`` at construction
time. Framework sources must stay untouched, so the briefing is re-attached
by wrapping ``ResourceAccessService.resolve_agent`` instead — that is the
single place ``ChatService`` loads the ``AgentRecord`` for a chat run (the
other ``resolve_agent`` callers discard the return value), so wrapping it
yields exactly the same behavior: a leader carrying a ``team_config`` with
non-empty ``member_ids`` gets its system prompt extended with the team
briefing before the agent is constructed.
"""
from __future__ import annotations

from agentscope.app._service._access import ResourceAccessService
from agentscope.app._tool._constants import HANDLE_LEN
from agentscope.app.storage import AgentRecord, StorageBase


# ----------------------------------------------------------------------
# Expert-team system-prompt briefing
# ----------------------------------------------------------------------
# When a leader agent carries a TeamConfig, we surface the configured
# members and handoff relations directly in its system prompt at session
# start. This makes the persistent team config authoritative: the LLM
# spawns exactly these members (via AgentCreate / AgentInvite) instead of
# inventing an ad-hoc team. This is the "config-driven soft handoff"
# integration point — free_handoff mode issues guidance; workflow mode is
# reserved (currently behaves like free_handoff). The runtime team tools
# (TeamCreate / AgentCreate / AgentInvite / TeamSay / TeamDelete) are
# untouched.
async def _build_leader_system_prompt(
    agent_record: AgentRecord,
    storage: StorageBase,
) -> str:
    """Return the leader's system prompt, extended with a team briefing.

    The briefing lists the configured members (names + roles) and the
    handoff relations, instructing the LLM to coordinate exactly this team
    when the task warrants it. Plain agents are returned unchanged.
    """
    from bocomadp import team_store

    base = agent_record.data.system_prompt
    rel = await team_store.get_team(
        storage,
        agent_record.user_id,
        agent_record.id,
    )
    if rel is None or not rel.member_ids:
        return base

    cfg = rel
    owner_id = agent_record.user_id
    lines: list[str] = []
    lines.append(
        "\n\n# Expert team briefing\n"
        "You lead a pre-configured expert team. When the user's "
        "request fits the configured team's expertise, PREFER using "
        "the members listed below and follow the configured handoff "
        "order as a useful guide. When you judge the request clearly "
        "does not fit the team (for example: a domain gap, or a "
        "specialty no member covers), you are explicitly free to "
        "create a new specialist via AgentCreate, or to spin up an "
        "ad-hoc member via TeamCreate + AgentInvite. The configured "
        "team is the default starting point, not a hard ceiling — "
        "honor the user's intent above loyalty to the existing "
        "roster.\n"
    )

    member_lines: list[str] = []
    for mid in cfg.member_ids:
        m = await storage.get_agent(owner_id, mid)
        if m is None:
            continue
        role = m.data.invite_config.invite_description or "team member"
        # Print the exact ``name@handle`` form: ``AgentInvite`` /
        # ``TeamSay`` resolve targets by it, and the handle is not
        # guessable from the display name, so hand it to the model
        # verbatim instead of letting it invent one.
        member_lines.append(
            f"- {m.data.name}@{m.id[:HANDLE_LEN]}: {role}"
        )
    if member_lines:
        lines.append(
            "## Team members\n"
            "(invite/delegate targets are `<name>@<handle>` exactly as "
            "listed)\n"
            + "\n".join(member_lines)
        )

    if cfg.handoff_relations:
        rel_lines = [
            f"- {await _name(owner_id, storage, r.from_agent_id)} → "
            f"{await _name(owner_id, storage, r.to_agent_id)}"
            f"{(' (' + r.description + ')') if r.description else ''}"
            for r in cfg.handoff_relations
        ]
        lines.append(
            "## Collaboration / handoff order\n"
            "Route sub-tasks along these edges:\n"
            + "\n".join(rel_lines)
        )
        if cfg.collaboration_mode == "free_handoff":
            lines.append(
                "Mode: free handoff — use the order above as guidance when "
                "delegating, and report results back to the user through "
                "the team lead."
            )
        else:
            lines.append(
                "Mode: **workflow** — strict sequential chain. You are "
                "the hub: members report ONLY to you, and you forward "
                "each member's result to the next member in the chain.\n"
                "\n"
                "Your team is CONFIGURED but NOT yet assembled: the "
                "members above are configured agent definitions — they "
                "have NO live session until you invite them. Assemble "
                "the team first:\n"
                "1. Call ``TeamCreate`` ONCE to create an empty team "
                "(this puts YOUR session into the team — required "
                "before ``TeamSay`` / ``AgentInvite`` can work).\n"
                "2. For each member you delegate to, call "
                "``AgentInvite(target=<name>@<handle>, prompt=<full "
                "task>)`` — this mints the member's live session inside "
                "your team and delivers the first task.\n"
                "Do NOT call ``AgentCreate`` — it would create a NEW "
                "unrelated agent instead of using the configured member "
                "above.\n"
                "\n"
                "Rules (hard-enforced):\n"
                "- You may ONLY call ``TeamSay`` to members that appear "
                "as a ``to`` endpoint of the edges above. Any other "
                "target FAILS — the message is NOT delivered.\n"
                "- A member starts working ONLY when you actually call "
                "``AgentInvite`` (first delegation) or ``TeamSay`` "
                "(follow-ups) — writing 'the member is already "
                "working' in your reply does nothing; no task is "
                "delivered, no session is woken up.\n"
                "- If you plan to delegate, do not end your turn until "
                "the real ``AgentInvite`` / ``TeamSay`` call has been "
                "sent.\n"
                "- Never skip a step: only delegate to the next member "
                "after the current member has reported back.\n"
                "- Forward the COMPLETE result (all details, numbers, "
                "conclusions), not a summary.\n"
                "\n"
                "Delegation workflow:\n"
                "1. ``TeamCreate`` an empty team (once).\n"
                "2. ``AgentInvite`` the FIRST member with the full "
                "task.\n"
                "3. Wait for that member to report back (via "
                "``TeamSay``).\n"
                "4. Forward the complete result to the NEXT member via "
                "``AgentInvite`` (or ``TeamSay`` if already in the "
                "team).\n"
                "5. Repeat until every member in the chain has "
                "finished, then report the final result to the user."
            )

    return base + "".join(lines)


async def _name(
    owner_id: str,
    storage: StorageBase,
    agent_id: str,
) -> str:
    """Best-effort display name for an agent id (leader or member)."""
    if agent_id == owner_id:
        return agent_id
    rec = await storage.get_agent(owner_id, agent_id)
    return rec.data.name if rec is not None else agent_id


# ----------------------------------------------------------------------
# Patch: re-attach the briefing via ResourceAccessService.resolve_agent
# ----------------------------------------------------------------------
# ChatService._run_impl loads the agent record through this method (the
# other callers discard the return value), so wrapping it injects the
# briefing exactly where the original ``_run_impl`` call did — before the
# Agent is constructed with ``system_prompt=agent_record.data.system_prompt``.
# 模块级捕获的只是 import 时的原始方法；真正生效的「下一层」在
# :func:`patch_team_briefing` 里动态绑定（例如 open_agent_access 先
# patch 的跨 owner 兜底层），否则兜底层会被本闭包绕过。
_original_resolve_agent = ResourceAccessService.resolve_agent


async def _resolve_agent_with_briefing(
    self: ResourceAccessService,
    viewer_id: str,
    agent_id: str,
) -> AgentRecord:
    # 动态取下一层（patch 时绑定的当前挂载版本），避免闭包固化
    # import 时的原始方法而绕过其它包装层（open_agent_access 等）。
    original = getattr(
        _resolve_agent_with_briefing,
        "_original",
        _original_resolve_agent,
    )
    record = await original(self, viewer_id, agent_id)
    from bocomadp import team_store

    rel = await team_store.get_team(self._storage, viewer_id, agent_id)
    if rel is not None and rel.member_ids:
        # Pydantic ``model_copy`` revalidates nothing; keep the original
        # record shape, swap only the system prompt with the briefing.
        record = record.model_copy(
            update={
                "data": record.data.model_copy(
                    update={
                        "system_prompt": await _build_leader_system_prompt(
                            record,
                            self._storage,
                        ),
                    },
                ),
            },
        )
    return record


def patch_team_briefing() -> None:
    """Wrap ``resolve_agent`` so chat runs get the expert-team briefing.

    Idempotent: calling twice does not wrap twice.
    """
    if ResourceAccessService.resolve_agent.__name__ != (
        _resolve_agent_with_briefing.__name__
    ):
        # 绑定「当前挂载的下一层」：调用方（main.py）保证 open 兜底等
        # 包装先于本函数 patch，这里绑定的就是它们——本层在外、兜底
        # 在里，兜底返回的 record 也能继续接受 briefing 注入。
        _resolve_agent_with_briefing._original = (  # type: ignore[attr-defined]
            ResourceAccessService.resolve_agent
        )
        ResourceAccessService.resolve_agent = _resolve_agent_with_briefing
