# -*- coding: utf-8 -*-
"""bocomadp 自定义事件投影器。

框架的 :class:`ChatService` 在构造时接受 ``extra_projectors``，
但由于 ChatService 由框架 lifespan 构造（早于本模块可介入的时机），
这里约定：启动时在 ``main.py`` 的 lifespan 里把投影器
append 到 ``app.state.chat_service._projectors``。
"""
from ._worker_failure_notifier import WorkerFailureNotifier

__all__ = ["WorkerFailureNotifier"]
