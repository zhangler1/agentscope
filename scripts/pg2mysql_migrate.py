#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pyright: basic
# 说明：本脚本为一次性运维工具，大量使用动态 DB 游标（Any），故采用 basic 检查模式，
# 不对 Any/Unknown 派生告警做全量收敛。
"""PostgreSQL -> MySQL 定向表迁移脚本。

从 PG 中读取指定表（可多张）的「结构 + 数据」，自动转换成 MySQL 方言建表后
批量插入，并做源/目标行数核对。适用于本仓库"老数据在 PG、新库切 MySQL/OB"的
一次性迁移场景（框架表 agents/sessions/messages/...，以及 runtime_configs、
system_prompts、agent_pool_configs 等业务自建表）。

特性
----
- 无需预写目标 DDL：从 PG information_schema 动态取列/主键/唯一键，
  按 PG->MySQL 类型映射自动建表（含 serial/identity -> AUTO_INCREMENT）。
- 类型自适应转换：boolean->tinyint(1)、json/jsonb->json、uuid->varchar(36)、
  timestamptz、bytea->longblob 等；值在 Python 层统一转换，规避方言差异。
- 多表批量、流式读 + 多行 VALUES 插入，遇包过大自动折半重试；
  每表单事务，失败回滚不影响其它表。
- 幂等选项：--recreate / --truncate-first / --skip-existing / --on-duplicate ignore，
  支持断点续跑。

运行示例
--------
# 安装依赖（任选一个 python 环境）
pip install psycopg2-binary PyMySQL

# 迁移多张表
python3 scripts/pg2mysql_migrate.py \
  --pg-url   "postgresql://agentscope:agentscope@localhost:5432/agentscope" \
  --mysql-url "mysql://agentscope:agentscope@localhost:3306/agentscope" \
  --tables credentials,agents,sessions,messages,runtime_configs,system_prompts

# 目标表已存在需要重建
python3 scripts/pg2mysql_migrate.py \
  --pg-url "$PG_URL" --mysql-url "$MYSQL_URL" \
  --tables sessions,messages --recreate

# 全库迁移（排除部分表）
python3 scripts/pg2mysql_migrate.py \
  --pg-url "$PG_URL" --mysql-url "$MYSQL_URL" --all --exclude alembic_version

说明
----
- PG 端用户需对目标表及 information_schema 有 SELECT 权限。
- MySQL 端库需已存在；字符集默认 utf8mb4，可用 --charset/--collation 覆盖。
- 大表建议加 --server-cursor（服务端游标流式读取，降低内存占用）；
  若报 max_allowed_packet 相关错误，先调大 MySQL 的 max_allowed_packet，
  或调小 --chunk-bytes。
- 不支持的类型（如 PG array / interval / 自定义复合类型）会在日志中明示并跳过该表。
- 外键、普通索引不迁移（生产索引建议用应用侧 alembic / create_tables 生成）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Optional
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# 驱动（惰性导入，缺依赖时给出安装提示）
# ---------------------------------------------------------------------------
# 运行时可选依赖：缺失时仅提示，不阻止 import（避免静态解析器报模块缺失）。
import importlib
psycopg2: Any = None
pymysql: Any = None
try:  # pragma: no cover
    psycopg2 = importlib.import_module("psycopg2")
except ImportError:  # pragma: no cover
    pass
try:  # pragma: no cover
    pymysql = importlib.import_module("pymysql")
except ImportError:  # pragma: no cover
    pass

_NO_DEFAULT_SUBSTR = ("text", "blob", "json", "geometry")


def _no_default(mt: str) -> bool:
    """TEXT/BLOB/JSON/GEOMETRY 列不能带 DEFAULT（MySQL/OB 不允许）。"""
    for kw in _NO_DEFAULT_SUBSTR:
        if kw in mt:
            return True
    return False


# ---------------------------------------------------------------------------
# 元数据模型
# ---------------------------------------------------------------------------
@dataclass
class ColMeta:
    name: str
    data_type: str           # information_schema.columns.data_type（小写）
    udt_name: str            # PG 完整类型名（_int4 / 枚举名等）
    max_length: Optional[int]
    num_precision: Optional[int]
    num_scale: Optional[int]
    nullable: bool
    default: Optional[str]
    identity: Optional[str]  # ALWAYS / BY DEFAULT / None
    ordinal: int


@dataclass
class TableMeta:
    name: str
    columns: list[ColMeta] = field(default_factory=list)
    pk: list[str] = field(default_factory=list)         # 有序主键列
    uniques: list[list[str]] = field(default_factory=list)  # list[list[str]]


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------
def parse_dsn(url: str, kind: str) -> dict[str, Any]:
    """解析 sqlalchemy 风格 DSN，兼容 postgresql+psycopg2://、mysql+aiomysql://。"""
    scheme, _, _ = url.partition("://")
    base_scheme = scheme.split("+", 1)[0].split(":", 1)[0]
    if base_scheme != kind:
        raise ValueError(f"URL scheme '{scheme}' 与 {kind} 不匹配: {url}")
    p = urlparse(url)
    if not p.path or p.path.lstrip("/") == "":
        raise ValueError(f"URL 缺少库名: {url}")
    query = {}
    if p.query:
        for item in p.query.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                query[k] = v
    info: dict[str, Any] = {
        "host": p.hostname or "localhost",
        "port": p.port or (5432 if base_scheme == "postgresql" else 3306),
        "user": unquote(p.username) if p.username else "",
        "password": unquote(p.password) if p.password else "",
        "database": unquote(p.path.lstrip("/")),
        "query": query,
    }
    if not info["user"]:
        raise ValueError(f"URL 缺少用户名: {url}")
    return info


def _connect_pg(info: dict[str, Any]) -> Any:
    assert psycopg2 is not None  # main() 已前置检查依赖
    kwargs = {
        "host": info["host"],
        "port": info["port"],
        "user": info["user"],
        "password": info["password"],
        "dbname": info["database"],
    }
    for k in ("sslmode", "sslrootcert", "connect_timeout", "application_name"):
        if k in info["query"]:
            kwargs[k] = info["query"][k]
    return psycopg2.connect(**kwargs)


def _connect_mysql(info: dict[str, Any], charset: str) -> Any:
    assert pymysql is not None  # main() 已前置检查依赖
    return pymysql.connect(
        host=info["host"],
        port=info["port"],
        user=info["user"],
        password=info["password"],
        database=info["database"],
        charset=charset,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=3600,
        write_timeout=3600,
    )


# ---------------------------------------------------------------------------
# PG 元数据读取
# ---------------------------------------------------------------------------
_SCHEMA_SQL = "SELECT current_schema()"

_COLUMNS_SQL = """
SELECT column_name, data_type, udt_name,
       character_maximum_length, numeric_precision, numeric_scale,
       is_nullable, column_default, identity_generation, ordinal_position
FROM information_schema.columns
WHERE table_schema = %(schema)s AND table_name = %(table)s
ORDER BY ordinal_position
"""

_CONSTRAINTS_SQL = """
SELECT tc.constraint_type, tc.constraint_name, kcu.column_name, kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name
 AND kcu.table_schema = tc.table_schema
WHERE tc.table_schema = %(schema)s
  AND tc.table_name = %(table)s
  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
ORDER BY tc.constraint_name, kcu.ordinal_position
"""

_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = %(schema)s AND table_type = 'BASE TABLE'
ORDER BY table_name
"""


def _load_table_meta(cur: Any, schema: str, table: str) -> Optional[TableMeta]:
    cur.execute(_COLUMNS_SQL, {"schema": schema, "table": table})
    rows = cur.fetchall()
    if not rows:
        return None
    meta = TableMeta(name=table)
    for r in rows:
        meta.columns.append(
            ColMeta(
                name=r[0],
                data_type=(r[1] or "").lower(),
                udt_name=r[2] or "",
                max_length=r[3],
                num_precision=r[4],
                num_scale=r[5],
                nullable=(r[6] == "YES"),
                default=r[7],
                identity=r[8],
                ordinal=r[9],
            )
        )
    cur.execute(_CONSTRAINTS_SQL, {"schema": schema, "table": table})
    seen: dict[tuple[str, str], list[str]] = {}
    for ctype, cname, colname, _pos in cur.fetchall():
        seen.setdefault((ctype, cname), []).append(colname)
    for (ctype, cname), cols in seen.items():
        if ctype == "PRIMARY KEY":
            meta.pk = cols
        else:
            meta.uniques.append(cols)
    return meta


# ---------------------------------------------------------------------------
# PG -> MySQL 类型映射
# ---------------------------------------------------------------------------
def _is_autoinc(col: ColMeta) -> bool:
    if col.identity:
        return True
    return bool(col.default and str(col.default).strip().lower().startswith("nextval("))


def map_mysql_type(col: ColMeta) -> Optional[str]:
    """返回 MySQL 列类型片段；不支持的返回 None。"""
    dt = col.data_type
    n = col.max_length
    if dt == "character varying":
        if n is None:
            return "varchar(255)"
        return "varchar(%d)" % n if n <= 16000 else "text"
    if dt == "character":
        return "char(%d)" % n if n else "char(1)"
    if dt == "text":
        return "text"
    if dt == "smallint":
        return "smallint"
    if dt in ("integer", "int"):
        return "int"
    if dt == "bigint":
        return "bigint"
    if dt in ("numeric", "decimal"):
        p = col.num_precision
        s = col.num_scale
        if p is None:
            p, s = 65, 30  # PG numeric 任意精度 -> MySQL decimal 上限
        s = 0 if s is None else s
        return "decimal(%d,%d)" % (max(p, s), s)
    if dt == "money":
        return "decimal(19,4)"
    if dt == "real":
        return "float"
    if dt == "double precision":
        return "double"
    if dt == "boolean":
        return "tinyint(1)"
    if dt == "date":
        return "date"
    if dt in ("timestamp without time zone", "timestamp with time zone"):
        return "datetime"
    if dt == "time without time zone":
        return "time"
    if dt == "time with time zone":
        return "time"
    if dt in ("json", "jsonb"):
        return "json"
    if dt == "uuid":
        return "varchar(36)"
    if dt == "bytea":
        return "longblob"
    if dt == "bit":
        return "bit(%d)" % (n or 1)
    if dt == "bit varying":
        return "varbinary(%d)" % (n or 65535)
    if dt in ("inet", "cidr"):
        return "varchar(64)"
    if dt == "macaddr":
        return "varchar(17)"
    if dt == "ARRAY":
        return None
    if dt == "USER-DEFINED":
        # 枚举 / domain / 自定义类型：落 varchar(255)，值侧统一 str()
        return "varchar(255)"
    return None  # interval、tsvector、复合类型等


def _render_default(col: ColMeta, mysql_type: str) -> Optional[str]:
    """把 PG column_default 转成 MySQL DEFAULT 片段；None 表示不写 DEFAULT。"""
    if _is_autoinc(col):
        return None
    raw = col.default
    if not raw:
        return None
    s = str(raw).strip()
    low = s.lower()

    if low in (
        "now()", "current_timestamp", "current_timestamp()",
        "transaction_timestamp()", "statement_timestamp()",
        "clock_timestamp()", "localtimestamp",
    ):
        return "CURRENT_TIMESTAMP" if mysql_type.startswith(("datetime", "timestamp")) else None
    if low in ("current_date", "current_date()"):
        return "CURRENT_DATE" if mysql_type == "date" else None

    # 剔除 '::type' 类型转换后缀，并解一层 PG 表达式括号
    val = re.sub(r"::[a-zA-Z_][\w\s]*", "", s).strip()
    if val.startswith("(") and val.endswith(")"):
        val = val[1:-1].strip()
    # 仍含函数调用（gen_random_uuid() 等）-> 忽略默认
    if re.search(r"[a-zA-Z_]\w*\s*\(", val):
        return None

    # 字符串字面量：'false'::boolean / '{}'::jsonb / 'xxx'::text
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        inner = val[1:-1].replace("''", "'")
        inner_low = inner.lower()
        if inner_low in ("true", "false") and mysql_type.startswith("tinyint"):
            return "1" if inner_low == "true" else "0"
        if _no_default(mysql_type):
            return None  # MySQL 不允许 text/blob/json 带默认值
        maxlen = _varchar_max(mysql_type)
        if maxlen is not None and len(inner) > maxlen:
            return None  # 默认值超过列长，避免建表失败
        return "'" + inner.replace("\\", "\\\\").replace("'", "''") + "'"

    # 裸字面量：数字 / 负数等
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", val):
        return val
    return None


def _varchar_max(mysql_type: str) -> Optional[int]:
    m = re.fullmatch(r"varchar\((\d+)\)", mysql_type)
    return int(m.group(1)) if m else None


def _build_create_sql(meta: TableMeta, charset: str, collation: str) -> str:
    lines = []
    for col in meta.columns:
        mt = map_mysql_type(col)
        if mt is None:
            raise ValueError(
                f"列 {meta.name}.{col.name} 的 PG 类型 {col.data_type} "
                f"(udt={col.udt_name}) 暂不支持自动迁移，请人工处理"
            )
        parts = ["`%s` %s" % (col.name, mt)]
        if _is_autoinc(col) and meta.pk and meta.pk[0] == col.name:
            parts.append("AUTO_INCREMENT")
        if col.name in meta.pk or not col.nullable:
            parts.append("NOT NULL")
        default = _render_default(col, mt)
        if default:
            parts.append("DEFAULT %s" % default)
        lines.append("  " + " ".join(parts))
    if meta.pk:
        pk_cols = ", ".join("`%s`" % c for c in meta.pk)
        lines.append("  PRIMARY KEY (%s)" % pk_cols)
    for uniq in meta.uniques:
        ucols = ", ".join("`%s`" % c for c in uniq)
        lines.append("  UNIQUE KEY `uniq_%s` (%s)" % ("_".join(uniq), ucols))
    body = ",\n".join(lines)
    suffix = " ENGINE=InnoDB DEFAULT CHARSET=%s" % charset
    if collation:
        suffix += " COLLATE=%s" % collation
    return "CREATE TABLE `%s` (\n%s\n)%s" % (meta.name, body, suffix)


# ---------------------------------------------------------------------------
# 值转换
# ---------------------------------------------------------------------------
_TZ_CACHE: dict[str, Any] = {}


def _resolve_tz(name: Optional[str]):
    if name is None:
        if "local" not in _TZ_CACHE:
            _TZ_CACHE["local"] = datetime.now().astimezone().tzinfo
        return _TZ_CACHE["local"]
    if name not in _TZ_CACHE:
        from zoneinfo import ZoneInfo
        _TZ_CACHE[name] = ZoneInfo(name)
    return _TZ_CACHE[name]


def convert_value(val: Any, mysql_type: str, tz) -> Any:
    """把 psycopg2 返回的 Python 值转成可安全绑定到 MySQL 列的值。"""
    if val is None:
        return None
    if mysql_type.startswith("json"):
        if isinstance(val, str):
            # psycopg2 在注册/未注册 json 扩展时返回形态不同：
            # 可能是已解析的标量字符串（需加引号），也可能是完整 JSON 文本。
            # 以"能否被 json.loads 解析"判定，避免二次转义。
            try:
                json.loads(val)
                return val
            except (ValueError, TypeError):
                return json.dumps(val, ensure_ascii=False)
        if isinstance(val, (dict, list, int, float, bool)):
            return json.dumps(val, ensure_ascii=False, default=str)
        return str(val)
    if mysql_type.startswith(("varchar", "char", "text")):
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False, default=str)
        if isinstance(val, bool):
            return "1" if val else "0"
        return str(val) if not isinstance(val, str) else val
    if mysql_type.startswith("tinyint"):
        return 1 if val is True else (0 if val is False else val)
    if "blob" in mysql_type or mysql_type.startswith(("binary", "bit")):
        if isinstance(val, (bytes, bytearray, memoryview)):
            return bytes(val)
        return str(val)
    if mysql_type.startswith("datetime"):
        if isinstance(val, datetime):
            if val.tzinfo is not None:
                return val.astimezone(tz).replace(tzinfo=None)
            return val
        if isinstance(val, date):
            return val
        return val
    if mysql_type == "time":
        if isinstance(val, dtime):
            if val.tzinfo is not None:
                # datetime.time 无 astimezone：直接剥离 tzinfo 保留墙上时间
                return val.replace(tzinfo=None)
            return val
        return val
    if mysql_type.startswith(("float", "double")):
        if isinstance(val, float) and not math.isfinite(val):
            return None  # MySQL 不支持 NaN/Inf
        return val
    if mysql_type.startswith(("decimal", "numeric")):
        if isinstance(val, float):
            if not math.isfinite(val):
                return None
            return str(val)
        return val
    if isinstance(val, uuid.UUID):
        return str(val)
    return val


# ---------------------------------------------------------------------------
# 数据拷贝
# ---------------------------------------------------------------------------
def _iter_chunks(rows: list[list[Any]], ncol: int, chunk_bytes: int):
    """把一批行按估算字节阈值再切成若干小批，避免单条 SQL 超 max_allowed_packet。"""
    if not rows:
        return
    start = 0
    cur = 0
    for i, row in enumerate(rows):
        size = 16 * ncol
        for v in row:
            if isinstance(v, str):
                size += len(v)
            elif isinstance(v, (bytes, memoryview)):
                size += len(bytes(v))
            elif isinstance(v, (int, float)):
                size += 16
            else:
                size += 32
        cur += size
        if cur >= chunk_bytes:
            yield rows[start : i + 1]
            start = i + 1
            cur = 0
    if start < len(rows):
        yield rows[start:]


def _exec_insert(cur: Any, table: str, cols: list[str], rows: list[list[Any]],
                 ignore: bool, chunk_bytes: int) -> int:
    """执行多行 VALUES 插入；单批失败时折半重试，返回成功行数。"""
    ncol = len(cols)
    inserted = 0
    for batch in _iter_chunks(rows, ncol, chunk_bytes):
        nb = len(batch)
        placeholders = ",".join(["%s"] * ncol)
        values_sql = ",".join(["(%s)" % placeholders] * nb)
        sql = "%s INTO `%s` (`%s`) VALUES %s" % (
            "INSERT IGNORE" if ignore else "INSERT",
            table,
            "`,`".join(cols),
            values_sql,
        )
        params = [v for row in batch for v in row]
        try:
            cur.execute(sql, params)
            inserted += max(cur.rowcount, 0)
        except Exception:
            if nb <= 1:
                raise
            half = (nb + 1) // 2
            inserted += _exec_insert(cur, table, cols, batch[:half], ignore, chunk_bytes // 2)
            inserted += _exec_insert(cur, table, cols, batch[half:], ignore, chunk_bytes // 2)
    return inserted


def migrate_table(cfg: dict[str, Any], schema: str, table: str,
                  pg_conn: Any, my_conn: Any) -> dict[str, Any]:
    """迁移单表，返回统计信息。"""
    result: dict[str, Any] = {"table": table, "status": "ok", "rows": 0, "source": 0, "detail": ""}
    pg_cur = pg_conn.cursor()
    my_cur = my_conn.cursor()
    copy_cur = None
    try:
        meta = _load_table_meta(pg_cur, schema, table)
        if meta is None or not meta.columns:
            raise RuntimeError("PG 中不存在该表或表无列")
        cols = meta.columns
        mysql_types = [map_mysql_type(c) for c in cols]
        bad = [cols[i].name for i, mt in enumerate(mysql_types) if mt is None]
        if bad:
            raise RuntimeError(
                "以下列类型不支持自动迁移，请排除该表后人工处理: %s"
                % ", ".join(bad)
            )

        pg_cur.execute('SELECT COUNT(*) FROM "%s"."%s"' % (schema, table))
        src_count = int(pg_cur.fetchone()[0])
        result["source"] = src_count

        # 目标表存在性 & 处理策略
        my_cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        exists = my_cur.fetchone()["c"] > 0
        if exists:
            if cfg["recreate"]:
                my_cur.execute("DROP TABLE `%s`" % table)
                exists = False
                result["detail"] = "目标表已 DROP 重建"
            elif cfg["skip_existing"]:
                result["status"] = "skip"
                result["detail"] = "目标表已存在(--skip-existing)"
                return result
            elif cfg["truncate_first"]:
                my_cur.execute("TRUNCATE TABLE `%s`" % table)
                result["detail"] = "目标表已 TRUNCATE"

        if not exists:
            create_sql = _build_create_sql(meta, cfg["charset"], cfg["collation"])
            print("  [DDL] 在目标库创建表 %s" % table)
            for ln in create_sql.splitlines():
                print("        %s" % ln.strip())
            my_cur.execute(create_sql)

        # 流式读取 + 批量插入
        col_names = [c.name for c in cols]
        quoted_cols = ", ".join('"%s"' % c for c in col_names)
        select_sql = 'SELECT %s FROM "%s"."%s"' % (quoted_cols, schema, table)

        if cfg["server_cursor"]:
            # 命名游标要求连接处于事务中（autocommit=False）
            copy_cur = pg_conn.cursor(name="pg2mysql_cursor")
            copy_cur.itersize = cfg["batch_size"]
        else:
            copy_cur = pg_conn.cursor()
        copy_cur.execute(select_sql)

        inserted = 0
        last_report = 0
        t0 = time.time()
        while True:
            rows = copy_cur.fetchmany(cfg["batch_size"])
            if not rows:
                break
            converted = []
            for row in rows:
                conv: list[Any] = []
                for v, mt in zip(row, mysql_types):
                    if mt is None:  # 前面已拦截，理论上不会到这里
                        raise RuntimeError("内部错误：列类型未映射")
                    conv.append(convert_value(v, mt, cfg["tz"]))
                converted.append(conv)
            inserted += _exec_insert(
                my_cur, table, col_names, converted,
                ignore=(cfg["on_duplicate"] == "ignore"),
                chunk_bytes=cfg["chunk_bytes"],
            )
            if inserted - last_report >= cfg["progress_every"]:
                last_report = inserted
                print(
                    "  [copy] %s: %,d/%,d 行 (%.1fs)"
                    % (table, inserted, src_count, time.time() - t0),
                    flush=True,
                )
        result["rows"] = inserted
        if inserted != src_count and cfg["on_duplicate"] != "ignore":
            result["status"] = "warn"
            result["detail"] = "源 %d 行 / 导入 %d 行不一致" % (src_count, inserted)
        return result
    finally:
        if copy_cur is not None:
            try:
                copy_cur.close()
            except Exception:
                pass
        try:
            pg_cur.close()
        except Exception:
            pass
        try:
            my_cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _parse_names(arg: str) -> list[str]:
    return [t.strip() for t in arg.split(",") if t.strip()]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="PostgreSQL -> MySQL 定向表迁移",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="详细示例见脚本头部 docstring。",
    )
    ap.add_argument("--pg-url", required=True, help="PG DSN，如 postgresql://u:p@host:5432/db")
    ap.add_argument("--mysql-url", required=True, help="MySQL DSN，如 mysql://u:p@host:3306/db")
    ap.add_argument("--pg-schema", default=None, help="PG schema，默认 current_schema()（通常 public）")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--tables", help="逗号分隔的表名列表")
    grp.add_argument("--all", action="store_true", help="迁移目标库全部基础表")
    ap.add_argument("--exclude", help="配合 --all 使用，逗号分隔排除的表")
    ap.add_argument("--recreate", action="store_true", help="目标表存在则 DROP 后重建再导入")
    ap.add_argument("--truncate-first", action="store_true", help="导入前 TRUNCATE 目标表（假定结构已就绪）")
    ap.add_argument("--skip-existing", action="store_true", help="目标表已存在则跳过")
    ap.add_argument("--on-duplicate", choices=("abort", "ignore"), default="abort",
                    help="唯一键冲突处理：abort 报错回滚该表 / ignore 跳过重复行（适合续跑）")
    ap.add_argument("--batch-size", type=int, default=500, help="每批读取行数（默认 500）")
    ap.add_argument("--chunk-bytes", type=int, default=8 * 1024 * 1024,
                    help="单条 INSERT 语句最大估算字节数（默认 8MB）")
    ap.add_argument("--charset", default="utf8mb4", help="目标表字符集（默认 utf8mb4）")
    ap.add_argument("--collation", default="utf8mb4_unicode_ci", help="目标表 collation")
    ap.add_argument("--target-tz", default=None,
                    help="timestamptz 落库目标时区（默认本机时区），如 Asia/Shanghai")
    ap.add_argument("--server-cursor", action="store_true",
                    help="PG 服务端游标流式读取（省内存；占用一条长事务）")
    ap.add_argument("--stop-on-error", action="store_true", help="单表失败即停止")
    ap.add_argument("--verbose", action="store_true", help="出错时打印完整堆栈")
    args = ap.parse_args(argv)

    if psycopg2 is None or pymysql is None:
        print("缺少依赖，请先执行: pip install psycopg2-binary PyMySQL", file=sys.stderr)
        return 2

    pg_info = parse_dsn(args.pg_url, "postgresql")
    my_info = parse_dsn(args.mysql_url, "mysql")
    if args.tables:
        tables = _parse_names(args.tables)
        exclude = set()
    else:
        exclude = set(_parse_names(args.exclude or ""))
        tables = None

    cfg = {
        "recreate": args.recreate,
        "truncate_first": args.truncate_first,
        "skip_existing": args.skip_existing,
        "on_duplicate": args.on_duplicate,
        "batch_size": max(1, args.batch_size),
        "chunk_bytes": max(64 * 1024, args.chunk_bytes),
        "charset": args.charset,
        "collation": args.collation,
        "tz": _resolve_tz(args.target_tz),
        "server_cursor": args.server_cursor,
        "progress_every": 100000,
    }

    print("PG   : %s@%s:%s/%s" % (pg_info["user"], pg_info["host"], pg_info["port"], pg_info["database"]))
    print("MySQL: %s@%s:%s/%s" % (my_info["user"], my_info["host"], my_info["port"], my_info["database"]))

    pg_conn = _connect_pg(pg_info)
    my_conn = _connect_mysql(my_info, args.charset)
    try:
        pg_conn.autocommit = not args.server_cursor
        pg_cur = pg_conn.cursor()
        if args.pg_schema:
            schema = args.pg_schema
        else:
            pg_cur.execute(_SCHEMA_SQL)
            schema = pg_cur.fetchone()[0] or "public"
        print("PG schema: %s" % schema)

        if tables is None:
            pg_cur.execute(_TABLES_SQL, {"schema": schema})
            tables = [r[0] for r in pg_cur.fetchall() if r[0] not in exclude]
        else:
            # 幂等去重，保持顺序
            seen = set()
            tables = [t for t in tables if not (t in seen or seen.add(t))]
        pg_cur.close()

        print("共 %d 张表: %s\n" % (len(tables), ", ".join(tables)))
        summary = []
        for table in tables:
            print("==> 迁移表 %s.%s" % (schema, table), flush=True)
            try:
                summary.append(migrate_table(cfg, schema, table, pg_conn, my_conn))
                my_conn.commit()
            except Exception as e:  # noqa: BLE001
                my_conn.rollback()
                if args.verbose:
                    traceback.print_exc()
                print("  [error] %s: %s" % (table, e), file=sys.stderr)
                summary.append(
                    {"table": table, "status": "error", "rows": 0,
                     "source": 0, "detail": str(e)}
                )
                if args.stop_on_error:
                    break

        print("\n================= 迁移汇总 =================")
        failed = 0
        for s in summary:
            flag = {"ok": "OK", "skip": "SKIP", "warn": "WARN", "error": "FAIL"}[s["status"]]
            extra = (" | " + s["detail"]) if s.get("detail") else ""
            print("  [%s] %-28s 源 %10s 行 -> 导入 %10s 行%s" % (
                flag, s["table"],
                "{:,}".format(s.get("source", 0)),
                "{:,}".format(s.get("rows", 0)),
                extra,
            ))
            if s["status"] in ("error", "warn"):
                failed += 1
        print("============================================")
        if args.server_cursor:
            pg_conn.commit()  # 结束服务端游标事务
        if failed:
            print("存在 %d 张表异常，请查看上方日志。" % failed, file=sys.stderr)
            return 1
        print("全部完成。")
        return 0
    finally:
        try:
            pg_conn.close()
        except Exception:
            pass
        try:
            my_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
