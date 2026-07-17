# 工具参数校验演进设计（args_model 增量方案）

本文件记录工具参数校验从「JSON Schema 手写」向「pydantic 模型为事实来源」演进的
设计。属于第一阶段之后的**建议做**项，目标是在不破坏现有工具与模型协议的前提下，
消除「schema 与 handler 期望漂移」这一长期隐患。

参考：`apps/backend/app/tools/schema/validation.py`、`apps/backend/app/tools/schemas/tool_definition.py`。

优先级约定沿用 AGENTS.md 协作原则（必须做 / 建议做 / 以后做）。

---

## 一、背景与现状

当前工具参数校验链路：

- `ToolDefinition.parameters_schema` 是一份手写 JSON Schema，同时承担两个职责：
  1. **模型侧契约**：直接喂给 OpenAI / DeepSeek 作为工具提示（协议要求 JSON Schema）。
  2. **校验侧来源**：`validate_tool_arguments(arguments, schema)` 拿它校验模型入参。
- `validate_tool_arguments` 已在本轮收干净：移除 optional 回退死代码，强制使用
  `jsonschema`（Draft 2020-12）。`jsonschema==4.23.0` 已是 `requirements.txt` 硬依赖。

这套「单一事实来源 = JSON Schema」设计本身是干净的：**模型看到的就是被校验的**。
但它有一个长期隐患——schema 是手写的，与 handler 真实期望会漂。作者改了 handler
参数，忘了改 schema，模型会按旧 schema 传参，运行时才炸。

---

## 二、核心问题

**事实来源错位**：校验依据（JSON Schema）与执行依据（handler 签名）是两份独立手写
产物，没有强制一致性。LLM 工具调用的失败往往来自这种漂移，而非逻辑错误。

---

## 三、目标

- 让工具作者写一个 pydantic `BaseModel`，即可同时获得：schema 自动派生、入参自动
  校验、handler 签名类型。
- 保持「模型侧 wire format 仍是 JSON Schema」这一正确决策不变（OpenAI / DeepSeek
  协议要求）。
- **增量迁移**：老工具零改动，新工具受益，不一次性铺大坑。

---

## 四、方案设计

### 4.1 ToolDefinition 新增可选字段

```python
# apps/backend/app/tools/schemas/tool_definition.py
from typing import Optional, Any
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    permission: str
    required_params: Iterable[str]
    handler: Callable[..., Any]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    args_model: Optional[type[BaseModel]] = None   # 新增：pydantic 事实来源
    timeout_seconds: float = 10.0
    risk_level: str = "low"
    visible_by_default: bool = True
    resource_keys: Sequence[str] = field(default_factory=tuple)
```

### 4.2 parameters_schema 派生规则

- 若提供 `args_model` 且未显式给 `parameters_schema`，由
  `args_model.model_json_schema()` 派生（允许显式覆盖，用于协议微调）。
- 注册阶段（如 `ToolRegistry.register`）统一归一化：有 `args_model` 就补算
  `parameters_schema`，保证下游（模型提示、校验）永远拿到 schema。

### 4.3 validate_tool_arguments 双分支

```python
def validate_tool_arguments(arguments, schema, args_model=None) -> str:
    if args_model is not None:
        try:
            args_model.model_validate(arguments)   # strict 模式见 4.5
        except ValidationError as exc:
            return exc.errors()[0].get("msg", str(exc))
        return ""
    # 老工具：保持现有 jsonschema 校验
    if not schema:
        return ""
    if not isinstance(arguments, Mapping):
        return "expected object"
    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except ValidationError as exc:
        return exc.message
    return ""
```

`ToolRuntime.prepare_tool_call` 在调用时把 `tool.args_model` 一并传入即可，调用点
（`platform.py`）改动极小。

### 4.4 错误回灌语义不变

校验失败仍返回**人类可读字符串**，由 `platform.py` 包装成
`ToolObservation(status="error")` 回灌模型，使模型自我纠正。这是设计核心，不改动。

---

## 五、增量迁移路径

1. 先落地 `args_model` 可选字段 + 派生 + 双分支校验（本设计主体）。
2. 挑 1~2 个内置工具（如 `builtin/safe_read.py` 的 `read_file`）改写为 pydantic
   `args_model`，验证 `model_json_schema()` 产物在 DeepSeek 上能被正常消费。
3. 验证通过后，再逐步推广到其余内置工具；业务工具（MCP / 外部注册）保持
   `parameters_schema` 不动。
4. 不强制一次性迁移；`args_model` 与 `parameters_schema` 长期共存。

---

## 六、风险与取舍

- **strict 模式（必须）**：默认 `model_config = ConfigDict(strict=True)`，避免
  `"3"` 被静默转成 `3` 后语义变化。LLM 传参多为字符串，strict 能暴露类型不符而非
  静默 coercion。
- **pydantic JSON Schema 与协议兼容性**：`model_json_schema()` 产出 Draft 2020-12
  兼容 schema，常规工具参数无碍；但自由对象（`additionalProperties`）、复杂 union
  有边缘情况，需在第 5.2 步抽样验证。
- **不新增依赖**：pydantic 已由 FastAPI 带来；本方案甚至为长期彻底移除 `jsonschema`
  埋下可能（见第七节），净减少依赖。
- **迁移节奏**：禁止一次性全量改写，先增量验证再推广。

---

## 七、后续（以后做）

- **彻底去 jsonschema**：当所有内置工具都改用 `args_model`、且派生 schema 在协议侧
  验证充分后，可移除 `jsonschema` 依赖，校验统一走 pydantic。需确认老 `parameters_schema`
  工具能等价表达。
- **typed results**：是否让 `ToolObservation` 也结构化是独立议题，与参数校验解耦，
  不在本设计范围。

---

## 八、验收标准（落地时）

- 新工具用 `args_model` 注册后，`parameters_schema` 能自动派生且被 DeepSeek 接受。
- 参数错误时模型收到人类可读观测并能自我纠正（行为不变）。
- 老工具（仅 `parameters_schema`）行为完全不变。
- 单测覆盖：pydantic 分支成功 / 失败、jsonschema 分支成功 / 失败、派生 schema 正确性。
