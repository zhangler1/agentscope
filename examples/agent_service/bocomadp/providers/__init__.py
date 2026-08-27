"""ELLM 网关协议适配与 API key 生命周期管理。

- :class:`EllmChatModel`（``ellm_chat_model.py``）：ELLM 网关的
  OpenAI 兼容协议适配层（``<think>`` 注入、401 重试、候选模型等）。
- :mod:`ellm_key`：``fetch_ellm_key`` 取 key 原语 +
  :class:`EllmKeyRefresher` 惰性刷新状态机。

模型装配与路由不在此层：内置模型条目（``bocomadp.config`` 的
``load_model_entries``）由启动时 ``ensure_default_credentials`` 幂等
刷入 storage，会话运行时由框架按 ``ChatModelConfig`` 重建模型实例。
"""

from .ellm_key import EllmKeyRefresher, fetch_ellm_key

__all__ = [
    "fetch_ellm_key",
    "EllmKeyRefresher",
]
