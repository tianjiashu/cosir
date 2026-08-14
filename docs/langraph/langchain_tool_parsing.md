# LangChain 的 Tool 解析逻辑源码剖析：从工具声明到 `tool_calls` 与 `invalid_tool_calls`

> 本文基于 **langchain-core 1.4.9** 源码逐行剖析 LangChain 的 tool 解析全链路，
> 属于纯技术文档，不绑定任何具体业务项目。内容均可在
> `langchain_core/messages/tool.py`、`langchain_core/messages/content.py`、
> `langchain_core/messages/ai.py`、`langchain_core/utils/function_calling.py`、
> `langchain_core/utils/json.py` 中核对。文中所有结论均来自源码，非推测。

---

## 0. 总览：一条消息的三个阶段

LangChain 对「模型调用工具」的处理，可以拆成三个阶段：

```
阶段 A：工具声明（下行）
   Python 对象 (Pydantic 模型 / 函数 / dict)
        │  convert_to_openai_tool(..., strict=True)
        ▼
   OpenAI 工具 schema：{type:"function", function:{name, description, parameters, strict}}

阶段 B：模型原始返回（上行）
   ChatModel 拿到 provider 原始响应 raw_tool_calls
        │  default_tool_parser / 流式 chunk 合并
        ▼
   AIMessage.tool_calls:           list[ToolCall]           ← 解析成功
   AIMessage.invalid_tool_calls:   list[InvalidToolCall]    ← 解析失败

阶段 C：消费
   业务代码遍历 tool_calls 执行工具；
   invalid_tool_calls 携带原始参数，供「自愈 / 重试」逻辑使用。
```

本文聚焦 **阶段 A（声明转换）** 与 **阶段 B（解析分流：tool_calls vs invalid_tool_calls）**，
因为这两个阶段正是「为什么会有 invalid_tool_calls」的根源。

---

## 1. 数据结构：两种调用的形态

### 1.1 `ToolCall`（解析成功）

定义在 `langchain_core/messages/tool.py:206`：

```python
class ToolCall(TypedDict):
    name: str                       # 工具名
    args: dict[str, Any]            # 已解析为 dict 的参数
    id: str | None                  # 调用标识，用于与 ToolMessage 配对
    type: NotRequired[Literal["tool_call"]]
```

工厂函数 `tool_call(name=, args=, id=)`（`tool.py:242`）会强制注入 `type="tool_call"`。

### 1.2 `InvalidToolCall`（解析失败）

定义在 `langchain_core/messages/content.py:336`：

```python
class InvalidToolCall(TypedDict):
    type: Literal["invalid_tool_call"]   # 判别字段
    id: str | None
    name: str | None
    args: str | None              # ← 注意：是「未解析的原始字符串」，不是 dict
    error: str | None             # ← 解析错误信息，可能为 None
    index: NotRequired[int | str] # 流式分块序号
    extras: NotRequired[dict[str, Any]]  # Provider 自定义元数据
```

工厂函数 `invalid_tool_call(name=, args=, id=, error=)`（`tool.py:326`）注入
`type="invalid_tool_call"`。

**关键差异**：`ToolCall.args` 是 **dict**，`InvalidToolCall.args` 是 **str**（原始未解析
的 JSON 文本）。这是后续所有「自愈」逻辑必须兼容两种形态的根本原因。

---

## 2. 阶段 A：工具声明如何变成 schema（`convert_to_openai_tool`）

入口 `convert_to_openai_tool`（`function_calling.py:517`）：

```python
def convert_to_openai_tool(tool, *, strict=None) -> dict:
    ...
    oai_function = convert_to_openai_function(tool, strict=strict)
    return {"type": "function", "function": oai_function}
```

它只是给内部 `convert_to_openai_function` 的结果套了一层 `{"type":"function", ...}`。

### 2.1 模型类 → function schema

`convert_to_openai_function`（`function_calling.py:373`）对 Pydantic 模型类分支，本质是用
Pydantic 自带的 `model_json_schema()` 产出 `parameters`，再补齐 `name`/`description`。

### 2.2 `strict=True` 到底做了什么

这是最关键的代码（`function_calling.py:470-494`）：

```python
if strict is not None:
    ...
    oai_function["strict"] = strict
    if strict:
        # (1) 所有 properties 的 key 强制进入 required
        parameters = oai_function.get("parameters")
        if isinstance(parameters, dict):
            fields = parameters.get("properties")
            if isinstance(fields, dict) and fields:
                parameters = dict(parameters)
                parameters["required"] = list(fields.keys())   # ← 全部字段
                oai_function["parameters"] = parameters

        # (2) 所有层级 additionalProperties 递归设为 False
        oai_function["parameters"] = _recursive_set_additional_properties_false(
            oai_function["parameters"]
        )
```

`_recursive_set_additional_properties_false`（`function_calling.py:820`）递归遍历
`properties` / `items` / `anyOf` 各层，但**不是无条件**给每层设 `additionalProperties=False`——
它在 `function_calling.py:826-835` 有明确前置条件：仅当当前 schema 满足以下任一条件时才设置：

```python
def _recursive_set_additional_properties_false(schema):
    if isinstance(schema, dict):
        # 前置条件：有 required、或空 properties、或已显式声明 additionalProperties
        if "required" in schema or (
            "properties" in schema and not schema["properties"]
        ) or "additionalProperties" in schema:
            schema["additionalProperties"] = False
        for sub in schema.get("properties", {}).values():
            _recursive_set_additional_properties_false(sub)
        for sub in schema.get("anyOf", []):
            _recursive_set_additional_properties_false(sub)
        if "items" in schema:
            _recursive_set_additional_properties_false(schema["items"])
    return schema
```

**含义**：只有「声明了 `required`，或显式给了 `properties`（哪怕为空），或已自带
`additionalProperties`」的 object 节点才会被强制关闭额外字段；完全没有任何结构声明的
节点不会被改动。对 Pydantic 生成的 schema（顶层必有 `properties`）而言，效果等价于
顶层到各嵌套 object 全关 `additionalProperties:false`，但源码语义是「带条件的递归」而非
「无脑全盘设 False」。

**结论（严格模式三方面）**：

1. **`required` 被覆盖为全部字段**（含有默认值 / Optional 的字段）。模型必须显式给出每个字段。
2. **`additionalProperties: false` 递归到所有 object 层级**，模型不能输出多余字段。
3. 原始 JSON schema 约束（`minimum`/`maximum`/`enum`/`items`/`anyOf` 等）原样保留。

> 若 `strict=None`（默认），则既不写 `strict` 标记，也不强制 required / additionalProperties。
> 若传入 Pydantic 模型自带 `extra="forbid"`，其顶层 `additionalProperties` 已是 `false`，
> 但 `strict=True` 还会**递归**保证嵌套 object 也关闭。

### 2.3 其他输入形态（避免「只有 Pydantic 模型」的误解）

`convert_to_openai_tool` / `convert_to_openai_function` 实际支持多种输入。理解它们有助于
避免「只有 Pydantic 模型一种输入」的误解。需区分两个层次：

**层次一：`convert_to_openai_tool` 入口层的快捷返回（`function_calling.py:560-574`）**

这一层在真正调用 `convert_to_openai_function` 之前就 return，因此 strict 逻辑（470-494）
**完全不生效**：

- **OpenAI Responses API 风格工具原样直通**（560-565）：当 `tool` 是 dict 且
  `type` 命中 `_WellKnownOpenAITools`（`"file_search"`/`"function"`/`"computer_use_preview"`/
  `"apply_patch"` 等），**或** `type` 以 `"web_search_preview"` 开头时，直接 `return tool`
  原样返回，不进转换。
- **`custom_tool` 快捷分支**（566-574）：当 `tool` 是 `langchain_core.tools.Tool` 且
  `metadata.type == "custom_tool"` 时，提前 return
  `{"type":"custom","name":...,"description":...}`（若 metadata 含 `"format"` 则附加
  `"format"`）。它**完全绕过** `convert_to_openai_function`，故 strict 处理对其无效。

**层次二：`convert_to_openai_function` 的转换分支（`function_calling.py:405-451`）**

进入这一层的 dict / 模型会先被归一为 `oai_function`，之后**统一**流经 470-494 的 strict
处理（含 `required` 全覆盖与递归 `additionalProperties:false`）。支持的输入：

- **Anthropic 格式 dict**（405-413）：含 `name` + `input_schema`，归一为
  `{"name", "parameters": input_schema, ["description"]}`。
- **Amazon Bedrock Converse 格式 dict**（415-421）：含 `toolSpec`，取
  `toolSpec.name` / `toolSpec.inputSchema.json`。
- **已是 OpenAI function 格式的 dict**（423-428）：做**白名单键过滤重建**——
  `{k: v for k, v in function.items() if k in {"name","description","parameters","strict"}}`，
  `parameters` 之外的顶层键会被丢弃。**注意**：不是「原样直接返回」，且过滤后的
  `oai_function` 会照常进入 470-494 的 strict 处理——只要 `parameters.properties` 非空，
  480-487 行就会**无条件把 `required` 覆盖为全部字段 key**，与 schema 是否「已声明
  required」无关。
- **带 `title` 的 JSON schema dict**（430-436）：用 `title` 作 `name`，其余字段作
  `parameters`。
- **Pydantic 模型类**（437-440）：走 `_convert_pydantic_to_openai_function`（本文 2.1 主线）。
- **TypedDict**（441-445）：走 `_convert_typed_dict_to_openai_function`。
- **`langchain_core.tools.base.BaseTool`**（446-447）：走 `_format_tool_to_openai_function`。
- **callable（函数）**（448-）：取 docstring + 类型注解生成 schema。

**strict 冲突校验（470-477）**：当传入的 `oai_function` 字典自身**已含 `strict` 键**（唯一
来源是 423-428 的 dict 白名单透传分支），且其值与显式传入的 `strict` 参数**不相等**
（任一方向，`True≠False` 或 `False≠True`），则在 471-477 行抛 `ValueError`。该冲突判定只
针对「已含 strict 键的 dict 输入」，与「Pydantic 模型是否配置 `strict=True`」**无关**
——Pydantic 分支不会产出 `strict` 键。

> 小结：除入口层快捷返回（560-574）外，所有转换分支产出的 `oai_function` 都会被 470-494
> 的 strict 逻辑统一处理，不存在「已声明 schema 不被覆盖」的特殊豁免。

---

## 3. 阶段 B：模型返回如何分流（解析核心）

模型返回的原始结构是 provider 响应里的 `tool_calls`（一串 dict，每个含
`function.name` / `function.arguments` 等）。LangChain 在构造 `AIMessage` 时把这批原始
dict 解析成 `tool_calls` 与 `invalid_tool_calls`。

### 3.1 非流式：`default_tool_parser`

定义在 `langchain_core/messages/tool.py:349`：

```python
def default_tool_parser(raw_tool_calls):
    tool_calls = []
    invalid_tool_calls = []
    for raw_tool_call in raw_tool_calls:
        if "function" not in raw_tool_call:
            continue
        function_name = raw_tool_call["function"]["name"]
        try:
            # 关键：用 json.loads 严格解析 arguments
            function_args = json.loads(raw_tool_call["function"]["arguments"])
            parsed = tool_call(
                name=function_name or "",
                args=function_args or {},
                id=raw_tool_call.get("id"),
            )
            tool_calls.append(parsed)
        except json.JSONDecodeError:
            # 解析失败 → 进 invalid_tool_calls
            invalid_tool_calls.append(
                invalid_tool_call(
                    name=function_name,
                    args=raw_tool_call["function"]["arguments"],  # 原始字符串
                    id=raw_tool_call.get("id"),
                    error=None,                                   # ← 注意：error 为 None
                )
            )
    return tool_calls, invalid_tool_calls
```

**核心事实**：

- 当且仅当 `function.arguments` 不是合法 JSON（`json.JSONDecodeError`）时，整条调用进入
  `invalid_tool_calls`。
- 进 `invalid_tool_calls` 时：
  - `args` = **原始未解析的字符串**（不是 dict）
  - `error` = **`None`**（LangChain 在 `JSONDecodeError` 分支硬编码 `error=None`，不记录异常文本）
  - `name` / `id` 仍取自原始响应。

这意味着「无法解析为 JSON」是 `invalid_tool_calls` 最主要的来源，且此类条目 `error` 恒为
`None`、`args` 是脏字符串。任何消费 `invalid_tool_calls` 的代码都必须对此做 `None` 容错与
`str()` 兼容（否则 `error` 为 None 时拼接会崩、`args` 为 str 时按 dict 取值会崩）。

### 3.2 非流式：何时触发 `default_tool_parser`

在 `AIMessage` 的反序列化路径 `ai.py:308 _backwards_compat_tool_calls`（这是
`@model_validator(mode="before")` 方法）中，当原始 `additional_kwargs["tool_calls"]` 存在且
未被显式字段覆盖时，会调用 `default_tool_parser`（非 chunk 分支）：

```python
elif raw_tool_calls := values.get("additional_kwargs", {}).get("tool_calls"):
    if chunk_condition:
        values["tool_call_chunks"] = default_tool_chunk_parser(raw_tool_calls)
    else:
        parsed_tool_calls, parsed_invalid_tool_calls = default_tool_parser(raw_tool_calls)
        values["tool_calls"] = parsed_tool_calls
        values["invalid_tool_calls"] = parsed_invalid_tool_calls
```

若解析过程本身抛异常，则 `except Exception: logger.debug("Failed to parse tool calls", ...)`
静默丢弃（仅 debug 日志），`tool_calls` / `invalid_tool_calls` 保持空。

> 注意区分两个**不同的** model_validator：非流式解析在 `ai.py:308`
> `_backwards_compat_tool_calls`（before）；流式合并在 `ai.py:509 init_tool_calls`
> （after，见 3.3），二者不能混为一谈。

### 3.3 流式：chunk 合并路径（`init_tool_calls`，after validator）

流式场景下，LangChain 先累积 `tool_call_chunks`（每个 chunk 是 `name`/`args` 的片段），在
`AIMessage` 的 `init_tool_calls` 方法（`ai.py:509`，`@model_validator(mode="after")`）内
于 `ai.py:542` 处合并成最终列表：

```python
tool_calls = []
invalid_tool_calls = []

def add_chunk_to_invalid_tool_calls(chunk):
    invalid_tool_calls.append(
        create_invalid_tool_call(
            name=chunk["name"], args=chunk["args"], id=chunk["id"], error=None
        )
    )

for chunk in self.tool_call_chunks:
    try:
        # 注意：流式用的是 parse_partial_json，不是严格的 json.loads
        args_ = parse_partial_json(chunk["args"]) if chunk["args"] else {}
        if isinstance(args_, dict):
            tool_calls.append(create_tool_call(
                name=chunk["name"] or "", args=args_, id=chunk["id"]
            ))
        else:
            add_chunk_to_invalid_tool_calls(chunk)
    except Exception:
        add_chunk_to_invalid_tool_calls(chunk)
```

**流式与非流式的本质区别**：

| | 解析函数 | 失败条件 | error 值 |
|---|---|---|---|
| 非流式 `default_tool_parser` | `json.loads`（严格） | 非合法 JSON → `invalid` | `None` |
| 流式 chunk 合并 | `parse_partial_json`（容错，可补闭合括号） | 解析结果非 dict 或抛异常 → `invalid` | `None` |

`parse_partial_json`（`json.py:58`，改编自 open-interpreter，MIT）会尝试补全缺失的闭合括号，
因此流式场景下的「半截 JSON」往往能被救回成合法 dict；只有当解析结果根本不是 dict 或彻底
失败时，才落入 `invalid_tool_calls`。这解释了为什么「流式中间分片误报的 invalid_tool_calls，
合并后常清零」——流式合并比单次严格解析更宽容。

---

## 4. 两个真实例子

### 4.1 合法调用（进入 `tool_calls`）

模型返回原始：

```json
{
  "function": {
    "name": "search_files",
    "arguments": "{\"pattern\": \"foo\", \"limit\": 10}"
  },
  "id": "call_abc"
}
```

`json.loads` 成功 → `tool_calls` 中得：

```python
{"name": "search_files", "args": {"pattern": "foo", "limit": 10}, "id": "call_abc", "type": "tool_call"}
```

### 4.2 非法 JSON（进入 `invalid_tool_calls`）

模型返回原始：

```json
{
  "function": {
    "name": "search_files",
    "arguments": "{\"pattern\": \"foo\""    // ← 缺少闭合，非法 JSON
  },
  "id": "call_xyz"
}
```

`json.loads` 抛 `JSONDecodeError` → `invalid_tool_calls` 中得：

```python
{
  "name": "search_files",
  "args": "{\"pattern\": \"foo\"",   # 原始未解析字符串
  "id": "call_xyz",
  "error": None,                    # 硬编码 None
  "type": "invalid_tool_call",
}
```

消费方若想把它喂回模型重试，必须：
- 把 `args`（str）当字符串预览，**不能**当 dict 取值；
- 不要依赖 `error`（可能为 None），需自行给出「参数无法解析」的说明。

---

## 5. 小结：为什么会有 `invalid_tool_calls`，以及它长什么样

1. **来源**：模型产出的 `function.arguments` 不是合法 JSON（非流式严格 `json.loads` 失败），
   或流式合并后结果不是 dict（流式 `parse_partial_json` 失败）。
2. **形态**：`InvalidToolCall` —— `args` 是**原始未解析字符串**、`error` 通常（JSON 解析失败
   分支）为 **`None`**、`name`/`id` 取自原始响应、`type="invalid_tool_call"`。
3. **与 `tool_calls` 的对称性**：`ToolCall.args` 是 dict，`InvalidToolCall.args` 是 str；
   两者共享 `name`/`id`/`type` 判别字段，业务层需分别处理。
4. **严格声明的角色**：`bind_tools` 阶段用 `convert_to_openai_tool(strict=True)` 把
   schema 收紧（所有字段 required + 递归 `additionalProperties:false`），从**源头**降低模型
   产出残缺 / 多余 / 非法 JSON 的概率——但仍无法 100% 杜绝，故 `invalid_tool_calls` 兜底机制
   始终必要。
5. **流式更宽容**：流式用 `parse_partial_json` 容错补全，中间分片的误报会在合并阶段被消化；
   最终 `AIMessage.invalid_tool_calls` 才是业务层应关注的真实失败集合。

---

## 附：关键源码位置速查

| 主题 | 文件:行 |
|------|---------|
| `ToolCall` 定义 | `langchain_core/messages/tool.py:206` |
| `tool_call()` 工厂 | `langchain_core/messages/tool.py:242` |
| `InvalidToolCall` 定义（含 `extras`） | `langchain_core/messages/content.py:336`（extras 字段 368） |
| `invalid_tool_call()` 工厂 | `langchain_core/messages/tool.py:326` |
| `default_tool_parser`（JSON 失败→invalid） | `langchain_core/messages/tool.py:349` |
| `AIMessage` 非流式触发 parser | `langchain_core/messages/ai.py:308`（`_backwards_compat_tool_calls`，before validator） |
| 流式 chunk 合并（parse_partial_json） | `langchain_core/messages/ai.py:542`（在 `init_tool_calls`，after validator, 509-601） |
| `convert_to_openai_tool` | `langchain_core/utils/function_calling.py:517` |
| `convert_to_openai_function`（strict 处理实际实现） | `langchain_core/utils/function_calling.py:470-494`（函数入口 373） |
| 递归关 additionalProperties | `langchain_core/utils/function_calling.py:820`（前置条件 826-835） |
| `parse_partial_json`（流式容错解析） | `langchain_core/utils/json.py:58` |

> 版本基线：langchain-core 1.4.9 / langchain 1.3.1。所有 `文件:行` 标注均基于本地锁定版本
> 源码 `apps/backend/.venv/Lib/site-packages/langchain_core/`（可由 `uv.lock` 复现），可逐行
> 复验。如升级大版本，行号与实现细节可能漂移，但上述解析分流逻辑自 0.2.x 以来总体稳定。
