# -*- coding: utf-8 -*-
"""智能体市场审批人白名单（JSON 文件持久化 + 接口管理 + 末位保护）。

数据性质是**运行时数据**而非部署配置——仿 per-agent 工具白名单
（``bocomadp/routers/agent_tools.py``）的成熟套路：进程内存 set +
启动时从 JSON 文件恢复 + 每次修改原子写盘（tmp + ``os.replace``）。
config.yaml 不参与：yaml 是运维地盘，程序只读不写；审批人名单由
接口自助管理（批量增 / 单删 / 全量覆盖），改完即生效，重启不丢。

生效名单 = JSON 文件内容（**无写死账号，纯数据驱动**）：

- **末位保护（last-admin protection）**：任何写操作都不允许把名单
  改空——``overwrite_reviewers`` 收到空/全空白清单返回 ``empty``、
  ``remove_reviewer`` 删最后一人返回 ``last_one``，路由层据此映射
  409"至少保留一个审批人"。无固定锚点时这条规则是唯一的防自锁
  防线，必须结构性成立；
- **首次部署引导**：文件缺失时用种子名单 ``SEED_REVIEWERS`` 一次性
  写入并落盘；
- 接口写操作（POST/PUT/DELETE）要求调用者在生效名单内——"进了
  名单的人才能改名单"；GET 公开（名单本身不是敏感信息）。

已知取舍：若名单只剩一人且该账号作废（离职/禁用），接口层面死锁
（删他 409、他登录不了无法加人），救急通道 = 手工编辑 JSON 文件。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("bocomadp.market_reviewers")

#: 进程内生效名单（与 JSON 文件同构，启动时恢复、修改时原子写盘）。
_reviewers: set[str] = set()

#: 初始审批人种子名单：**仅当名单文件不存在时**（首次部署）一次性
#: 写入文件并落盘，之后文件即唯一真相源——种子用户与接口新增者完全
#: 同权，可删可改（仅受末位保护约束）。
SEED_REVIEWERS: tuple[str, ...] = ("sunw_94", "jiangchengwei")


def _whitelist_file() -> Path:
    """JSON 持久化路径：``{workspace}/_meta/agent_market_reviewers.json``。

    优先级：环境变量 ``BOCOMADP_MARKET_REVIEWERS_FILE``（测试隔离用，
    指向 tmp_path）> workspace 目录（与工具白名单 ``_whitelist_file``
    同款，重启不丢）> 工程根目录散文件（workspace 不可用时的兜底）。
    """
    env_path = os.environ.get("BOCOMADP_MARKET_REVIEWERS_FILE")
    if env_path:
        return Path(env_path)
    try:
        from bocomadp.config.uploads_config import get_workspace_dir

        return get_workspace_dir() / "_meta" / "agent_market_reviewers.json"
    except Exception:  # noqa: BLE001
        return Path(
            os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)),
                ),
                ".agent_market_reviewers.json",
            ),
        )


def _persist() -> None:
    """原子写盘（先写 ``.tmp`` 再 ``os.replace``，防写坏）；失败仅告警。"""
    path = _whitelist_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(_reviewers), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.warning("persist market reviewer whitelist failed", exc_info=True)


def load_whitelist() -> None:
    """启动时从 JSON 恢复名单（main.py 调用）。

    文件缺失 = 空名单（首次部署的正常初始态，打 info 不告警，等待
    运维手工种入第一批审批人）；格式非法整个忽略，绝不因脏文件阻断
    启动。
    """
    path = _whitelist_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning(
                "market reviewers file %s is not a list; ignoring", path,
            )
            _reviewers.clear()
            return
        _reviewers.clear()
        for uid in data:
            if isinstance(uid, str) and uid.strip():
                _reviewers.add(uid.strip())
        logger.info("loaded %d market reviewers from %s", len(_reviewers), path)
    except FileNotFoundError:
        _reviewers.clear()
        seed = {uid.strip() for uid in SEED_REVIEWERS if uid and uid.strip()}
        if seed:
            # 首次部署：把种子名单落盘（之后文件为准，种子可被接口删改）
            _reviewers.update(seed)
            _persist()
            logger.info(
                "seeded %d initial market reviewers to %s", len(seed), path,
            )
        else:
            logger.info("no market reviewer whitelist file yet: %s", path)
    except Exception:  # noqa: BLE001
        logger.warning("load market reviewer whitelist failed", exc_info=True)


def effective_reviewers() -> list[str]:
    """生效名单（即文件内容），排序返回；可能为空（首次部署初始态）。"""
    return sorted(_reviewers)


def is_reviewer(user_id: str) -> bool:
    """审批权 / 管理权统一判据：在生效名单内。"""
    return user_id in _reviewers


def add_reviewers(user_ids: list[str]) -> int:
    """批量新增（幂等，空白忽略）；返回实际新增人数。"""
    added = 0
    for raw in user_ids or []:
        uid = (raw or "").strip()
        if not uid or uid in _reviewers:
            continue
        _reviewers.add(uid)
        added += 1
    if added:
        _persist()
    return added


def overwrite_reviewers(user_ids: list[str]) -> str:
    """全量覆盖名单（"清空重来"的显式出口）。

    返回 ``ok`` / ``empty``（清单为空或全空白——会触发末位保护，
    路由层据此映射 409）。内容无变化时不写盘。
    """
    fresh = {(raw or "").strip() for raw in (user_ids or [])}
    fresh.discard("")
    if not fresh:
        return "empty"
    changed = fresh != _reviewers
    _reviewers.clear()
    _reviewers.update(fresh)
    if changed:
        _persist()
    return "ok"


def remove_reviewer(user_id: str) -> str:
    """删单个审批人（带末位保护）。

    返回 ``ok`` / ``last_one``（要删的是最后一个审批人——路由层据此
    映射 409）/ ``not_found``（不在名单）。
    """
    uid = (user_id or "").strip()
    if uid not in _reviewers:
        return "not_found"
    if len(_reviewers) == 1:
        return "last_one"
    _reviewers.discard(uid)
    _persist()
    return "ok"


__all__ = [
    "SEED_REVIEWERS",
    "add_reviewers",
    "effective_reviewers",
    "is_reviewer",
    "load_whitelist",
    "overwrite_reviewers",
    "remove_reviewer",
]
