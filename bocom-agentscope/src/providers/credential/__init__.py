# -*- coding: utf-8 -*-
"""自定义供应商凭证（ELLM）。

- :class:`ELLMCredential`: 自研 ELLM 供应商（模型 DeepSeek-V4-Flash，
  OpenAI 兼容端点）。
- 导入本包即完成 :class:`CredentialFactory` 注册（副作用，幂等），
  ``GET /credential/schemas`` 会自动包含它，前端无需改动。
"""
from agentscope.credential import CredentialFactory

from .ellm import ELLMCredential

# 注册自定义供应商：导入 providers.credential 即注册。注册前做幂等检查，
# 与同环境已有注册（如 bocomadp）共存时不重复注册同 type。
# 注意：type 是 pydantic 字段，不能直接类访问（pydantic 2.12 起
# 类属性不再暴露字段默认值，会抛 AttributeError）——经 model_fields 读取。
_ellm_type = ELLMCredential.model_fields["type"].default
if CredentialFactory.get_credential_class(_ellm_type) is None:
    CredentialFactory.register_credential(ELLMCredential)

__all__ = ["ELLMCredential"]
