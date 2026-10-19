#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向 ``credentials`` 表插入固定的凭证数据（直接写库，不经 HTTP 接口）。

用途
----
本仓库的凭证由 ``AsyncSQLAlchemyStorage`` 落在 ``credentials`` 表，行结构为：

=============== ========================== ============================================
列                类型                        说明
=============== ========================== ============================================
``id``          varchar(255) PK            凭证 id（app 侧 ``CredentialBase.id``）
``user_id``     varchar(255)               归属用户；HTTP 层即 ``X-User-ID``
``created_at``  datetime (naive UTC)       创建时间
``updated_at``  datetime (naive UTC)       更新时间
``payload``     json                       剩余字段，形如 ``{"data": <凭证字典>}``
=============== ========================== ============================================

``payload`` 的形状由 ``_mappers._from_record`` 决定：``CredentialRecord`` 的
``id`` / ``created_at`` / ``updated_at`` 提列为主键与时间戳，``user_id`` 提为
独立列，剩下的 ``data`` 原样进 ``payload``——所以**只能**是 ``{"data": {...}}``
这一个顶层键，且 ``data`` 中必须带 ``type`` 判别字段（如
``bocom_ellm_credential`` / ``openai_credential`` / ``deepseek_credential``），
否则 app 侧 ``CredentialFactory.from_dict`` 无法反序列化。

所有入参都是本文件"配置区"的代码变量：改完直接运行，无命令行参数。
脚本按 ``id`` 做幂等 UPSERT（见 ``ON_DUPLICATE``），重复执行不报错。

用法::

    # 编辑配置区的 DB_* 与 CREDENTIALS 后运行
    python3 scripts/insert_credentials.py

    # 只想校验配置、不落库：把 DRY_RUN 改成 True

依赖：``pymysql``（``pip install PyMySQL``；仓库运维脚本 pg2mysql_migrate.py
同款驱动）。

注意：直接写库绕过了 app 的凭证接口校验，``data`` 写错不会立刻报错，
只会在使用时（如建会话）暴露。因此脚本只做最基本的结构校验，请自行对照
``src/agentscope/credential/`` 与 ``bocomadp/credential/`` 下的凭证类字段。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from uuid import uuid4

# ---------------------------------------------------------------------------
# 配置区：按需修改（全部为脚本内变量，不接受命令行参数）
# ---------------------------------------------------------------------------

#: 目标库连接参数（默认对齐 examples/agent_service/config.yaml 的 db.url）。
DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "agentscope"
DB_PASSWORD = "agentscope"
DB_DATABASE = "agentscope"
DB_CHARSET = "utf8mb4"
DB_CONNECT_TIMEOUT = 10

#: 目标表名（框架默认 credentials）。
TABLE = "credentials"

#: 主键冲突处理：
#:   "update" —— 覆盖同 id 行的 payload/updated_at（幂等，可反复执行；推荐）
#:   "ignore" —— 跳过已存在的行，保留库中原值
#:   "error"  —— 直接报错回滚（严格只新增）
ON_DUPLICATE = "update"

#: True 时只打印将要写入的内容并校验，不连库、不落库。
DRY_RUN = False

#: 缺失 ``id`` 时是否自动生成 32 位十六进制 id（与 app 的 _generate_id 同形）。
#: 注意：自动生成的 id 每次都不同，此时 ON_DUPLICATE 无法起到幂等作用。
AUTO_GENERATE_ID = True

# ---------------------------------------------------------------------------
# 要插入的两条固定数据：按需填写
# ---------------------------------------------------------------------------
# 每个元素支持三个键：
#   "id"       —— 主键凭证 id，留空按 AUTO_GENERATE_ID 处理；
#   "user_id"  —— 归属用户（app 侧 X-User-ID）；同一 id 全局唯一。
#   "data"     —— 凭证字典，必须含 "type"；"id" 会自动与上面的 id 对齐。
# 另可选 "created_at" / "updated_at"：ISO 字符串或 datetime，缺省为当前 UTC。
#
# 下面两条是 ELLM 凭证的模板，请替换 id / user_id / api_key / base_url 等。
CREDENTIALS: list[dict] = [
    {
        "id": "default001",
        "user_id": "admin",
        "data": {
            "type": "bocom_ellm_credential",
            "name": "default1",
            "model": None,
            "api_key": "sk-REPLACE-ME-1",
            "base_url": "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-ADAPTER.V-1.0/v1",
            "organization": None,
            "scene_code": "P2024017",
            "api_key_url": "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do",
            "inject_think_tag": False,
            "apikey_expires_at": None,
        },
    },
    {
        "id": "default002",
        "user_id": "admin",
        "data": {
            "type": "bocom_ellm_credential",
            "name": "default2",
            "model": None,
            "api_key": "sk-REPLACE-ME-2",
            "base_url": "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-ADAPTER.V-1.0/v1",
            "organization": None,
            "scene_code": "P2026116",
            "api_key_url": "http://eaip-ellm-1.bocomm.com/ELLM.ELLM-OMSERVICE.V-1.0/createSceneApiKey.do",
            "inject_think_tag": False,
            "apikey_expires_at": None,
        },
    },
    # 其他凭证类型（字段以对应凭证类为准），例如：
    # {
    #     "id": "manual-openai-1",
    #     "user_id": "lwh",
    #     "data": {
    #         "type": "openai_credential",
    #         "name": "OpenAI",
    #         "api_key": "sk-xxx",
    #         "organization": None,
    #         "base_url": None,
    #     },
    # },
]

# ---------------------------------------------------------------------------
# 以下无需改动
# ---------------------------------------------------------------------------

_SUPPORTED_ON_DUPLICATE = ("update", "ignore", "error")


def _utcnow() -> datetime:
    """当前时间（naive UTC），与 app 侧 ``_storage._utcnow`` 一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _coerce_dt(value: object, default: datetime, label: str) -> datetime:
    """把 ISO 字符串 / datetime 归一成 naive UTC；缺省返回 *default*。"""
    if value is None:
        return default
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    raise ValueError(f"{label} 只支持 ISO 字符串或 datetime，收到 {value!r}")


def _normalize(items: list[dict]) -> list[dict]:
    """校验并补全配置区条目，返回可直接落库的行列表。"""
    if not items:
        raise ValueError("CREDENTIALS 为空，没有要插入的数据")

    now = _utcnow()
    rows: list[dict] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条不是 dict: {item!r}")

        data = item.get("data")
        if not isinstance(data, dict) or not data:
            raise ValueError(f"第 {index} 条的 data 必须是非空 dict")
        if not data.get("type"):
            raise ValueError(
                f"第 {index} 条的 data 缺少 type 判别字段（如 "
                "'bocom_ellm_credential'）",
            )

        credential_id = str(item.get("id") or "").strip()
        if not credential_id:
            if not AUTO_GENERATE_ID:
                raise ValueError(f"第 {index} 条缺少 id 且 AUTO_GENERATE_ID=False")
            credential_id = uuid4().hex
        if credential_id in seen_ids:
            raise ValueError(f"第 {index} 条的 id 与前面的条目重复: {credential_id}")
        seen_ids.add(credential_id)

        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            raise ValueError(f"第 {index} 条缺少 user_id")

        # data["id"] 与主键列保持一致（app 侧的 dump 两者同值）。
        data = dict(data)
        if data.get("id") not in (None, "", credential_id):
            print(
                f"[warn] 第 {index} 条 data['id']={data['id']!r} 与 id="
                f"{credential_id!r} 不一致，已按 id 覆盖",
                file=sys.stderr,
            )
        data["id"] = credential_id
        data.setdefault("name", "")

        rows.append(
            {
                "id": credential_id,
                "user_id": user_id,
                "created_at": _coerce_dt(
                    item.get("created_at"), now, f"第 {index} 条 created_at",
                ),
                "updated_at": _coerce_dt(
                    item.get("updated_at"), now, f"第 {index} 条 updated_at",
                ),
                "payload": json.dumps({"data": data}, ensure_ascii=False),
            },
        )

    return rows


def _build_sql() -> str:
    """按 ON_DUPLICATE 生成 INSERT 语句。"""
    base = (
        f"INSERT {'IGNORE ' if ON_DUPLICATE == 'ignore' else ''}"
        f"INTO `{TABLE}` (`id`, `user_id`, `created_at`, `updated_at`, `payload`) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    if ON_DUPLICATE == "update":
        # VALUES() 语法兼容 MySQL 5.7/8.x 与 OceanBase；不上 8.0.20+ 的别名写法。
        base += (
            " ON DUPLICATE KEY UPDATE "
            "`user_id`=VALUES(`user_id`), "
            "`payload`=VALUES(`payload`), "
            "`updated_at`=VALUES(`updated_at`)"
        )
    return base


def _verify(cur, credential_id: str) -> str:
    """按 id 回读一行，返回 payload 文本（便于肉眼确认）。"""
    cur.execute(
        f"SELECT user_id, payload FROM `{TABLE}` WHERE `id` = %s",
        (credential_id,),
    )
    row = cur.fetchone()
    if row is None:
        return "<未查到>"
    payload = row[1]
    if isinstance(payload, (str, bytes, bytearray)):
        # JSON 列在部分驱动/兼容库里按字符串返回，展开后再打印更易读。
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            pass
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return f"user_id={row[0]} payload={text}"


def main() -> int:
    if ON_DUPLICATE not in _SUPPORTED_ON_DUPLICATE:
        print(
            f"[error] ON_DUPLICATE={ON_DUPLICATE!r} 非法，"
            f"取值为 {_SUPPORTED_ON_DUPLICATE}",
            file=sys.stderr,
        )
        return 2

    try:
        rows = _normalize(CREDENTIALS)
    except ValueError as exc:
        print(f"[error] 配置有误: {exc}", file=sys.stderr)
        return 2

    print(f"目标: mysql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_DATABASE} 表 `{TABLE}`")
    print(f"模式: {'DRY-RUN（不落库）' if DRY_RUN else ON_DUPLICATE}，共 {len(rows)} 条")
    for row in rows:
        print(f"  - id={row['id']} user_id={row['user_id']}")
        print(f"    payload={row['payload']}")

    if DRY_RUN:
        print("\n[DRY-RUN] 未连接数据库，未写入任何数据。")
        return 0

    try:
        import pymysql
    except ImportError:
        print("缺少依赖，请先执行: pip install PyMySQL", file=sys.stderr)
        return 2

    sql = _build_sql()
    conn = None
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_DATABASE,
            charset=DB_CHARSET,
            connect_timeout=DB_CONNECT_TIMEOUT,
            autocommit=False,
            cursorclass=pymysql.cursors.Cursor,
        )
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    sql,
                    (
                        row["id"],
                        row["user_id"],
                        row["created_at"],
                        row["updated_at"],
                        row["payload"],
                    ),
                )
                # rowcount: 1=插入, 2=更新（值有变化）, 0=已存在且内容一致
                # （ON DUPLICATE="ignore" 跳过的行也是 0）。
                action = {
                    0: "unchanged/skipped（已存在，值一致）",
                    1: "insert",
                    2: "update",
                }.get(cur.rowcount, f"rowcount={cur.rowcount}")
                print(f"[ok] {row['id']} -> {action}")
                print(f"     {_verify(cur, row['id'])}")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            conn.rollback()
        print(f"[error] 写入失败，已回滚: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()

    print(f"\n完成：{len(rows)} 条已提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
