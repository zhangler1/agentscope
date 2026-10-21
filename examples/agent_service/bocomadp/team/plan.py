# -*- coding: utf-8 -*-
"""DAG validation and stage planning for the orchestrated workflow.

The configuration layer stores an unordered set of handoff edges. This
module turns that graph into a deterministic list of stages:

- every stage contains members that may run in parallel;
- stages are ordered: stage N must finish before stage N+1 starts;
- leader edges are stripped before layering and only used to validate the
  leader's first/last-node role.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field

from bocomadp.team_store import ExpertTeamRelation

logger = logging.getLogger("bocomadp.team.plan")


class PlanError(ValueError):
    """Raised when the configured handoff graph cannot form a valid plan."""


class WorkflowStage(BaseModel):
    """One dependency layer; members in the same stage run in parallel."""

    stage: int
    members: list[str]


class WorkflowPlan(BaseModel):
    """Deterministic execution plan derived from the team configuration."""

    leader_agent_id: str
    stages: list[WorkflowStage]
    edges: list[tuple[str, str]] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    standby_members: list[str] = Field(default_factory=list)

    @property
    def stage_count(self) -> int:
        return len(self.stages)

    def members_of(self, stage_index: int) -> list[str]:
        if 0 <= stage_index < len(self.stages):
            return list(self.stages[stage_index].members)
        return []


def _unique_edges(
    relations: list[Any],
    leader_id: str,
) -> list[tuple[str, str]]:
    """Return member-to-member edges, rejecting self loops and leader edges."""
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel in relations:
        source = str(rel.from_agent_id)
        target = str(rel.to_agent_id)
        if source == leader_id and target == leader_id:
            raise PlanError("handoff edge cannot be leader -> leader")
        if source == target:
            raise PlanError(f"handoff edge cannot be a self loop: {source}")
        if source == leader_id or target == leader_id:
            continue
        pair = (source, target)
        if pair not in seen:
            seen.add(pair)
            edges.append(pair)
    return edges


def _kahn_layers(
    nodes: list[str],
    edges: list[tuple[str, str]],
) -> list[list[str]]:
    """Kahn topological layering; rejects cycles."""
    order = {node: index for index, node in enumerate(nodes)}
    indegree = {node: 0 for node in nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        if source not in indegree or target not in indegree:
            continue
        indegree[target] += 1
        outgoing[source].append(target)

    ready = sorted(
        [node for node, degree in indegree.items() if degree == 0],
        key=lambda node: order.get(node, 0),
    )
    layers: list[list[str]] = []
    processed = 0
    while ready:
        layer = list(ready)
        layers.append(layer)
        processed += len(layer)
        next_ready: list[str] = []
        for node in layer:
            for target in outgoing.get(node, []):
                indegree[target] -= 1
                if indegree[target] == 0:
                    next_ready.append(target)
        ready = sorted(
            next_ready,
            key=lambda node: order.get(node, 0),
        )
    if processed != len(nodes):
        remaining = sorted(
            [node for node, degree in indegree.items() if degree > 0],
        )
        raise PlanError(
            "handoff graph contains a cycle involving: "
            + ", ".join(remaining),
        )
    return layers


def build_plan(
    rel: ExpertTeamRelation,
    leader_agent_id: str,
) -> WorkflowPlan:
    """Validate ``rel`` and build a stage plan.

    Rules enforced here (see the design doc §5):

    - leader must be the first node: ``leader -> X`` edges are required;
    - all stage-0 nodes must be exactly the leader's out-edge targets;
    - leader in-edge sources must be in the last stage;
    - member graph must be acyclic;
    - members not on any edge are standby and never dispatched.
    """
    leader_id = str(leader_agent_id)
    members = [str(mid) for mid in rel.member_ids]
    if not members:
        raise PlanError("team has no members")

    allowed = {leader_id, *members}
    for edge in rel.handoff_relations:
        if edge.from_agent_id not in allowed or edge.to_agent_id not in allowed:
            raise PlanError(
                "handoff endpoint must be the leader or a current member: "
                f"{edge.from_agent_id!r} -> {edge.to_agent_id!r}",
            )

    leader_out = [str(r.to_agent_id) for r in rel.handoff_relations
                  if str(r.from_agent_id) == leader_id]
    leader_in = [str(r.from_agent_id) for r in rel.handoff_relations
                 if str(r.to_agent_id) == leader_id]
    if not leader_out:
        raise PlanError(
            "leader must be the first node: at least one leader -> X edge "
            "is required",
        )

    edges = _unique_edges(rel.handoff_relations, leader_id)

    nodes: list[str] = []
    seen_nodes: set[str] = set()
    for source, target in edges:
        for node in (source, target):
            if node not in seen_nodes:
                seen_nodes.add(node)
                nodes.append(node)
    for node in leader_out + leader_in:
        if node not in seen_nodes:
            seen_nodes.add(node)
            nodes.append(node)

    if not nodes:
        raise PlanError("handoff graph has no member nodes")

    layers = _kahn_layers(nodes, edges)
    if not layers:
        raise PlanError("handoff graph produced no stages")

    stage_zero = set(layers[0])
    if stage_zero != set(leader_out):
        raise PlanError(
            "leader must only start the first stage: stage-0 members are "
            f"{sorted(stage_zero)}, but leader out-edges point to "
            f"{sorted(set(leader_out))}",
        )

    if leader_in:
        last_stage = set(layers[-1])
        bad = sorted(set(leader_in) - last_stage)
        if bad:
            raise PlanError(
                "leader may only receive results from the last stage; "
                f"these in-edge sources are not in the last stage: {bad}",
            )

    stages = [
        WorkflowStage(stage=index, members=layer)
        for index, layer in enumerate(layers)
    ]
    standby = [mid for mid in members if mid not in seen_nodes]
    return WorkflowPlan(
        leader_agent_id=leader_id,
        stages=stages,
        edges=edges,
        nodes=nodes,
        standby_members=standby,
    )


__all__ = [
    "PlanError",
    "WorkflowPlan",
    "WorkflowStage",
    "build_plan",
]
