# -*- coding: utf-8 -*-
"""扫描一个总文件夹下的所有 skill 文件夹，逐个上传到指定 agent 会话的 workspace。

调用后端接口（等价于 Web UI 的「选择文件夹上传 skill」）::

    POST {API_BASE}/workspace/skill/upload?agent_id=..&session_id=..
    multipart/form-data:
        manifest = {"entries": [{"path": "<folder>/<rel>", "size": 123}, ...]}
        files    = 与 manifest.entries 顺序一致的文件夹内所有文件

每发现一个 skill 文件夹就单独发一次请求；某个失败不影响其余，最后汇总并
以非零码退出。服务端 ``_skill_upload._tar_stream`` 会边收边打成 tar 流解压
进 workspace，因此 manifest 里的 path/size 必须与实际文件严格一致（顺序也要
一致）。服务端另有以下校验（本脚本在本地先做一遍，避免白传）::

    - 所有 path 必须同属一个顶层目录（即 skill 文件夹名）；
    - 该目录根部必须有 ``SKILL.md``；
    - 文件数 <= 100、单文件 <= 50MB、总量 <= 500MB。

所有可变参数都是本文件顶部的代码变量：改参数只需编辑下方变量，无需命令行参数。

用法::

    # 批量：扫描 SKILLS_ROOT 下所有 skill 文件夹，逐个上传
    # 单个：把 SINGLE_SKILL_DIR 指向某个 skill 文件夹，只上传它
    # 编辑「配置区」的 AGENT_ID 等变量后直接运行
    python scripts/upload_skill.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# 配置区：按需修改
# ---------------------------------------------------------------------------

#: 后端接口基础地址（不带末尾斜杠）。上传接口为
#: ``POST {API_BASE}/workspace/skill/upload``。
#: 注意服务以 ``python main.py`` 启动时全部路由挂在 ``/api`` 下，
#: 故此处必须带 ``/api``；若用 ``uvicorn main:app`` 起内层 app 则去掉。
API_BASE = "http://localhost:8000/api"

#: 调用方用户 ID，作为 ``X-User-ID`` 请求头（接口 get_current_user_id 必需）。
USER_ID = "admin"

#: 目标 agent ID：skill 会被装进该 agent 某个会话的 workspace。
AGENT_ID = "_agent-creator"

#: 目标会话 ID。留空（""）时脚本自动复用/创建该 agent 的一个会话；
#: 批量上传时所有 skill 共用同一个会话（只解析一次）。
SESSION_ID = "e5822dddfbba47a6910e282a371a5920"

#: 装着所有 skill 文件夹的总文件夹（相对本仓库或绝对路径均可）。
#: 每个 skill 文件夹的根部必须有 ``SKILL.md``，该文件 YAML frontmatter
#: 里的 ``name`` 才是最终 skill 名。若该路径自身就是一个含 ``SKILL.md``
#: 的 skill 文件夹，则只上传它一个。
SKILLS_ROOT = ""

#: 直接上传的**单个** skill 文件夹（相对本仓库或绝对路径均可）。
#: 非空（"" 以外）时只上传该文件夹，忽略 SKILLS_ROOT / RECURSIVE；
#: 留空（""）时回到批量扫描模式（原有功能）。文件夹根部需有 ``SKILL.md``。
SINGLE_SKILL_DIR = "/home/lwh/project/agentscope/agent_creator_skill/agent-factory"

#: 是否递归下钻查找 skill 文件夹。
#: ``False``：只把 ``SKILLS_ROOT`` 的直接子目录当作 skill（推荐，结构清晰）；
#: ``True``：在任意深度查找含 ``SKILL.md`` 的目录，找到后不再深入其子目录。
RECURSIVE = False

#: 全部上传完成后是否调用 ``GET /workspace/skill`` 列出 workspace 内 skill 校验。
VERIFY_AFTER_UPLOAD = True

#: 单次请求超时（秒）。skill 较大或网络较慢时调大。
TIMEOUT = 300

#: 跳过上传的名字（按路径中任一段精确匹配）。版本控制 / 缓存 / 编辑器残留
#: 不应随 skill 走，否则既浪费配额又可能被服务端的路径校验拒绝。
EXCLUDE_NAMES = {
    ".git",
    ".svn",
    ".hg",
    ".DS_Store",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    ".venv",
    "node_modules",
}

#: 额外请求头，如 ``{"guwpToken": "..."}``。按需添加，默认空。
EXTRA_HEADERS: dict[str, str] = {}

#: 服务端硬限制（与 ``agentscope.app._service._skill_upload`` 保持一致）。
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILE_COUNT = 100

# ---------------------------------------------------------------------------
# 以下无需改动
# ---------------------------------------------------------------------------

#: 读取本地文件的分块大小（64 KiB）。
_READ_CHUNK = 64 * 1024

#: multipart 中文件名保留的后缀长度上限（仅作可读性，服务端不依赖文件名）。
_SUFFIX_MAX = 12

#: 每行的缩进，用于展开单个 skill 的明细。
_INDENT = "    "


class SkillError(ValueError):
    """单个 skill 无法上传（本地预检不通过）。"""


def _die(message: str) -> None:
    """打印致命错误并退出，返回码 1。"""
    print(f"[error] {message}", file=sys.stderr)
    raise SystemExit(1)


def _base() -> str:
    """返回去掉末尾斜杠的接口基础地址。"""
    return API_BASE.rstrip("/")


def _human_size(num: int) -> str:
    """把字节数格式化成人类可读字符串。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _find_skill_dirs(
    root: str,
    recursive: bool,
) -> tuple[list[str], list[str]]:
    """在总文件夹下找出所有 skill 文件夹。

    Args:
        root (`str`): 总文件夹绝对路径。
        recursive (`bool`): 是否递归下钻查找。

    Returns:
        `tuple[list[str], list[str]]`: ``(skill 目录列表, 被跳过的目录名列表)``。
        被跳过的仅在非递归模式下统计（即直接子目录里不含 ``SKILL.md`` 的那些）。
    """
    if os.path.isfile(os.path.join(root, "SKILL.md")):
        return [root], []

    found: list[str] = []
    skipped: list[str] = []
    if recursive:
        for dirpath, dirs, _ in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES)
            if dirpath == root:
                continue
            if os.path.isfile(os.path.join(dirpath, "SKILL.md")):
                found.append(dirpath)
                # 命中的 skill 不再下钻，避免嵌套目录被重复上传。
                dirs[:] = []
        return found, skipped

    for name in sorted(os.listdir(root)):
        if name in EXCLUDE_NAMES:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if os.path.isfile(os.path.join(path, "SKILL.md")):
            found.append(path)
        else:
            skipped.append(name)
    return found, skipped


def _collect_files(skill_dir: str) -> list[tuple[str, str, int]]:
    """收集 skill 文件夹内所有待上传文件。

    Args:
        skill_dir (`str`): skill 文件夹绝对路径。

    Returns:
        `list[tuple[str, str, int]]`: ``[(相对路径, 绝对路径, 字节数), ...]``，
        相对路径统一用 ``/`` 分隔且已按字典序排序，保证多次运行结果稳定。
    """
    collected: list[tuple[str, str, int]] = []
    for root, dirs, names in os.walk(skill_dir):
        # 原地裁剪，os.walk 便不会进入被排除的目录。
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_NAMES)
        for name in sorted(names):
            if name in EXCLUDE_NAMES:
                continue
            abs_path = os.path.join(root, name)
            rel_path = os.path.relpath(abs_path, skill_dir).replace(os.sep, "/")
            collected.append((rel_path, abs_path, os.path.getsize(abs_path)))
    return collected


def _check_limits(files: list[tuple[str, str, int]]) -> None:
    """按服务端的限制在本地预检。

    Args:
        files (`list[tuple[str, str, int]]`): :func:`_collect_files` 的结果。

    Raises:
        `SkillError`: 文件为空、数量或大小超过服务端上限。
    """
    if not files:
        raise SkillError("文件夹内没有可上传的文件")
    if len(files) > MAX_FILE_COUNT:
        raise SkillError(f"文件数 {len(files)} 超过上限 {MAX_FILE_COUNT}")
    total = sum(size for _, _, size in files)
    if total > MAX_TOTAL_BYTES:
        raise SkillError(
            f"总大小 {_human_size(total)} 超过上限 "
            f"{_human_size(MAX_TOTAL_BYTES)}",
        )
    for rel_path, _, size in files:
        if size > MAX_FILE_BYTES:
            raise SkillError(
                f"{rel_path} 大小 {_human_size(size)} 超过单文件上限 "
                f"{_human_size(MAX_FILE_BYTES)}",
            )


def _read_skill_meta(skill_path: str) -> tuple[str, str]:
    """从 ``SKILL.md`` 的 YAML frontmatter 里粗读 name / description。

    仅用于日志展示，不参与任何校验（真正的解析在服务端）。读不到就返回空串。

    Args:
        skill_path (`str`): ``SKILL.md`` 绝对路径。

    Returns:
        `tuple[str, str]`: ``(name, description)``，缺失时为空串。
    """
    try:
        with open(skill_path, "r", encoding="utf-8") as fp:
            head = fp.read(4096)
    except OSError:
        return "", ""

    match = re.match(r"^---\s*\n(.*?)\n---", head, re.DOTALL)
    if not match:
        return "", ""

    name = ""
    description = ""
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key == "name" and not name:
            name = value
        elif key == "description" and not description:
            description = value
    return name, description


def _ascii_filename(rel_path: str) -> str:
    """把相对路径压成纯 ASCII 的 multipart 文件名。

    服务端只认 manifest 里的 path，multipart 的 filename 纯属占位，
    因此这里把所有非 ASCII 字符替换掉，避免非法请求头字节。

    Args:
        rel_path (`str`): 文件相对路径，如 ``references/a.md``。

    Returns:
        `str`: 形如 ``references_a.md`` 的 ASCII 文件名。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", rel_path)
    base, ext = os.path.splitext(safe)
    if len(base) > _SUFFIX_MAX:
        base = base[-_SUFFIX_MAX:]
    return f"{base}{ext}" or "part"


def _build_multipart_body(
    manifest: str,
    parts: list[tuple[str, str]],
) -> tuple[bytes, str]:
    """构造 multipart/form-data 请求体。

    Args:
        manifest (`str`): ``manifest`` 字段的 JSON 字符串。
        parts (`list[tuple[str, str]]`):
            ``[(multipart 文件名, 本地文件绝对路径), ...]``，顺序必须与
            manifest.entries 完全一致（服务端按顺序 zip 组装 tar）。

    Returns:
        `tuple[bytes, str]`: ``(请求体字节, boundary)``。
    """
    boundary = "----AgentScopeSkillUpload" + uuid.uuid4().hex
    delimiter = f"--{boundary}\r\n".encode("ascii")
    buf = io.BytesIO()

    # 1) manifest 文本字段
    buf.write(delimiter)
    buf.write(b'Content-Disposition: form-data; name="manifest"\r\n\r\n')
    buf.write(manifest.encode("utf-8"))
    buf.write(b"\r\n")

    # 2) files 文件字段：同名多值，顺序即 manifest.entries 顺序
    for filename, abs_path in parts:
        buf.write(delimiter)
        header = (
            "Content-Disposition: form-data; "
            f'name="files"; filename="{filename}"\r\n'
        )
        buf.write(header.encode("ascii"))
        buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
        with open(abs_path, "rb") as fp:
            while chunk := fp.read(_READ_CHUNK):
                buf.write(chunk)
        buf.write(b"\r\n")

    buf.write(f"--{boundary}--\r\n".encode("ascii"))
    return buf.getvalue(), boundary


def _call(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, str]:
    """发一个 HTTP 请求，把非 2xx 与网络错误折成 ``(状态码, 文本)``。

    Args:
        method (`str`): HTTP 方法。
        url (`str`): 完整 URL。
        body (`bytes | None`): 请求体。
        content_type (`str | None`): 有请求体时显式声明内容类型。

    Returns:
        `tuple[int, str]`: ``(HTTP 状态码, 响应体文本)``；网络失败返回
        状态码 ``0``。
    """
    headers = {"X-User-ID": USER_ID}
    headers.update(EXTRA_HEADERS)
    if content_type:
        headers["Content-Type"] = content_type

    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def _resolve_session_id() -> str:
    """返回目标会话 ID：显式配置优先，否则复用或新建一个会话。

    skill 相关端点都要先由 ``(user_id, agent_id, session_id)`` 解析出
    workspace，所以操作前必须保证该 agent 至少有一个会话。

    Returns:
        `str`: 会话 ID。
    """
    configured = SESSION_ID.strip()
    if configured:
        print(f"[session] 使用配置的会话: {configured}")
        return configured

    list_url = (
        f"{_base()}/sessions/?agent_id={urllib.parse.quote(AGENT_ID)}"
    )
    status, text = _call("GET", list_url)
    if status == 200:
        try:
            sessions = json.loads(text).get("sessions") or []
        except json.JSONDecodeError:
            sessions = []
        if sessions:
            session_id = (sessions[0].get("session") or {}).get("id", "")
            if session_id:
                print(f"[session] 复用已有会话: {session_id}")
                return session_id

    create_url = f"{_base()}/sessions/"
    payload = json.dumps({"agent_id": AGENT_ID}).encode("utf-8")
    status, text = _call(
        "POST",
        create_url,
        body=payload,
        content_type="application/json",
    )
    if status not in (200, 201):
        _die(f"创建会话失败 (HTTP {status}): {text}")
    try:
        session_id = json.loads(text).get("session_id", "")
    except json.JSONDecodeError:
        session_id = ""
    if not session_id:
        _die(f"创建会话返回异常: {text}")
    print(f"[session] 新建会话: {session_id}")
    return session_id


def _upload_one(skill_dir: str, session_id: str) -> str:
    """上传一个 skill 文件夹。

    Args:
        skill_dir (`str`): skill 文件夹绝对路径。
        session_id (`str`): 已解析好的目标会话 ID。

    Returns:
        `str`: 成功返回空串；失败返回错误描述（由调用方打印并计数）。
    """
    root_name = os.path.basename(os.path.normpath(skill_dir))
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        return f"缺少 {root_name}/SKILL.md"

    files = _collect_files(skill_dir)
    try:
        _check_limits(files)
    except SkillError as exc:
        return str(exc)

    name, description = _read_skill_meta(skill_md)
    total = sum(size for _, _, size in files)
    print(f"{_INDENT}目录: {skill_dir}")
    print(f"{_INDENT}name={name or '(未从 frontmatter 读到)'}")
    if description:
        print(f"{_INDENT}description={description}")
    print(f"{_INDENT}{len(files)} 个文件, {_human_size(total)}")

    # 服务端要求所有 path 同属一个顶层目录，且根下有 SKILL.md。
    parts: list[tuple[str, str]] = []
    entries: list[dict[str, object]] = []
    for rel_path, abs_path, size in files:
        parts.append((_ascii_filename(rel_path), abs_path))
        entries.append({"path": f"{root_name}/{rel_path}", "size": size})
    manifest = json.dumps({"entries": entries}, ensure_ascii=False)
    body, boundary = _build_multipart_body(manifest, parts)

    url = (
        f"{_base()}/workspace/skill/upload"
        f"?agent_id={urllib.parse.quote(AGENT_ID)}"
        f"&session_id={urllib.parse.quote(session_id)}"
    )
    status, text = _call(
        "POST",
        url,
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if status not in (200, 201):
        return f"HTTP {status}: {text}"

    print(f"{_INDENT}[ok] 上传成功 (HTTP {status})")
    return ""


def _verify(agent_id: str, session_id: str) -> None:
    """上传后列出 workspace 内 skill，作为成功与否的旁证。"""
    url = (
        f"{_base()}/workspace/skill"
        f"?agent_id={urllib.parse.quote(agent_id)}"
        f"&session_id={urllib.parse.quote(session_id)}"
    )
    status, text = _call("GET", url)
    if status != 200:
        print(f"[warn] 校验失败 (HTTP {status}): {text}", file=sys.stderr)
        return
    try:
        skills = json.loads(text)
    except json.JSONDecodeError:
        print(f"[warn] 校验返回非 JSON: {text}", file=sys.stderr)
        return
    if not skills:
        print("[verify] workspace 内暂无 skill（上传可能未生效）")
        return
    print(f"[verify] workspace 内 skill 共 {len(skills)} 个:")
    for skill in skills:
        print(
            f"         - {skill.get('name', '?')}: "
            f"{skill.get('description', '')}",
        )


def main() -> int:
    """脚本入口：单个或批量扫描 -> 逐个上传 -> 汇总 -> 可选校验。"""
    if not AGENT_ID.strip():
        _die("AGENT_ID 为空：请先在脚本顶部填写目标 agent ID")

    single = SINGLE_SKILL_DIR.strip()
    skipped: list[str] = []
    if single:
        # 单个模式：只上传指定的 skill 文件夹，忽略 SKILLS_ROOT / RECURSIVE。
        skill_dir = os.path.abspath(os.path.expanduser(single))
        if not os.path.isdir(skill_dir):
            _die(f"SINGLE_SKILL_DIR 不是文件夹: {skill_dir}")
        skill_dirs = [skill_dir]
        print(f"[mode] 单个 skill 上传: {skill_dir}")
    else:
        root = os.path.abspath(os.path.expanduser(SKILLS_ROOT))
        if not os.path.isdir(root):
            _die(f"SKILLS_ROOT 不是文件夹: {root}")

        skill_dirs, skipped = _find_skill_dirs(root, RECURSIVE)
        if not skill_dirs:
            _die(f"未在 {root} 下找到含 SKILL.md 的 skill 文件夹")

        print(f"[scan] 总文件夹: {root}")
        print(f"[scan] 发现 {len(skill_dirs)} 个 skill 文件夹"
              f"（{'递归' if RECURSIVE else '仅直接子目录'}）")
        if skipped:
            shown = ", ".join(skipped[:5])
            more = f" 等 {len(skipped)} 个" if len(skipped) > 5 else ""
            print(f"[scan] 跳过不含 SKILL.md 的子目录: {shown}{more}")

    session_id = _resolve_session_id()

    total = len(skill_dirs)
    failed = 0
    for index, skill_dir in enumerate(skill_dirs, start=1):
        label = f"[{index}/{total}]"
        name = os.path.basename(os.path.normpath(skill_dir))
        print(f"{label} {name}")
        error = _upload_one(skill_dir, session_id)
        if error:
            failed += 1
            print(f"{_INDENT}[fail] {error}", file=sys.stderr)

    print(f"[summary] 成功 {total - failed} / {total} 个")
    if VERIFY_AFTER_UPLOAD:
        _verify(AGENT_ID, session_id)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
