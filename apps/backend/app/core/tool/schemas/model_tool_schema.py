"""工具模型可见 schema 的统一生成边界。"""

from openai import pydantic_function_tool
from openai.types.shared_params import FunctionParameters
from pydantic import BaseModel

# 取值本身是子 schema 的关键字，与取值是子 schema 列表的关键字；``properties`` / ``$defs`` 另算，
# 它们的键是字段名或定义名，不是 schema 注解。
_SUBSCHEMA_KEYS = ("items", "additionalProperties", "not")
_SUBSCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")


def strict_parameters_schema(args_model: type[BaseModel]) -> FunctionParameters:
    """复用 SDK 生成封闭对象及全量 required，不自行维护 strict 转换算法。

    默认值仍服务本地调用；模型请求必须显式填写，只有可空字段允许 null。
    平台投影在此结果上收窄枚举或补充说明，再随 Session 固化，不能另生非 strict schema。

    出口只留对模型有用的内容：
    - 模型类 docstring 不进入结果：它由 pydantic 写成定义级 description，会让开发注释
      变成模型可见文本，并与字段描述构成第二个描述源。模型可见说明只来自字段描述。
    - pydantic 给每个字段和模型生成的 ``title``（字段名或类名的人读形式）不进入结果：它不带
      任何字段名之外的信息，全部工具加起来却有几千字符，每次请求都随工具定义发出。
    """
    parameters = pydantic_function_tool(args_model)["function"]["parameters"]
    _drop_model_level_descriptions(parameters)
    _drop_titles(parameters)
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


def _drop_titles(schema: object) -> None:
    """就地删除每个 schema 节点自身的 ``title`` 注解。

    只沿 schema 结构下行：``properties`` 与 ``$defs`` 的键是字段名与定义名，叫 ``title`` 的业务字段
    在那里是键，不会被当成注解删掉。
    """

    if not isinstance(schema, dict):
        return
    schema.pop("title", None)
    for container in ("properties", "$defs"):
        children = schema.get(container)
        if isinstance(children, dict):
            for child in children.values():
                _drop_titles(child)
    for key in _SUBSCHEMA_KEYS:
        _drop_titles(schema.get(key))
    for key in _SUBSCHEMA_LIST_KEYS:
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                _drop_titles(child)
