"""工具模型可见 schema 的统一生成边界。"""

from openai import pydantic_function_tool
from openai.types.shared_params import FunctionParameters
from pydantic import BaseModel


def strict_parameters_schema(args_model: type[BaseModel]) -> FunctionParameters:
    """复用 SDK 生成封闭对象及全量 required，不自行维护 strict 转换算法。

    默认值仍服务本地调用；模型请求必须显式填写，只有可空字段允许 null。
    平台投影在此结果上收窄枚举或补充说明，再随 Task 固化，不能另生非 strict schema。
    """
    return pydantic_function_tool(args_model)["function"]["parameters"]
