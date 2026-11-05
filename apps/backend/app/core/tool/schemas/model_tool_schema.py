"""工具模型可见 schema 的统一生成边界。"""

from openai import pydantic_function_tool
from openai.types.shared_params import FunctionParameters
from pydantic import BaseModel


def strict_parameters_schema(args_model: type[BaseModel]) -> FunctionParameters:
    """复用 SDK 生成封闭对象及全量 required，不自行维护 strict 转换算法。

    默认值仍服务本地调用；模型请求必须显式填写，只有可空字段允许 null。
    平台投影在此结果上收窄枚举或补充说明，再随 Task 固化，不能另生非 strict schema。

    模型类 docstring 不进入结果：它由 pydantic 写成定义级 description，会让开发注释
    变成模型可见文本，并与字段描述构成第二个描述源。模型可见说明只来自字段描述。
    """
    parameters = pydantic_function_tool(args_model)["function"]["parameters"]
    _drop_model_level_descriptions(parameters)
    return parameters


def _drop_model_level_descriptions(schema: dict[str, object]) -> None:
    """就地删除根对象与 ``$defs`` 条目的 ``description``。

    ``properties`` 下的字段描述不受影响：那里才是模型可见说明的唯一位置。
    """

    schema.pop("description", None)
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        for definition in definitions.values():
            if isinstance(definition, dict):
                _drop_model_level_descriptions(definition)
