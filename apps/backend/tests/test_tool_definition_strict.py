"""ToolDefinition 模型可见结构不变量测试。

守护两件事：
1. ``to_model_tool_definition()`` 的 name/description 必须取自本类契约，
   不被 pydantic ``args_model.__name__`` 污染；且返回裸 function 形状
   （``{"name", "description", "parameters"}``），``parameters`` 为原始
   ``args_model.model_json_schema()``（**非** strict 化）。
2. strict 化（强制全 ``required``、递归 ``additionalProperties: false``）的
   **唯一事实来源是下游 ``bind_tools(strict=True)``**。本测试通过模拟
   ``bind_tools`` 的内部行为（对 ``model_tools_to_langchain`` 的输出再调
   ``convert_to_openai_tool(..., strict=True)``）验证：strict 在下游生效，
   且工具名仍为本类注册名（不被 pydantic 类名覆盖）。
"""

from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.llm.langchain_bridge import model_tools_to_langchain
from app.tools.tool_handler.replace_tool import build_replace_definition
from app.tools.tool_handler.search_files import build_search_files_definition


def test_name_not_polluted_by_args_model_class() -> None:
    definition = build_search_files_definition()
    model_def = definition.to_model_tool_definition()
    # 注册名 search_files 必须保留，而非 pydantic 类名 SearchFilesArgs
    assert model_def["name"] == "search_files"
    assert model_def["name"] != "SearchFilesArgs"
    assert model_def["description"] == definition.description


def test_to_model_tool_definition_shape() -> None:
    definition = build_search_files_definition()
    model_def = definition.to_model_tool_definition()
    assert set(model_def.keys()) == {"name", "description", "parameters"}


def test_projection_uses_raw_model_json_schema_not_strict() -> None:
    # to_model_tool_definition 只做投影，parameters 直接来自
    # args_model.model_json_schema()，不应预先 strict 化
    # （strict 化交由下游 bind_tools(strict=True) 统一承担）。
    definition = build_search_files_definition()
    parameters = definition.to_model_tool_definition()["parameters"]
    assert parameters == definition.args_model.model_json_schema()


def test_bind_tools_strict_applied_downstream_preserves_name() -> None:
    # 模拟 bind_tools 的内部行为：对 model_tools_to_langchain 的输出再调
    # convert_to_openai_tool(strict=True)。验证 strict 在下游生效，且 name
    # 仍为本类注册名（不被 pydantic 类名覆盖）。
    definition = build_replace_definition()
    projected = model_tools_to_langchain([definition])[0]
    strict_function = convert_to_openai_tool(projected, strict=True)
    parameters = strict_function["function"]["parameters"]

    # strict 生效：顶层 additionalProperties 为 false，所有 property 进 required
    assert parameters.get("additionalProperties") is False
    required = set(parameters.get("required", []))
    properties = set(parameters.get("properties", {}).keys())
    assert required == properties

    # name 仍为注册名而非 pydantic 类名
    assert strict_function["function"]["name"] == definition.name
    assert strict_function["function"]["name"] != definition.args_model.__name__


def test_normalized_parameters_schema_consistent_with_projection() -> None:
    definition = build_search_files_definition()
    normalized = definition.normalized()
    projected_parameters = definition.to_model_tool_definition()["parameters"]
    # normalized 派生的 schema 与投影来源一致（单一事实来源：args_model.model_json_schema()）
    assert normalized.parameters_schema == projected_parameters
