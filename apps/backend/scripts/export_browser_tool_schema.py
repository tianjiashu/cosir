"""导出浏览器工具参数模型的 JSON Schema，作为宿主校验 ``browser.*`` 请求参数的唯一契约。

参数的唯一来源是后端 Pydantic 模型（``app/tools/tool_models/browser_args/``）；宿主不再手写同一套
校验，而是按这里导出的 ``tool-args.schema.json`` 校验（键为工具名）。改了参数模型后重新运行本脚本，
``tests/test_browser_contract_guard.py`` 会在导出文件过期时失败。

``browser_handoff`` 不在其中：它发给宿主的是 ``browser.tabs`` / ``browser.snapshot`` 请求，按那两个
工具的 schema 校验（参数也用那两个工具的模型构造，字段齐全）。

用法（在 apps/backend 下）::

    uv run python scripts/export_browser_tool_schema.py
"""

import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 先装载 app.models：直接从 app.core.tool.schemas 进入会撞上存量的循环导入
# （schemas → tool_observation → app.models → … → tool_batch → schemas）。
import app.models  # noqa: E402, F401
from app.tools.tool_handler.browser import (  # noqa: E402
    BrowserActTool,
    BrowserEvaluateTool,
    BrowserLogsTool,
    BrowserNavigateTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
    BrowserTabsTool,
)

SCHEMA_PATH = (
    BACKEND_ROOT.parent / "desktop" / "electron" / "main" / "browser" / "contract"
) / "tool-args.schema.json"

HOST_VALIDATED_TOOLS = (
    BrowserNavigateTool,
    BrowserSnapshotTool,
    BrowserActTool,
    BrowserScreenshotTool,
    BrowserTabsTool,
    BrowserLogsTool,
    BrowserEvaluateTool,
)


def _wire_shape(schema: Any) -> Any:
    """把每个对象的全部属性标为必填：工具照 ``model_dump()`` 原样发给宿主，每个字段都在（未填的可选
    字段为 null），导出的 schema 描述的就是宿主实际收到的形状，宿主不必另补缺省值。"""

    if isinstance(schema, dict):
        shaped = {key: _wire_shape(value) for key, value in schema.items()}
        if isinstance(shaped.get("properties"), dict):
            shaped["required"] = sorted(shaped["properties"])
        return shaped
    if isinstance(schema, list):
        return [_wire_shape(item) for item in schema]
    return schema


def render_schema() -> str:
    """导出文件的完整文本；键排序、保留非 ASCII，生成结果稳定，守卫测试按文本比较。"""

    schemas = {
        tool.name: _wire_shape(tool.args_model.model_json_schema()) for tool in HOST_VALIDATED_TOOLS
    }
    return json.dumps(schemas, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    SCHEMA_PATH.write_text(render_schema(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
