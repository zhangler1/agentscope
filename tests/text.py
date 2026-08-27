from pydantic import BaseModel, field_validator, model_validator

class InjectionConfig(BaseModel):
    template: str
    max_len: int
    min_len: int

    # 1. 模型 before：处理原始输入dict
    @model_validator(mode="before")
    @classmethod
    def pre_raw(cls, data):
        print("[model before] 原始data:", data)
        return data

    # 2. 字段 before：template 原始值预处理（类型转换之前）
    @field_validator("template", mode="before")
    @classmethod
    def pre_template(cls, raw_val):
        print("[field before template] raw_val =", raw_val)
        return raw_val

    # 3. field_validator 默认after：类型转换完成后校验（你的业务代码）
    @field_validator("template")
    @classmethod
    def check_template(cls, value: str):
        print("[field after template] value =", value)
        if "{runtime_state}" not in value:
            raise ValueError("template must contain {runtime_state}")
        return value

    # 4. model after：实例完成，跨字段校验
    @model_validator(mode="after")
    def cross_validate(self):
        print("[model after] self实例", self.template, self.max_len, self.min_len)
        if self.max_len < self.min_len:
            raise ValueError("max_len不能小于min_len")
        return self


# 调用实例化
cfg = InjectionConfig(
    template="abc {runtime_state} xyz",
    max_len=1000,
    min_len=100
)