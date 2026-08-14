# `convert_to_openai_tool` 技术说明

> 本文为纯技术文档，介绍 LangChain `convert_to_openai_tool(function, *, strict=False)`
> 这个方法的**输入格式**与**输出格式**，并给出真实可复跑的例子。
> 文中例子取自项目真实文件 `app/tools/tool_handler/list_directory.py`，但仅用于
> 说明「之前什么格式 → 转换后什么格式」，**不涉及任何项目特有的消费逻辑**。

---

## 一、方法是什么

- **所属**：`langchain_core.utils.function_calling`
- **签名**：`convert_to_openai_tool(function: object, *, strict: bool = False) -> dict`
- **作用**：把一个「可调用对象 / Pydantic 模型 / TypedDict」转换成
  **OpenAI function-calling 工具定义** 的 JSON（dict）。

本文聚焦最常见的输入：**Pydantic `BaseModel` 子类**。

- **输入**：一个 Pydantic 模型类（如 `ListDirectoryArgs`）
- **输出**：形如 `{"type": "function", "function": {...}}` 的 dict（可直接喂给
  `ChatOpenAI.bind_tools([...])`）

---

## 二、真实例子（取自 `list_directory.py`）

在 `app/tools/tool_handler/list_directory.py` 中，工具类 `ListDirectoryTool` 通过
`args_model = ListDirectoryArgs` 声明参数结构（`list_directory.py:53`）。其参数模型
定义于 `app/tools/tool_models/list_directory_args.py`：

```python
# app/tools/tool_models/list_directory_args.py
from pydantic import BaseModel, ConfigDict, Field


class ListDirectoryArgs(BaseModel):
    """list_directory 工具接受的校验参数。"""
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        description='Directory path relative to the project root. Use "." for the workspace root; '
                    'do not leave it empty (e.g. "apps/backend" for a subfolder, "." for root).'
    )
    offset: int = Field(default=0, ge=0, description="Skip first N entries for pagination.")
    limit: int = Field(
        default=200, ge=1, le=500,
        description="Maximum number of directory entries to return.",
    )
    include_hidden: bool = Field(
        default=False,
        description="When true, include dot entries (names starting with '.'); "
                    "skipped by default to keep directory listings concise.",
    )
    ignore_globs: list[str] = Field(
        default_factory=list,
        description="Glob patterns matched against each entry name; matching entries "
                    "are excluded (e.g. '*.py', '.git'). Empty by default.",
    )
```

该模型混合了：必填字符串 `path`、带约束与默认值的整型 `offset`/`limit`、布尔
`include_hidden`、列表 `ignore_globs`（用 `default_factory=list`），正好用来展示
strict 化的差异。

### 2.1 转换前：Pydantic 模型本身

模型类 `ListDirectoryArgs` 不是 dict，也不是实例，它只是「参数结构的定义」。

如果手动取它自带的 JSON schema（即 `ListDirectoryArgs.model_json_schema()`），得到
（以下为真实运行输出，仅保留字段语义、未改动内容）：

```json
{
  "additionalProperties": false,
  "description": "list_directory 工具接受的校验参数。...",
  "properties": {
    "path": {
      "description": "Directory path relative to the project root. Use \".\" for the workspace root; do not leave it empty (e.g. \"apps/backend\" for a subfolder, \".\" for root).",
      "title": "Path",
      "type": "string"
    },
    "offset": {
      "default": 0,
      "description": "Skip first N entries for pagination.",
      "minimum": 0,
      "title": "Offset",
      "type": "integer"
    },
    "limit": {
      "default": 200,
      "description": "Maximum number of directory entries to return.",
      "maximum": 500,
      "minimum": 1,
      "title": "Limit",
      "type": "integer"
    },
    "include_hidden": {
      "default": false,
      "description": "When true, include dot entries (names starting with '.'); skipped by default to keep directory listings concise.",
      "title": "Include Hidden",
      "type": "boolean"
    },
    "ignore_globs": {
      "description": "Glob patterns matched against each entry name; matching entries are excluded (e.g. '*.py', '.git'). Empty by default.",
      "items": { "type": "string" },
      "title": "Ignore Globs",
      "type": "array"
    }
  },
  "required": ["path"],
  "title": "ListDirectoryArgs",
  "type": "object"
}
```

注意此时 **`required` 只有 `["path"]`** —— 只有没有默认值的必填字段才会进 `required`；
`offset` / `limit` / `include_hidden` / `ignore_globs` 因为有默认值，不强制要求模型给出。

### 2.2 转换后：`convert_to_openai_tool(ListDirectoryArgs, strict=True)`

```python
from langchain_core.utils.function_calling import convert_to_openai_tool

result = convert_to_openai_tool(ListDirectoryArgs, strict=True)
```

真实输出（去掉字段顺序差异，与运行结果一致）：

```json
{
  "type": "function",
  "function": {
    "name": "ListDirectoryArgs",
    "description": "list_directory 工具接受的校验参数。...",
    "parameters": {
      "additionalProperties": false,
      "properties": {
        "path": {
          "description": "Directory path relative to the project root. Use \".\" for the workspace root; do not leave it empty (e.g. \"apps/backend\" for a subfolder, \".\" for root).",
          "type": "string"
        },
        "offset": {
          "default": 0,
          "description": "Skip first N entries for pagination.",
          "minimum": 0,
          "type": "integer"
        },
        "limit": {
          "default": 200,
          "description": "Maximum number of directory entries to return.",
          "maximum": 500,
          "minimum": 1,
          "type": "integer"
        },
        "include_hidden": {
          "default": false,
          "description": "When true, include dot entries (names starting with '.'); skipped by default to keep directory listings concise.",
          "type": "boolean"
        },
        "ignore_globs": {
          "description": "Glob patterns matched against each entry name; matching entries are excluded (e.g. '*.py', '.git'). Empty by default.",
          "items": { "type": "string" },
          "type": "array"
        }
      },
      "required": ["path", "offset", "limit", "include_hidden", "ignore_globs"],
      "type": "object"
    },
    "strict": true
  }
}
```

---

## 三、之前 vs 之后：格式对比

| 维度 | 转换前（Pydantic 模型 / `model_json_schema()`） | 转换后（`convert_to_openai_tool(..., strict=True)`） |
|------|-----------------------------------------------|---------------------------------------------------|
| 形态 | Python 类；或 `model_json_schema()` 出的裸 JSON schema（顶层 `{type:object, properties, required}`） | 嵌套 dict：`{type:"function", function:{name, description, parameters, strict}}` |
| 外层包装 | 无 `type`/`function` 包裹 | 多了一层 `{"type":"function","function":{...}}` |
| `name` 来源 | `title`（值为 `ListDirectoryArgs`，即类名） | 出现在 `function.name`，同样为类名 `ListDirectoryArgs` |
| `required` | 仅 `["path"]`（只含无默认值字段） | `["path","offset","limit","include_hidden","ignore_globs"]`（**所有字段**） |
| `additionalProperties` | `false`（本例模型自带 `extra="forbid"`） | `false`（strict 化递归保证） |
| 字段约束 | `minimum`/`maximum`/`items`/`default` 保留 | 同左，原样保留 |
| `strict` 标记 | 无 | 出现在 `function.strict = true` |

**一句话概括格式变化：**

> 从「一个 Pydantic 模型类（或它的裸 JSON schema）」变成
> 「`{type:"function", function:{name, description, parameters, strict:true}}`
> 这样的 OpenAI 工具定义字典」。

---

## 四、`strict=True` 到底改了什么

以 `ListDirectoryArgs` 实测为准，strict 化做了三件事：

1. **所有字段强制进 `required`**
   普通 schema 只有 `path` 在 `required` 里；strict 后
   `offset` / `limit` / `include_hidden` / `ignore_globs` 也被强制列入。模型**必须给出
   每个字段**，不能省略（即使它有默认值）。实测关键输出：
   ```
   普通 required   : ['path']
   strict required : ['path', 'offset', 'limit', 'include_hidden', 'ignore_globs']
   ```
2. **`additionalProperties: false`**
   本例模型本身已 `extra="forbid"`，故普通 schema 顶层已是 `false`；strict 化（以及
   LangChain 的递归逻辑）保证所有 object 层级都拒绝多余字段。
3. **保留原始 JSON schema 约束**
   `minimum`/`maximum`（ge/le 校验）、`items`（数组元素）、`default` 都原样保留，只是
   在此基础上叠加了 required 强制。

> 若调用 `convert_to_openai_tool(ListDirectoryArgs)`（不带 `strict=True`，默认
> `strict=False`），则 `required` 仍是 `["path"]`，且不会加 `function.strict` 标记——
> 与 Pydantic 自带 schema 几乎一致，仅多了 `function` 包装层。

---

## 五、可复跑脚本

下面脚本与本文输出一致（仅依赖 LangChain + 项目中的 `ListDirectoryArgs`，不依赖任何
业务消费逻辑）：

```python
import json
from langchain_core.utils.function_calling import convert_to_openai_tool
from app.tools.tool_models.list_directory_args import ListDirectoryArgs

plain = ListDirectoryArgs.model_json_schema()
func = convert_to_openai_tool(ListDirectoryArgs, strict=True)

print("转换前 (plain schema):")
print(json.dumps(plain, indent=2, ensure_ascii=False))
print("\n转换后 (strict function):")
print(json.dumps(func, indent=2, ensure_ascii=False))

params = func["function"]["parameters"]
print("\n关键差异:")
print("  普通 required   :", plain.get("required"))
print("  strict required :", params.get("required"))
print("  additionalProps :", params.get("additionalProperties"))
```

运行输出（节选）：

```
普通 required   : ['path']
strict required : ['path', 'offset', 'limit', 'include_hidden', 'ignore_globs']
additionalProps : False
```

---

## 六、总结

- **输入**：Pydantic 模型类（或函数 / TypedDict）。本文例子取真实文件
  `list_directory.py` 的 `ListDirectoryArgs`。
- **输出**：`{"type":"function","function":{name, description, parameters, strict}}`
  字典，可直接用于模型工具绑定。
- **`strict=True` 的本质**：用 schema 约束模型输出——把所有字段强制列入 `required`
  并关闭 `additionalProperties`，让模型必须给出**完整且不多余**的参数 JSON。
