# -*- coding: utf-8 -*-
"""行内模型平台配置包（bocom-as 顶层子包）。

业务代码统一从 ``config`` 导入，如::

    from config import get_ellm_settings

环境变量驱动：读取优先级为**进程环境变量 > 代码默认值**；``.env`` 由宿主
应用加载（本包不主动加载，保持职责单一）。
"""
from .app_config import EllmSettings, get_ellm_settings

__all__ = ["EllmSettings", "get_ellm_settings"]
