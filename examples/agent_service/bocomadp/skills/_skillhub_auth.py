# -*- coding: utf-8 -*-
"""SkillHub 第三方登录的 AES 凭证生成（本地实现）。

与 BocomWork 前端的 ``lib/skillhub/token.ts`` 算法完全一致：

.. code-block:: text

    key       = <北京时间 YYYYMMDD>(8 字节) + <channelRand>(8 字节) = 16 字节
    plaintext = <OA 账号> + "#" + <13 位毫秒时间戳>
    token     = hex( AES-128-ECB / PKCS7(key, plaintext) )

配置项（环境变量）：

- ``SKILLHUB_CHANNEL_RAND``：密钥后 8 字节，必须是 **8 个 ASCII 字符**
  （缺省 ``abcdefgh``，即上游的测试值；本环境为 ``ygzzfyzh``）。
- ``SKILLHUB_PLATFORM``：登录请求体 ``platform`` / ``platForm`` 取值
  （缺省 ``BOCOMCODE``；本环境为 ``BOCOMWORK``）。

注意：token 有效期约 5 分钟，必须「用前现造」，不可缓存 token 本身。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

try:  # 运行时容器内有 agentscope；独立/离线场景（只装 cryptography）降级为 stdlib
    from agentscope._logging import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

#: channel rand 环境变量名（密钥后 8 字节）。
CHANNEL_RAND_ENV = "SKILLHUB_CHANNEL_RAND"

#: platform 环境变量名（登录请求体）。
PLATFORM_ENV = "SKILLHUB_PLATFORM"

#: channel rand 缺省值（上游测试值）。
DEFAULT_CHANNEL_RAND = "abcdefgh"

#: platform 缺省值。
DEFAULT_PLATFORM = "BOCOMCODE"

#: AES-128 密钥长度（字节）。
KEY_LEN = 16

#: 明文里 OA 与时间戳的分隔符。
OA_SEPARATOR = "#"


def channel_rand() -> str:
    """读取 channel rand（环境变量 → 缺省测试值）。"""
    return (
        (os.environ.get(CHANNEL_RAND_ENV) or "").strip()
        or DEFAULT_CHANNEL_RAND
    )


def skillhub_platform() -> str:
    """读取登录请求体的 platform 取值（环境变量 → 缺省值）。"""
    return (os.environ.get(PLATFORM_ENV) or "").strip() or DEFAULT_PLATFORM


def beijing_day(timestamp_ms: int) -> str:
    """把毫秒时间戳转成**北京时间**的 ``YYYYMMDD``。

    实现与 ``token.ts::todayStr`` 等价：先把时间戳当 UTC，再加 8 小时取
    日期字段，因此结果与宿主机时区无关。
    """
    utc8 = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ) + timedelta(hours=8)
    return utc8.strftime("%Y%m%d")


def build_key(
    channel_rand_value: str | None = None,
    *,
    timestamp_ms: int | None = None,
) -> str:
    """构造 16 字节 AES 密钥：``<北京日期> + <channelRand>``。

    Args:
        channel_rand_value (`str | None`): 指定 channel rand；``None`` 时
            取 :func:`channel_rand`（环境变量）。
        timestamp_ms (`int | None`): 用于派生日期的毫秒时间戳；``None``
            时取当前时间。允许显式传入以便测试与「日期/时间戳同源」。

    Returns:
        `str`: 16 字节（UTF-8）的密钥字符串。

    Raises:
        ValueError: 密钥不是 16 字节（即 channel rand 不是 8 个 ASCII
            字符）。
    """
    rand = (
        channel_rand_value
        if channel_rand_value is not None
        else channel_rand()
    )
    ts = int(
        timestamp_ms if timestamp_ms is not None else time.time() * 1000,
    )
    key = f"{beijing_day(ts)}{rand}"
    key_bytes = len(key.encode("utf-8"))
    if key_bytes != KEY_LEN:
        raise ValueError(
            f"SkillHub key must be {KEY_LEN} bytes (got {key_bytes}): "
            f"{key!r} — {CHANNEL_RAND_ENV} must be exactly 8 ASCII chars",
        )
    return key


def build_auth_token(
    oa: str,
    *,
    timestamp_ms: int | None = None,
    channel_rand_value: str | None = None,
) -> str:
    """生成 AES 登录凭证：``hex(AES-128-ECB/PKCS7(f"{oa}#{ts}"))``。

    Args:
        oa (`str`): OA 账号（本工程即请求的 ``X-User-ID``）。
        timestamp_ms (`int | None`): 毫秒时间戳；``None`` 时取当前时间。
            **日期与明文共用同一个时间戳**，避免跨零点出现「key 用昨天、
            时间戳是今天」的不一致。
        channel_rand_value (`str | None`): 指定 channel rand；``None``
            时取环境变量。

    Returns:
        `str`: hex 编码的密文（直接作为登录请求体的 ``token``）。

    Raises:
        ValueError: OA 为空、包含分隔符 ``#``，或密钥长度不为 16 字节。
    """
    account = (oa or "").strip()
    if not account:
        raise ValueError("OA account is required to build a SkillHub token")
    if OA_SEPARATOR in account:
        raise ValueError(
            f"OA account must not contain {OA_SEPARATOR!r}: {account!r}",
        )

    ts = int(
        timestamp_ms if timestamp_ms is not None else time.time() * 1000,
    )
    key = build_key(channel_rand_value, timestamp_ms=ts)

    # 延迟导入：未安装 ``cryptography`` 的环境仍可 import 本模块
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    plaintext_text = f"{account}{OA_SEPARATOR}{ts}"
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext_text.encode("utf-8")) + padder.finalize()

    encryptor = Cipher(
        algorithms.AES(key.encode("utf-8")),
        modes.ECB(),
    ).encryptor()
    token = (encryptor.update(padded) + encryptor.finalize()).hex()

    # 每次生成凭证都留痕（含 token 与明文，属敏感信息，仅 DEBUG 级别输出：
    # 需要时用 BOCOMADP_LOG_LEVEL=debug 打开，生产建议保持 INFO）。
    logger.debug(
        "SkillHub AES credential generated: oa=%s key=%s plaintext=%s token=%s",
        account,
        key,
        plaintext_text,
        token,
    )
    return token
