# tools/ 目录开发约定

本文件是 `app/tools/` 包下的**开发约定**，描述目录职责、新增工具的步骤、工具类与
观察构造的强制约定，以及 `guard/` 守卫层（设计中）的使用约定。新代码必须遵守本
约定；存量代码与本约定冲突时，以「真实代码为准、冲突处修订本文件」为基线。

约定优先级（来自 `rules/Agent代码开发规范.md`）：正确性 > 单一职责 > 不重复造轮子
≈ 适度扩展性 > 可读性 > 简洁性 > 性能。

---

## 一、目录职责概览

`tools/` 是工具系统统一收口，**不基于 LangGraph**；内置工具经 `core` 调度执行。
各子包职责边界：

| 子包 | 职责 | 不负责 |
|------|------|--------|
| `schemas/` | 共享值对象（`ToolObservation` / `ToolDefinition` / `ToolCall` / `ToolExecutionContext` / `ToolDisplayHints`），纯数据零依赖 | 不承载服务、适配、执行逻辑 |
| `tool_execute/` | 工具执行与调度（`ToolScheduler` / `ToolExecutor` / 子进程隔离 / 输出预算 / `tool_success` / `tool_error` 工厂） | 不实现具体工具业务 |
| `tool_handler/` | 工具实现（一文件一工具），含 `file_io` / `patch` / `search` / `security` / `terminal` 子包 | 不承载校验之外的调度 |
| `tool_models/` | 各工具 Pydantic 参数模型（一文件一模型） | 不含执行逻辑 |
| `validation/` | 参数校验唯一收口（`arguments.py`） | 不校验路径边界（归 `security`） |
| `guard/` | 工具执行守卫层（装饰器形式，设计中），拦截输入 / 修改输出 | 不实现工具业务、不持有执行状态 |
| 根模块 | `tool_registry.py`（注册表）/ `tool_system.py`（进程级装配） | — |

依赖方向（单向 DAG）：`tool_execute` / `tool_handler` / `tool_models` / `validation` /
`guard` 均依赖 `schemas`；`guard` 与 `tool_handler` 互不反向依赖（守卫只读
`ToolObservation`，不 import 业务 handler）。

---

## 二、新增工具的标准步骤

1. **参数模型**：在 `tool_models/` 新建 `<tool>_args.py`，定义 Pydantic 模型（字段
   即工具入参，必填项用必填声明）。
2. **工具实现**：在 `tool_handler/` 新建 `<tool>.py`，写入工具类（见第三节）与
   `build_<tool>_definition()` 工厂。
3. **注册接线**：在 `tool_system.py`（或对应装配入口）用
   `build_<tool>_definition()` 把定义加入注册表 / 集合。
4. **观察构造**：所有成功 / 失败返回均经 `tool_success` / `tool_error`（见第四节），
   不得裸构造 `ToolObservation`。
5. **守卫（按需）**：若该工具需要输入拦截或输出审查，按第五节在 `execute` 上叠加
   对应守卫装饰器。
6. **测试**：在 `apps/backend/tests/` 下加 `test_<tool>.py`，覆盖成功、失败、边界
   与守卫行为（风格 / 语法）。
7. **文档同步**：若工具语义影响 `AGENTS.md` 目录职责表，回写对应条目。

---

## 三、工具类约定（强制）

### 3.1 类结构：三件套

每个工具一个类，遵循「类属性元信息 + `execute` + `to_definition`」结构：

```python
class ReadFileTool:
    name = "read_file"
    description = "..."            # 面向模型的英文描述
    permission = "file_read"       # 权限标识，与审批系统对齐
    args_model = ReadFileArgs      # 参数校验契约（强制必填）
    timeout_seconds = 15.0         # 超时秒数
    risk_level = "low"             # low / medium / high

    def __init__(self) -> None:
        """初始化 ... 工具实例。参数: 无。返回: 无。异常: 无。"""

    def execute(self, *, execution_context: ToolExecutionContext | None = None, **kwargs) -> ToolObservation:
        """... 四段式 docstring（参数/返回/异常/副作用）。"""

    def to_definition(self) -> ToolDefinition:
        """..."""


def build_read_file_definition() -> ToolDefinition:
    """构造 read_file 工具定义。"""
    return ReadFileTool().to_definition()
```

### 3.2 强制约定

- **`execute` 签名**：必须持有 `execution_context: ToolExecutionContext | None = None`
  作为末位参数（关键字参数），不管工具是否用到。它是预留扩展点——工具系统在将来可以
  在 `ToolExecutionContext` 上增加新字段（如 trace_id / 会话状态 / 客户端能力声明），
  不要求各工具逐项修改签名。`execution_context` 由执行链在执行期**强制注入**，
  handler 契约必须接受此 kwarg。工具内不再校验 `execution_context is None`（因为
  注入是执行链的强制约定）。破坏性操作以其 `workspace_root` 作为路径 containment
  唯一事实源。返回类型恒为 `ToolObservation`。
- **绝不主动抛异常**：所有失败路径归一化为 `status="error"` 的 `ToolObservation`，经
  `tool_error()` 构造。`execute` 对调用方永远「要么返回观察，要么返回观察」。
- **路径安全委托 `security.ProjectPathResolver`**：不在 handler 内联路径越界规则；
  workspace 外写 / 删由 `resolve` 的 `path_escape` 天然拒绝。
- **模块级 `build_*_definition` 工厂**：注册入口只调工厂，不直接 `new` 实例。
- **`to_definition` 透传字段**：`name` / `description` / `permission` / `handler` /
  `args_model` / `timeout_seconds` / `risk_level` / `resource_keys` / `display` 均来自
  类属性，不重写在构造里。

### 3.3 `ToolDisplayHints` 约定（展示元数据）

`ToolDisplayHints` 是 `ToolDefinition.display` 字段的类型，承载「工具在前端如何展示」
的语义元数据。新增工具时必须附带，前端保留一个通用渲染引擎，不按工具名写特化分支。

```python
display=ToolDisplayHints(
    verb="写入",               # 动作名（中文），前端主标题动词
    icon="file-plus",          # lucide 图标名
    summary_template="{path_basename}",  # 摘要模板，str.format 占位符引用参数字典
    detail_keys=("path", "content"),     # 展开态优先展示的参数 key 顺序
    click_action="open_file:{path}",     # 可选点击动作，格式 "<action>:<target_template>"
)
```

关键规则：
- **所有工具必须有 `display`**，前端依赖它做统一渲染。当前存量工具（write_file / read_file
  / patch_tool / list_directory / search_files / delete / execute_terminal）均已携带。
- **`summary_template`** 优先用 `path_basename`（自动派生自 `path` 参数）做精简摘要；
  分页类工具自动补 `start` / `end` 行号，模板可引用如 `"{path}:L{start}-L{end}"`。
- **`detail_keys`** 控制展开态展示哪些参数及其顺序；未列出的参数按字典序兜底排在后面。
- **`click_action`** 格式 `<action>:<target_template>`，模板占位符来自参数字典；
  渲染时解析为 `{"action": ..., "target": ...}` 由前端按 `action` 分发（如
  `"open_file:{path}"` → 点击文件路径跳转）。
- **渲染零依赖**：`ToolDisplayHints.render(arguments)` 不 import `app.*` 之外的业务模块；
  模板缺字段安全降级为 `verb + 主参数`，不抛异常。

### 3.4 文件与命名

- 一文件一工具；文件名 = 主类名 snake_case（`ReadFileTool` ↔ `read_file.py`）。
- 绝对导入：`from app.tools.subpkg.module import Thing`；禁止相对导入跨包、禁止 `import *`。
- 禁止模糊命名（`Utils` / `Helper` / `Common` / `Misc` / `Manager`）；共享纯函数可用
  功能命名（如 `tool_error.py` / `atomic_write.py`）。

---

## 四、观察构造约定（强制）

成功 / 失败观察的构造收口在 `tool_execute/tool_success.py` 与 `tool_execute/tool_error.py`，
**禁止**在 handler 内裸构造 `ToolObservation`。

### 4.1 成功：`tool_success`

```python
return tool_success(
    tool=self.to_definition(),
    content="Wrote 42 bytes to path/to/file",   # 英文、对模型友好
    tool_call_id=...,                            # 可选
    data={"path": "...", "bytes": 42},           # 机读结构化载荷
)
```

- `content`：`str`，面向模型的人读文本，**必须英文**、简洁、便于模型直接消费。
- `data`：`dict`，机读结构化载荷（路径、类型、退出码等），供上层程序不解析文本即可
  消费；与 `content` 互不替代，可同时填充。

### 4.2 失败：`tool_error`

```python
return tool_error(
    tool_name=self.name,
    error="could not write the file: permission denied",  # 英文，点明动作+原因
    reason="...",                                          # 富文本：根因+修正+重试提示
    retryable=False,                                       # 瞬态True / 确定性False
    permission=self.permission,
)
```

三字段语义分工（已在全仓库落地）：

| 字段 | 含义 | 约定 |
|------|------|------|
| `error` | 「发生了什么错误」 | 英文动作+直接人读原因，非异常噪声/堆栈；同时写入 `content` |
| `reason` | 「为什么失败 + 如何修正 + 是否重试」 | 面向模型富文本，**非**稳定机器短码（旧分类码 `write_failed` 已废弃） |
| `retryable` | 「原样重试是否可能成功」 | 程序化布尔；`reason` 重试提示须与之保持一致 |

共享文本助手（`tool_error.py` 内）复用点：
- `os_error_message(exc, action)`：文件类工具 OSError → 英文短句。
- `blocked_device_reason(action)`：命中 OS 设备 / 伪文件分支。
- `handler_exception_reason(header)`：`ToolExecutor` 的 handler 异常尾部。

---

## 五、守卫层约定（`guard/` — 设计中）

> 本节描述**已达成方向的约定**，守卫层尚未落地实现。落地前以本节为规范基线。

### 5.1 定位

`guard/` 是工具执行后的**外部审查层**，与工具业务解耦。守卫对工具 `execute` 产出的
`ToolObservation` 做判读与处理，可在「输入拦截」与「输出修改」两个时机介入。

### 5.2 形态：装饰器优先

- 守卫以**装饰器**形式实现，叠加在工具类的 `execute` 方法上。
- 每个守卫一个文件、一个装饰器（如 `syntax_guard.py` → `@syntax_guard`、
  `lint_guard.py` → `@lint_guard`、`line_numbered_guard.py` → `@line_numbered_guard`），
  符合单一职责，独立可测。
- 工具「需要哪些守卫就加哪些装饰器」，装饰器顺序即守卫执行顺序：

```python
class WriteFileTool:
    @line_numbered_guard     # 输入拦截：行号污染 → 拒写
    @syntax_guard            # 输入拦截：.py 语法错误 → 拒写
    @lint_guard              # 输出审查：ruff/eslint 风格 → 仅告警追加
    def execute(self, ...) -> ToolObservation:
        ...
```

### 5.3 装饰器生效范围（已验证）

- `ToolDefinition.handler` 存的是可调用对象，不关心是否装饰过；注册不受影响。
- `execution_mode="thread"`（默认）下 `ToolExecutor` 直接 `handler(**kwargs)` 调用，
  装饰器链路完美工作。
- `execution_mode="process"`（如 `execute_terminal`）跨进程 pickle，装饰器暂不保证
  生效；该模式工具当前无语法 / 行号 / 风格守卫需求（YAGNI）。

### 5.4 守卫的两种介入时机

| 时机 | 作用 | 典型守卫 |
|------|------|----------|
| 输入拦截（pre） | 在 `execute` 业务前检查入参 / 待写内容，不满足直接返回 error 观察，**不进入业务** | 行号污染、`.py` 语法错误 |
| 输出修改（post） | 在 `execute` 返回后审查 `ToolObservation`，追加 `data` 告警或替换为新观察 | ruff / eslint 风格 |

### 5.5 守卫与 `ToolObservation` 的契约

- 守卫只读 / 改 `ToolObservation`，不接触任何工具的私有状态。
- 输出修改型守卫只向 `data` 追加结构化诊断（如 `data["lint"]`），并向 `content` 末尾
  拼接人读告警，不改变 `status`（除非语法 / 安全拦截类需将 success 替换为 error）。
- 拦截型守卫返回新的 `status="error"` 观察，复用 `tool_error` 语义（`error` / `reason`
  / `retryable` 三字段分工）。

### 5.6 可扩展性

- 新增语言 / 新规则 = 新增一个守卫文件 + 在对应工具 `execute` 上叠加装饰器，**零改动**
  既有守卫与 handler（即插即用）。
- `guard/` 只依赖 `schemas`（`ToolObservation` / `ToolDefinition`）、`config`（`Settings`
  总开关）、`trace_infra`；不反向 import `tool_handler` / `tool_execute`，避免循环依赖。

---

## 六、文档与 Docstring 约定（强制）

### 6.1 模块级 docstring

每个模块以一句话描述开头，空行后接段落说明，再用 `设计边界：` 子弹列表显式声明
「不负责」的相邻职责：

```python
"""write_file 工具实现。

本模块只承载 write_file 这一个工具。写盘经由 file_io.atomic_write 做原子写……

设计边界：
- 路径安全委托 security.ProjectPathResolver，不内联路径规则。
- 只写文件，不读（除 .py 语法校验外）。
"""
```

### 6.2 函数 / 方法 docstring

四段式中文：`参数` / `返回` / `异常` / `副作用`。签名变更时同步更新。

### 6.3 配置与日志

- 配置一律读 `app.config.settings.Settings`（静态 `Settings.X`），不实例化、不传递
  `Settings` 对象。
- 日志统一 `from app.config.logging.logger import log`，禁止业务代码散落 `getLogger`。
- 跨进程 spawn 经 `process_bridge` 队列桥汇入父进程管线。

---

## 七、待定 / 开放项

以下为 `guard/` 落地前仍需最终拍板的约定细节，本节随讨论结论更新：

1. **写类工具 `data` 是否需要暴露被写文件路径**（供后置守卫读盘检查）？当前
   `write_file` 仅在 `content` 文本里含路径，`data` 为空；`patch` 的 `data` 含
   `diff_stats` 而非 per-file path 列表。若采用后置审查型守卫，需约定暴露方式。
2. **语法错误的语义**：装饰器 pre 拦截为「真拦截（文件不落盘）」；若改为 post
   输出替换（文件已写、返回 error），则语义为「文件已写但报告失败、模型重试覆盖」。
3. **多个守卫的串行与短路**：当前约定「按装饰器顺序串行，后续守卫总跑（观察到什么
   就是什么）」；是否需要「某守卫已将观察替换为 error 后中断后续」的机制待定。
4. **JS/**TS **的语法底线**：无标准库等价物，依赖 ESLint（best-effort）；是否加
   `node --check` 轻量兜底待定。
