# LangChain OpenAI `bind_tools` 深度解析

> 本文基于本地已安装源码逐行核实，所有行号均可复验：
> - `langchain_openai/chat_models/base.py`（版本随 `langchain-openai` 锁定）
> - `langchain_core/utils/function_calling.py`
> - `langchain_core/language_models/chat_models.py`
> - `langchain_core/runnables/base.py`
>
> 本文与具体业务项目解耦，可作为通用技术参考。
>
> 注意：本项目现已改用 `ChatLiteLLM`/`litellm` 作为 LLM 收口，不再使用 `langchain-openai`；本文仅作 `bind_tools` 通用机制参考，文中行号与依赖版本为历史状态。

## 1. 一句话定位

`ChatOpenAI.bind_tools(tools, ...)` 不做任何网络请求，它只做**一件事**：

> 把任意形态（Pydantic 类 / TypedDict / dict / Python 函数 / LangChain `BaseTool`）的工具定义，**规范化**为 OpenAI 的 `tools` 数组，连同 `tool_choice`、`parallel_tool_calls`、`strict` 等参数，一起**绑定**到一个新的 `Runnable` 上。真正把这些参数塞进 HTTP 请求体，是后续 `invoke`/`stream` 时由 `RunnableBinding` 透传完成的。

它本质是"**格式归一 + 配置挂载**"，不是"执行工具"。工具的实际执行由调用方（如 LangGraph 的 `ToolNode`）在拿到 `AIMessage.tool_calls` 后自行驱动。

## 2. 调用链路总览

```
ChatOpenAI.bind_tools(tools, tool_choice=..., strict=..., ...)
   │
   ├─ (1) 校验 tool_choice 与 parallel_tool_calls / tools 的互斥关系
   │
   ├─ (2) 对每个 tool 调用 convert_to_openai_tool(tool, strict=strict)
   │         → 统一输出 OpenAI tool 包裹: {"type":"function","function":{name,description,parameters}}
   │
   ├─ (3) 组装 bind_kwargs: {tools, tool_choice, parallel_tool_calls, strict, ls_model_name, ...}
   │
   └─ (4) return super().bind(**bind_kwargs)
              │
              ▼
        BaseChatModel.bind(**kwargs) → RunnableBinding(self, kwargs=bind_kwargs)
              │
              ▼  （延迟到 invoke/stream 时）
        RunnableBinding.invoke(input, config, **kwargs)
              → self.bound.invoke(input, config, **{**self.kwargs, **kwargs})
              → 底层 ChatOpenAI.invoke 把 tools/tool_choice 合并进请求体
```

关键结论：**`bind_tools` 与 `bind` 的边界是"配置准备"与"配置挂载"**。`bind_tools` 准备出规范化后的 `tools` 列表和 `tool_choice` 等参数，交由通用的 `bind` 机制挂到 `RunnableBinding.kwargs` 上；直到真正调用模型时才注入请求。

## 3. `bind_tools` 核心实现

源码位于 `langchain_openai/chat_models/base.py`，定义为 `BaseChatOpenAI.bind_tools`（实例方法，覆写父类）。逐段拆解：

### 3.1 工具列表规范化（核心）

```python
# langchain_openai/chat_models/capability.py:2171-2173
formatted_tools = [
    convert_to_openai_tool(tool, strict=strict) for tool in tools
]
```

**这是 `bind_tools` 最重要的一行**：无论输入是 Pydantic 类、TypedDict、dict 还是 `BaseTool`，都经 `convert_to_openai_tool` 归一为 OpenAI tool 包裹结构。

### 3.2 `tool_choice` 冲突校验

`bind_tools` 会对 `tool_choice` 与 `parallel_tool_calls` / `tools` 做一组互斥检查（base.py:2213-2267），要点：

- `tool_choice` 取值域：`"auto"` / `"none"` / `"any"` / `"required"` / `{"type":"function","function":{"name":...}}`（specific）/ 纯工具名字符串（specified）/ `None`。
- 若 `tool_choice == "none"`，则**不允许**同时传入 `tools`（否则无意义，抛 `ValueError`）。
- 若 `tool_choice == "any"` 或 `"required"`，则**必须**提供 `tools`（否则抛 `ValueError`）。
- 若 `tool_choice` 是具体函数名（specific），则**必须**该名字存在于 `formatted_tools` 中（否则抛 `ValueError`）。
- `parallel_tool_calls=False` 与 `tool_choice` 指向多工具时无冲突，但与单次调用意图需自洽；校验逻辑以 `tool_names` 提取后比对。

> 这些校验是**调用前静态校验**，避免把非法组合打给 API 才报错。

### 3.3 `bind_kwargs` 组装

```python
# langchain_openai/chat_models/capability.py:2269-2295（节选）
bind_kwargs = self._filter_disabled_params(
    **{
        "tools": formatted_tools,
        "tool_choice": tool_choice,
        "parallel_tool_calls": parallel_tool_calls,
        "strict": strict,
        "ls_model_name": self.model_name,
        **kwargs,
    }
)
```

- `strict` 透传给 `convert_to_openai_tool` 的同时也挂在 kwargs 上（供下游/观测使用）。
- `**kwargs` 允许调用方把额外参数（如 `response_format` 之外与工具调用并存的参数）一并绑定。
- `_filter_disabled_params` 负责剔除当前模型/版本不可用的参数，避免误传。

### 3.4 委托给 `bind`

```python
# langchain_openai/chat_models/capability.py:2297-2299
return super().bind(**bind_kwargs)
```

即 `BaseChatModel.bind(**bind_kwargs)`。

## 4. `convert_to_openai_tool` 的 dict 分支（关键细节）

`bind_tools` 对列表每个元素都调 `convert_to_openai_tool`。该函数在 `langchain_core/utils/function_calling.py`，对 dict 输入的分支（:560-576）尤其值得关注：

```python
# langchain_core/utils/function_calling.py:560-576
if isinstance(tool, dict):
    if tool.get("type") in _WellKnownOpenAITools:
        return tool                      # 已知 OpenAI 内置工具 → 原样直通
    # As of 03.12.25 can be "web_search_preview" or "web_search_preview_2025_03_11"
    if (tool.get("type") or "").startswith("web_search_preview"):
        return tool                      # web_search_preview 系列 → 原样直通
if isinstance(tool, Tool) and (tool.metadata or {}).get("type") == "custom_tool":
    oai_tool = {"type": "custom", "name": tool.name, "description": tool.description}
    if tool.metadata is not None and "format" in tool.metadata:
        oai_tool["format"] = tool.metadata["format"]
    return oai_tool                      # custom_tool → 构造 custom 包裹
oai_function = convert_to_openai_function(tool, strict=strict)
return {"type": "function", "function": oai_function}
```

### 4.1 "裸 function" dict 不会双重包裹

如果传入的 `tool` 是裸 function 形状 `{"name", "description", "parameters"}`（即已含 `"name"` 键的 dict），它会落到最后的 `convert_to_openai_function` 分支。而 `convert_to_openai_function` 对 dict 的处理（:422-428）：

```python
# langchain_core/utils/function_calling.py:422-428
# already in OpenAI function format
elif isinstance(function, dict) and "name" in function:
    oai_function = {
        k: v
        for k, v in function.items()
        if k in {"name", "description", "parameters", "strict"}
    }
```

即**裸 function dict 被识别为"已经是 OpenAI function 格式"，只过滤保留白名单字段**，然后被包成 `{"type":"function","function":{...}}`。

**推论（项目相关但本文中立陈述）**：
- 输入裸 function `{"name",...}` → 输出 `{"type":"function","function":{"name",...}}`（`convert_to_openai_function` 过滤 + 外层包 type）。
- 输入已是完整包裹 `{"type":"function","function":{...}}` → 命中 `tool.get("type") in _WellKnownOpenAITools` 或经 dict 分支后，由 `convert_to_openai_tool` 的 dict 直通逻辑**原样返回**（不会再加一层 type）。
- 因此**不存在"双重包裹"风险**：`bind_tools` 对同一元素只会规范化一次。

### 4.2 `strict` 冲突检查

`convert_to_openai_function` 在 :470-476 检查：若输入 dict 已带 `strict` 键，且与显式 `strict` 参数冲突 → 抛 `ValueError`。这解释了为什么 `bind_tools(..., strict=True)` 时，传入的工具不应自己携带不一致的 `strict` 键。

## 5. `bind` 与 `RunnableBinding`：配置如何抵达请求体

`super().bind(**bind_kwargs)` 最终构造 `RunnableBinding`（`langchain_core/runnables/base.py`）。

调用绑定后的 Runnable 时，`RunnableBinding.invoke` 的关键逻辑（:5996-6006）：

```python
# langchain_core/runnables/capability.py:5996-6006
def invoke(self, input, config=None, **kwargs):
    return self.bound.invoke(
        input,
        self._merge_configs(config),
        **{**self.kwargs, **kwargs},   # 绑定参数与调用时参数合并
    )
```

即 `bind_tools` 准备的 `tools` / `tool_choice` / `parallel_tool_calls` / `strict` 都在 `self.kwargs` 中，调用 `invoke` 时通过 `**{**self.kwargs, **kwargs}` 透传给底层 `ChatOpenAI.invoke`。底层再把它们合并进 OpenAI 的 HTTP 请求体（`tools`、`tool_choice` 字段）。

> 这印证了第 2 节的结论：**`bind_tools` 只负责准备并挂载配置，真正的请求注入发生在 `invoke` 阶段**。

## 6. `with_structured_output` 复用 `bind_tools`

`ChatOpenAI.with_structured_output(schema, method="function_calling", ...)` 在内部直接复用 `bind_tools`（`langchain_openai/chat_models/base.py:2424`）：

```python
# langchain_openai/chat_models/capability.py:2410-2424
tool_name = convert_to_openai_tool(schema)["function"]["name"]
bind_kwargs = self._filter_disabled_params(
    **{
        "tool_choice": tool_name,
        "parallel_tool_calls": False,
        "strict": strict,
        "ls_structured_output_format": {"kwargs": {"method": method, "strict": strict}, "schema": schema},
        **kwargs,
    }
)
llm = self.bind_tools([schema], **bind_kwargs)
```

要点：
- `with_structured_output` 把 `schema` 当作**唯一工具**传给 `bind_tools`，并把 `tool_choice` 固定为该工具名，强制模型调用它。
- `parallel_tool_calls=False` 保证只产出一次工具调用，便于后续解析。
- 返回的是 `bind_tools` 结果再串一个输出解析器（`PydanticToolsParser` / `JsonOutputKeyToolsParser`），将 `tool_calls[0]` 反序列化为目标结构。

**结论**：`with_structured_output(method="function_calling")` 是 `bind_tools` 的一个特化封装，二者共享同一套工具规范化与 `tool_choice` 机制。

## 7. 常见疑问速查

| 问题 | 结论 |
|------|------|
| `bind_tools` 会发网络请求吗？ | 不会，只准备并挂载配置，请求在 `invoke` 时发出。 |
| 裸 function dict 会被双重包裹吗？ | 不会。`convert_to_openai_tool` 对已是 OpenAI function 格式的 dict 直通/过滤一层，外层只包一次 `type`。 |
| 传入完整包裹 `{"type":"function",...}` 会怎样？ | 命中 dict 直通分支，原样返回，不会再加一层。 |
| `tool_choice="none"` 还能传 `tools` 吗？ | 不能，`bind_tools` 会抛 `ValueError`（无意义组合）。 |
| `strict=True` 时工具自带 `strict=False` 会怎样？ | `convert_to_openai_function` 检测到冲突抛 `ValueError`。 |
| `with_structured_output` 与 `bind_tools` 关系？ | 前者 `function_calling` 模式内部调用后者，是特化封装。 |
| 非 OpenAI 模型（如 DeepSeek 走 OpenAI 协议）能用吗？ | 能。本文分析的 `bind_tools`/`convert_to_openai_tool` 是 OpenAI 协议通用层，任何兼容 OpenAI `/chat/completions` 的供应商都可接收其产物。 |

## 8. 源码行号索引（复验用）

| 内容 | 位置 |
|------|------|
| `bind_tools` 规范化工具体列表 | `langchain_openai/chat_models/base.py:2171-2173` |
| `tool_choice` / `parallel_tool_calls` 互斥校验 | `langchain_openai/chat_models/base.py:2213-2267` |
| `bind_kwargs` 组装 | `langchain_openai/chat_models/base.py:2269-2295` |
| 委托 `super().bind` | `langchain_openai/chat_models/base.py:2297-2299` |
| `convert_to_openai_tool` dict 分支（直通/包 type） | `langchain_core/utils/function_calling.py:560-576` |
| `convert_to_openai_function` dict 已是 function 格式分支 | `langchain_core/utils/function_calling.py:422-428` |
| `strict` 冲突检查 | `langchain_core/utils/function_calling.py:470-476` |
| `RunnableBinding.invoke` 透传 kwargs | `langchain_core/runnables/base.py:5996-6006` |
| `with_structured_output` 复用 `bind_tools` | `langchain_openai/chat_models/base.py:2410-2424` |
