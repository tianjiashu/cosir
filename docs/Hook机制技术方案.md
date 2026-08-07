# Hook 机制技术方案（进程内 Python Hook）

> 状态：**审查通过**（第 3 版；两轮独立审查，第 2 轮结论「通过，阻断项 0」，本版已吸收其 5 条建议；代码尚未实现）
> 适用范围：`apps/backend/app/core/hook/` + `apps/backend/app/tools/tool_execute/`
> 创建 / 修订：2026-08-06
> 现状事实：`app/core/hook/` 当前仅有空 `__init__.py` 与占位 `hook_base.py`（共 7 行：`class HookBase(object)` + 空 `__init__`，**非 ABC**、无 docstring），无任何被调用点。

---

## 一、目标与边界

### 1.1 目标

在 Agent 运行链路的关键生命周期节点引入**进程内 Python Hook 扩展点**，使项目内部可以在不改动主链路代码的前提下，插入规则校验、审计与上下文注入（预留，首版仅落日志）等横切逻辑。

### 1.2 已确认决策（本方案的不可动摇前提）

| # | 决策 | 说明 |
|---|---|---|
| D1 | **形态：进程内 Python 类** | 复用已建的 `HookBase`。**不做**进程外脚本 / stdio 协议 / subprocess 执行器。 |
| D2 | **事件：支持全部 7 类** | `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` / `SessionStart` / `SessionEnd` / `PreCompact`。「支持」= 枚举 + 契约 + 挂接点接通；不要求每个事件首版都有内置实现。 |
| D3 | **无任何配置层** | 无 `hooks.json`、无 `Settings` 新增字段、无配置加载模块、无环境变量开关。 |
| D4 | **只有 `HookRegistry`** | 不单列 `hook_runner.py` / `hook_manager.py`；执行编排（异常隔离/聚合短路）直接收进 `HookRegistry.fire()`。 |
| D5 | **内部扩展，不对外** | Hook 是项目开发者的内部扩展点，不是终端用户 API 表面；不存在用户自定义 Hook 加载通道。 |
| D6 | **`PreToolUse` 的 deny 为硬拒绝** | 直接返回 `tool_error` 观察，**不**复用 LangGraph `interrupt()` 审批。理由：审批是 agent 权限层职责，Hook 是规则层。 |
| D7 | **`additional_context` 首版不进模型上下文** | 仅落日志 / 作为 `HookResult` 字段回传给调用方，`RuntimeContextBuilder` 本版不改。 |

### 1.3 非目标（明确不做）

- 不做进程外 hook、不做 hook 配置文件与热加载。
- 不做用户可见的 hook 管理 UI / API、不做 hook 执行记录表（仅日志）。
- 不改 `RuntimeContextBuilder` / `system_prompt_builder`。
- **不做危险命令拦截 Hook**（详见 §七修订说明）。

---

## 二、目录结构与职责

```text
apps/backend/app/tools/tool_execute/
  tool_call_interceptor.py   # 新增：ToolCallInterceptor Protocol + InterceptDecision 值对象
                             # （tools 层自定义自消费，零 core 依赖）

apps/backend/app/core/hook/
  __init__.py            # 薄壳 re-export：HookBase / HookEvent / HookContext / HookResult /
                         # HookDecision / HookRegistry / get_hook_registry
  hook_event.py          # HookEvent 枚举（7 类事件）+ HookDecision 枚举（决策单一事实来源）
  hook_context.py        # HookContext 值对象（Hook 输入）
  hook_result.py         # HookResult 值对象（Hook 输出；决策枚举在 hook_event.py）
  hook_base.py           # HookBase 抽象基类（已存在占位，本次重写）
  hook_registry.py       # HookRegistry：register / resolve_for / list_hooks / fire
                         # + 进程级单例 get_hook_registry()
  hook_tool_interceptor.py  # ToolCallInterceptor 的 core 侧实现，转调 HookRegistry.fire
  builtins/
    __init__.py          # bootstrap_hooks(registry)：集中注册全部内置 Hook
    tool_audit_hook.py   # PostToolUse：工具执行结果审计日志（首版唯一内置 Hook）
```

**单一职责说明**（对照规范第一章，每个文件一句话、无「和」）：

- `tool_call_interceptor.py`：定义工具调用前后的拦截契约。
- `hook_event.py`：定义 Hook 生命周期事件枚举。
- `hook_context.py`：承载一次 Hook 触发的输入快照。
- `hook_result.py`：承载一次 Hook 执行的决策输出。
- `hook_base.py`：约束 Hook 实现类的契约。
- `hook_registry.py`：管理 Hook 的注册解析与隔离执行调度。
- `hook_tool_interceptor.py`：把工具调用拦截事件适配为 Hook 触发。
- `builtins/tool_audit_hook.py`：记录工具执行结果审计日志。

> **`hook_registry.py` 兼任索引与执行**是 D4 的直接结果。判定依据：其一句话职责为「管理 Hook 的注册解析与隔离执行调度」——注册与触发是同一件事的两面，且执行逻辑（约 50 行）不含独立业务规则。**拆分阈值（写入文档作为后续判据）**：若 fire 逻辑引入并发执行、优先级图或执行策略分支，立即拆出 `hook_dispatcher.py`。

---

## 三、分层与依赖方向合规性

`app/core/hook/` 属 `core` 层，`app/tools/tool_execute/` 属 `tools` 层。依据 `AGENTS.md` 第五章第 2 条：`core → tools` 合法，`tools → core` 违规。

| 依赖 | 方向 | 合规 |
|---|---|---|
| `core/hook/*` → `app.config.logging.logger` | core → config | ✅ |
| `core/hook/hook_context` → `app.tools.schemas`（`ToolObservation` / `ToolExecutionContext`） | core → tools | ✅ |
| `core/hook/hook_tool_interceptor` → `app.tools.tool_execute.tool_call_interceptor`（Protocol） | core → tools | ✅ |
| `tools/tool_execute/tool_scheduler` → `tools/tool_execute/tool_call_interceptor` | tools 包内 | ✅ |
| `api/app.py` → `core.hook` + `tools.tool_system` | api → core / tools | ✅ |
| ~~`core/hook/builtins` → `app.tools.tool_handler.terminal.*`~~ | **禁止** | ❌ 见下 |

### 3.1 `terminal` 包的额外约束（第 1 版误判，本版修正）

`app/tools/tool_handler/terminal/__init__.py:3-5` 明确写明：

> 本包为工具系统内部引擎，只被同包 `app.tools.tool_handler.*` 调用，禁止被 `app.api` / `app.core` / `app.service` 跨层直调。

因此即便 `core → tools` 总方向合法，**`core/hook/` 也不得 import `terminal` 子包下的任何模块**（含 `detect_dangerous_command`）。这是本版删除危险命令拦截 Hook 的依据之一（另一依据见 §七）。

---

## 四、挂接点设计

### 4.1 `PreToolUse` / `PostToolUse`（依赖倒置）

**问题**：最自然的挂点在 `ToolScheduler.execute`（`tools` 层），但 `tools → core` 是反向依赖。

**方案**：采用与 `ToolTraceRecorder`（`service/tool_execution/tool_trace_recorder.py`）同构的**依赖倒置**范式——**定义方 = 消费方，实现方在另一层**。

1. **tools 层定义 Protocol 与值对象**（`tool_call_interceptor.py`，零 core 依赖）：

```python
@dataclass(frozen=True)
class InterceptDecision:
    """工具调用前置拦截的裁决结果。"""
    allowed: bool = True
    reason: str = ""
    modified_arguments: dict | None = None


class ToolCallInterceptor(Protocol):
    def before_tool_call(
        self, tool_name: str, arguments: dict,
        context: ToolExecutionContext | None,
    ) -> InterceptDecision: ...

    def after_tool_call(
        self, tool_name: str, observation: ToolObservation,
        context: ToolExecutionContext | None,
    ) -> None: ...
```

2. **`ToolScheduler` 接入**（`tool_scheduler.py`）：
   - `__init__` 新增 `interceptor: ToolCallInterceptor | None = None`。
   - `before_tool_call` 调用点：参数校验通过之后（当前 `:224` 之后、`:228` 文件状态协调分支之前）。`allowed=False` 时经既有 `tool_error(...)` 构造 error 观察并经 `_apply_output_budget` 短路返回（复用现有失败归一化路径，不新造返回形态）；`modified_arguments` 非空时替换 `validation.arguments` 后继续。
   - `after_tool_call` 调用点：两条真实执行分支各自 `self._executor.execute(...)` 返回之后（当前 `:283` 后与 `:315` 后）。

3. **core 层实现 Protocol**（`hook_tool_interceptor.py`）：构造 `HookContext` 并转调 `get_hook_registry().fire(...)`，把 `HookResult` 翻译为 `InterceptDecision`。

4. **api 层装配**（见 §4.5）。

**`after_tool_call` 触发语义（显式声明，第 3 版修正）**

准确表述是：**仅在拿到 `ToolObservation` 后触发**，而非「进入执行后触发」。两者在一条路径上不等价。

不触发 `after_tool_call` 的路径分两类：

- **工具从未执行**（前置短路）：未知工具 `:148`、profile 拒绝 `:165`、缺工作区上下文 `:184`、参数非法 `:209`、文件路径/状态协调失败 `:236~261`、`early_observation` `:263`、stale `:274`；以及 `before_tool_call` 的 deny。
- **已进入执行但无 observation**：文件工具分支的 `except RuntimeError` `:290-306`。该 except 覆盖 `lock` / `execute` / `complete` 三个阶段，工具**可能已经真实执行**（例如 `complete` 阶段抛错时文件已被写入），但因异常导致无 `observation` 可传，故不触发。

> 该分类是 `after_tool_call(observation)` 签名的直接后果——无 observation 即无法调用。若未来需要覆盖失败路径，应新增独立的 `on_tool_error` 契约，而非把 observation 参数放宽为可选。

两类路径均须由测试用例锁定。

**跨进程边界（显式声明）**：`ToolExecutor` 对 `execution_mode="process"` 的工具（当前仅 `execute_terminal`）走子进程隔离。**Hook 全部在父进程执行**（`ToolScheduler` / `runner` / `lifespan` 均在父进程），隔离子进程内不存在 Hook 机制。

### 4.2 `UserPromptSubmit` / `Stop`

| 事件 | 挂点（`app/core/runtime/runner.py`） | 依赖方向 |
|---|---|---|
| `UserPromptSubmit` | `run_turn` 内、`claim_pending_turn` 成功之后、`RUN_STARTED` emit 之前（当前 `:335` 附近） | core → core ✅ |
| `Stop` | `run_turn` 正常路径 `_persist_turn_trajectory` 之后、`_publish_stable_file_changes` 之前（当前 `:366`/`:367` 之间） | core → core ✅ |

**`UserPromptSubmit` 的 deny 不阻断（显式限制）**：记 warning 日志后继续执行。理由：turn 已被 `claim_pending_turn` 认领，硬中断需额外的终态收敛路径（`update_turn_status` + `RUN_FAILED` 事件 + `_mark_stable_file_changes`），会显著扩大侵入面。若后续需要真正阻断，作为独立变更设计终态收敛。**实现者不得误以为此处 deny 会生效。**

### 4.3 `SessionStart` / `SessionEnd`

挂点：`app/api/app.py` 的 `lifespan`。

- `SessionStart`：`_mark_boot_ready()`（`:106`）之前。
- `SessionEnd`：`finally` 块内、`close_service_dependencies()`（`:113`）之前。

**"Session" 语义澄清**：本项目无独立"会话"概念，此处 Session = **后端进程生命周期**，与前端 task/turn 无关。

### 4.4 `PreCompact`

context compaction 当前未实现。本方案**只交付枚举成员与契约**，不写空调用点、不写 TODO 占位、不写无触发方的 Hook 实现。compaction 落地时由其模块自行 `fire(PRE_COMPACT, ...)`。

> 无死代码、无注释掉的代码，符合规范第三章「禁止先堆上去以后再整理」。

### 4.5 装配路径（第 1 版含糊，本版明确）

真实事实：`ToolScheduler` 在 `ToolSystem.build_tool_system`（`tool_system.py:94-97`）内部构造，`api/app.py:92` 只拿到已构造好的 `ToolSystem`；且 `ToolSystem` 是 `frozen dataclass`。因此第 1 版「在 lifespan 注入」缺少可行机制。

**本版方案**：给 `build_tool_system` 增加一个 **tools 层 Protocol 类型**的参数，由 api 层传入 core 侧实现：

```python
# tool_system.py（tools 层）
@classmethod
def build_tool_system(
    cls,
    client: CodeGraphKernelClient | None = None,
    interceptor: ToolCallInterceptor | None = None,   # tools 层 Protocol
) -> "ToolSystem":
    ...
    scheduler = ToolScheduler(
        registry=registry,
        output_budget=ToolOutputBudget(Settings.MAX_TOOL_OUTPUT_CHARS),
        interceptor=interceptor,
    )
```

```python
# api/app.py（api 层，lifespan 内），当前 :89~:106 段落
_kernel_supervisor = await _start_codegraph_kernel()

initialize_hook_registry()  # 新增：必须早于任何 fire 与 interceptor 构造

if runtime_override is None:
    tool_system = tool_system or ToolSystem.build_tool_system(
        _codegraph_client(), interceptor=HookToolInterceptor()
    )
    ...
else:
    ...  # 覆写分支不注入 interceptor，见下

get_hook_registry()._fire(HookContext(event=HookEvent.SESSION_START))  # 新增
_mark_boot_ready()
```

**装配顺序约束（第 3 版补充）**：

1. `initialize_hook_registry()` 必须在 `build_tool_system` 之前——`HookToolInterceptor` 构造后即可能被调用。
2. `SESSION_START` 的 fire 必须在 `initialize_hook_registry()` 之后、`_mark_boot_ready()`（`:106`）之前。
3. `SESSION_END` 的 fire 在 `finally` 块内、`close_service_dependencies()`（`:113`）之前。

**`runtime_override` 分支（真实存在，第 2 版遗漏）**：`lifespan` 有 `runtime_override is not None` 的测试覆写分支（`:97-104`），该分支不调用 `build_tool_system`，因此**不注入 interceptor**，Pre/PostToolUse 不生效。这是可接受的——该分支专供测试注入自定义 runtime，测试若需验证 Hook 应自行构造带 interceptor 的 `ToolSystem` 传入。此限制须写入代码注释。

依赖方向验证：`tool_system.py` 只 import 同层 Protocol；`api` 同时 import `core.hook` 与 `tools`，由 api 完成组装。**无反向依赖。**

**覆盖面**：`ToolScheduler.execute` 的调用方只有 `ToolExecutionService`（`tool_execution_service.py:166`），且全链路共享 `ToolSystem` 持有的同一 scheduler 单例，因此一处注入即覆盖全部工具调用。

**向后兼容**：`interceptor` 默认 `None` 时，`ToolScheduler.execute` 行为与现状完全一致（由测试用例锁定）。

---

## 五、核心契约

### 5.1 `HookEvent`

```python
class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    STOP = "stop"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_COMPACT = "pre_compact"
```

### 5.2 `HookContext`（frozen dataclass）

| 字段 | 类型 | 适用事件 | 说明 |
|---|---|---|---|
| `event` | `HookEvent` | 全部 | 触发事件 |
| `task_id` | `str \| None` | 多数 | 任务标识 |
| `turn_id` | `str \| None` | 多数 | 轮次标识 |
| `workspace_id` | `str \| None` | 多数 | 工作区标识 |
| `tool_name` | `str \| None` | Pre/PostToolUse | 工具名，同时是 matcher 匹配目标 |
| `tool_arguments` | `dict \| None` | PreToolUse | 已通过 schema 校验的参数 |
| `tool_observation` | `ToolObservation \| None` | PostToolUse | 工具执行结果 |
| `session_id` | `str \| None` | 全部 | 会话标识（桌面一次性后端进程对应一个 session）；`UserPromptSubmit` 等事件经此关联会话，不携带用户输入原文（避免落盘敏感 prompt，对齐 §7.2） |
| `metadata` | `dict` | 全部 | 扩展位，声明为 `field(default_factory=dict)` |

> `metadata` **必须**用 `field(default_factory=dict)`，禁止裸 `= {}`（可变默认值反模式）。

**关于「通用契约含工具专有字段」的取舍（显式声明）**：`HookContext` 是全 7 类事件共用的载体，却含 4 个工具专有字段（`tool_name` / `tool_arguments` / `tool_observation` 及对 `ToolObservation` 的类型依赖），非工具事件下恒为 `None`。

这是**有意选择**，理由：

- 备选方案是按事件拆分多个 Context 子类（`ToolHookContext` / `PromptHookContext` / ...），但会使 `HookRegistry.fire` 的签名泛型化、`HookBase.execute` 失去统一类型，为 7 个事件引入 7 个类型——复杂度收益不成正比。
- 扁平 Optional 字段 + `metadata` 扩展位是进程内 Hook 的常见做法，字段数量可控（9 个）。
- `core → tools` 是合法依赖方向（`ToolObservation` 位于 `app.tools.schemas`，非受限的 `terminal` 子包）。

**拆分阈值**：若工具专有字段增至 6 个以上，或出现第二个事件族的专有字段簇（如 compaction 专有的 3+ 字段），则按事件族拆分 Context。

### 5.3 `HookDecision` / `HookResult`

```python
class HookDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"

@dataclass(frozen=True)
class HookResult:
    decision: HookDecision = HookDecision.ALLOW
    reason: str = ""
    modified_arguments: dict | None = None   # 仅 PreToolUse 有效
    additional_context: str | None = None    # 首版仅落日志（D7）
```

**去掉 `ASK`**：D6 已确定 deny 为硬拒绝、不走审批中断，`ASK` 无消费方，保留即死枚举。未来接入审批时作为独立变更引入。

### 5.4 `HookBase`

```python
class HookBase(ABC):
    """进程内 Hook 的抽象契约。"""

    hook_event: ClassVar[HookEvent]   # 子类必须声明
    matcher: ClassVar[str] = ""       # 正则；空串匹配该事件全部触发

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def execute(self, context: HookContext) -> HookResult: ...
```

**删除 `timeout_seconds`**（第 1 版保留，本版删）：Python 无法安全中断同步函数调用，该字段只能做「耗时告警阈值」，字段名具误导性；且首版内置 Hook 为微秒级纯计算，告警永不触发，属为未来预留的死能力，违反规范第三章防膨胀。若未来确需硬超时，届时以线程池 + `future.result(timeout)` 方案作为独立变更引入。

**同步接口**：全部 Hook 为同步方法。理由：内置 Hook 为纯计算 + 日志，无 I/O 等待；同步接口可同时被同步的 `ToolScheduler` 与异步的 `run_turn` 调用。`run_turn` 内直接同步调用（微秒级，不阻塞事件循环），**不**包 `asyncio.to_thread`（避免为微秒级调用付出线程切换成本）。若未来出现耗时 Hook，需重新评估。

子类**不应**抛异常；抛出的异常由 `HookRegistry.fire()` 隔离。

---

## 六、`HookRegistry` 设计

参考 `AgentProfileRegistry`（`core/agents/agent_profile_registry.py`）的极简内存注册表风格。

```python
class HookRegistry:
    def register(self, hook: HookBase) -> None
    def resolve_for(self, context: HookContext) -> list[HookBase]
    def list_hooks(self) -> list[HookBase]
    def fire(self, context: HookContext) -> HookResult
```

### 6.1 `resolve_for` 匹配规则

1. 按 `context.event` 取该事件下已注册 Hook（按注册顺序）。
2. `matcher` 为空 → 匹配。
3. `matcher` 非空 → `re.search(matcher, context.tool_name or "")`。
4. 正则在 `register` 时 `re.compile` 预编译并缓存；编译失败 → `register` 抛 `ValueError`（**fail-fast**：这是开发者编码错误，必须在启动期暴露）。

**`matcher` 的适用范围（显式声明）**：`matcher` 只匹配 `tool_name`，因此它**只对 `PRE_TOOL_USE` / `POST_TOOL_USE` 有意义**。非工具事件（`UserPromptSubmit` / `Stop` / `SessionStart` / `SessionEnd` / `PreCompact`）的 `tool_name` 恒为 `None`，此时**非空 matcher 恒不命中**，该事件的 Hook 必须留空 matcher。此行为是有意设计而非缺陷，须写入 `HookBase.matcher` 的 docstring，防止实现者给非工具事件 Hook 配 matcher 后困惑于「Hook 从不触发」。

### 6.2 `fire` 执行语义

```
for hook in resolve_for(context):
    try:
        result = hook.execute(context)
    except Exception:
        log.exception("hook_failed", ...)   # fail-open，继续下一个
        continue
    if result.additional_context:
        contexts.append(result.additional_context)
    if result.decision is DENY:
        log.warning("hook_denied", ...)
        return HookResult(DENY, reason=result.reason,
                          modified_arguments=result.modified_arguments)   # 短路
    if result.modified_arguments is not None:
        merged_arguments = result.modified_arguments   # 后者覆盖前者
return HookResult(ALLOW, modified_arguments=merged_arguments,
                  additional_context="\n".join(contexts) or None)
```

**fail-open 决策**：Hook 抛异常时视为 ALLOW 并继续后续 Hook。理由：Hook 是横切增强，一个坏 Hook 不应拖垮 Agent 主链路。异常必须 `log.exception`（含 hook 名、event、上下文关键 ID、完整 traceback）。

**空注册表**：`resolve_for` 返回空列表时 `fire` 直接返回默认 `HookResult()`（ALLOW），零开销。这是首版 6 个无实现事件的正常路径。

### 6.3 两个输出字段的消费方状态（显式声明，避免死字段误解）

| 字段 | 链路是否打通 | 首版有无触发方 | 说明 |
|---|---|---|---|
| `modified_arguments` | ✅ 完整（`fire` → `InterceptDecision` → `ToolScheduler` 替换 `validation.arguments`） | 无（首版无内置 PreToolUse Hook） | 链路必须由测试用例覆盖（用测试专用 Hook 触发），保证后续接线时不返工 |
| `additional_context` | ⚠️ 仅收集，无消费方 | 无 | D7 决定首版不进模型上下文。`fire` 收集拼接后放入返回的 `HookResult`；`HookToolInterceptor` **不**消费它（`InterceptDecision` 不含该字段）。它当前的唯一用途是让 `run_turn` 侧的 `fire` 调用方可按需 `log.debug` 记录 |

> `additional_context` 是本方案中唯一「保留但暂无实际消费」的字段。保留理由：它是 D7 明确点名的未来通道，且零运行成本（`None` 时不参与任何逻辑）。**若实现时发现连日志都不记，应直接删除该字段**，待 D7 解禁时再加——不留无人读的字段。

### 6.4 进程级单例与线程安全

```python
_HOOK_REGISTRY: HookRegistry | None = None

def get_hook_registry() -> HookRegistry:
    """返回进程级 Hook 注册表；未初始化时抛 RuntimeError。"""

def initialize_hook_registry() -> HookRegistry:
    """构建注册表、播种全部内置 Hook 并设为进程级单例。"""
```

**线程安全设计（第 1 版遗漏，本版补）**：真实事实是 `ToolExecutionService.run_calls_with_events` 整体运行在 `asyncio.to_thread` 的**工作线程**（`tool_execution_service.py:139` 注释明确），而 `UserPromptSubmit` / `Stop` 的 fire 在**事件循环线程**。两者并发访问同一 registry。

因此**不采用懒初始化**（会有 double-init 竞态），改为：

- `initialize_hook_registry()` 在 `api/app.py` lifespan **启动期单线程**调用一次（在 `HookToolInterceptor()` 构造之前）。
- 运行期 registry **只读**：`resolve_for` / `fire` / `list_hooks` 不修改内部状态，`register` 仅在启动期调用。只读并发访问无需加锁。
- `get_hook_registry()` 未初始化即调用 → 抛 `RuntimeError`（与 `config/configuration.py` 的 `get_agent_registry` 一致的 fail-fast 语义）。
- 测试可直接构造 `HookRegistry()` 实例，不依赖进程单例。

> 与 `AgentProfileRegistry` 的差异说明写入 `hook_registry.py` 模块 docstring：hook registry 无 override 场景、无 API 查询需求，故 `initialize_*` 内部完成播种，不像 `build_agent_registry` 那样把播种与注入分离。

---

## 七、内置 Hook 清单（本版大幅收敛）

| 文件 | 事件 | matcher | 职责（一句话） |
|---|---|---|---|
| `tool_audit_hook.py` | `POST_TOOL_USE` | `""` | 记录工具执行结果审计日志 |

其余 6 类事件：**枚举与挂接点接通，首版零注册 Hook**。`fire` 对空列表直接返回 ALLOW，零运行开销。这满足 D2——「支持全部事件」支持的是**事件与挂接点**，而非强制每个事件都有实现。

### 7.1 删除 `tool_guard_hook` 的理由（第 1 版保留，本版删除）

第 1 版设计了 `tool_guard_hook`（PreToolUse 复用 `detect_dangerous_command` 拦截危险命令），独立审查判定为**双重阻断**，本版删除：

1. **依赖违规**：`terminal/__init__.py:3-5` 明确禁止 `app.core` 跨层直调该包，`core/hook/builtins/` 无法合法 import `detect_dangerous_command`（第 1 版 §3 表格误标为合规）。
2. **重复实现**：`ExecuteTerminalTool.execute` 内部**已经**调用 `detect_dangerous_command` 并返回阻断观察。Hook 再拦一次构成同一安全规则的两处触发点，两处 `reason` 文案不同会造成模型感知不一致，且新增 deny 模式时极易漂移。违反第零铁律「不重复造轮子是不可解除的底线」。原「省子进程开销」收益极微（拦截仅发生在父进程、子进程尚未启动前），不足以抵消双实现代价。

**结论**：危险命令判定的唯一事实来源保持为 `dangerous_command.detect_dangerous_command`，唯一触发点保持为 `ExecuteTerminalTool.execute`。Hook 机制不复制该规则。

### 7.2 保留 `tool_audit_hook` 的理由

它是**唯一一个真正被触发的内置 Hook**，作用是证明 `PostToolUse` 挂接链路（Protocol → interceptor → registry → hook）端到端可用，并为后续内部扩展提供可复制样板。为避免日志噪音：

- 使用 `log.debug` 级别，不污染默认 info 输出。
- 字段与既有 `TOOL_CALL_FINISHED` 事件、`api_logging` 中间件日志**不重复**：只记 `hook_name` / `tool_name` / `status` / `turn_id`。
- docstring 中说明「本 Hook 的首要价值是保持 PostToolUse 挂接点活跃，日志为次要产出」。

---

## 八、日志规范落实

统一 `from app.config.logging.logger import log`，落 `logs/app.log`。

| 日志键 | 级别 | 触发 | 关键字段 |
|---|---|---|---|
| `hook_denied` | warning | `fire` 收到 DENY | `hook_name` / `event` / `reason` / `task_id` / `turn_id` / `tool_name` |
| `hook_failed` | error（`log.exception`） | Hook 抛异常 | `hook_name` / `event` / `task_id` / `turn_id` / `tool_name` + traceback |
| `hook_registered` | debug | 启动期注册内置 Hook | `hook_name` / `event` / `matcher` |
| `tool_audit` | debug | `tool_audit_hook` 执行 | `hook_name` / `tool_name` / `status` / `turn_id` |

**禁止**：记录完整 `tool_arguments`（可能含文件全文、命令原文）与完整 `prompt`。只记 `tool_name`、参数**键名**列表、prompt 长度。若确需记录命令类内容，必须经 `app.trace_infra.redaction` 脱敏。

---

## 九、测试计划（交由独立测试 Agent 执行）

| 用例 | 覆盖点 |
|---|---|
| `register` 后 `resolve_for` 按 event 命中 | 注册与索引 |
| `matcher` 正则命中 / 未命中 / 空串全匹配 | 匹配规则 |
| `matcher` 非法正则 → `register` 抛 `ValueError` | fail-fast |
| `get_hook_registry` 未初始化 → 抛 `RuntimeError` | fail-fast |
| Hook 抛异常 → `fire` 返回 ALLOW 且写 `hook_failed` 日志 | fail-open + 日志 |
| 首个 DENY 短路，后续 Hook 不执行 | 短路聚合 |
| 多个 Hook 的 `additional_context` 拼接、`modified_arguments` 后者覆盖 | 聚合语义 |
| 空注册表 `fire` → 返回 ALLOW、零异常 | 边界（6 类无实现事件的正常路径） |
| `HookContext` 两个实例的 `metadata` 互不共享 | 可变默认值 |
| 非工具事件下非空 matcher 恒不命中 | matcher 适用范围 |
| `ToolScheduler` 注入 interceptor 后 deny → 返回 `tool_error` 观察且不执行工具 | PreToolUse 挂接 |
| `ToolScheduler` deny 时**不**触发 `after_tool_call` | 触发语义 |
| 未知工具 / 参数非法等前置短路路径**不**触发 `after_tool_call` | 触发语义 |
| 文件工具 `except RuntimeError` 路径**不**触发 `after_tool_call` | 触发语义（已执行但无 observation） |
| `modified_arguments` 生效：实际执行收到替换后的参数（用测试专用 Hook 触发） | 参数改写链路 |
| `ToolScheduler` 未注入 interceptor（None）→ 行为与现状一致 | 向后兼容 |
| `build_tool_system` 不传 interceptor → 与现状一致 | 向后兼容 |
| 日志不含完整参数值 / prompt 原文 | 敏感信息 |

---

## 十、实施顺序

1. 契约层：`hook_event.py` / `hook_context.py` / `hook_result.py` / `hook_base.py`（重写占位）。
2. `hook_registry.py`（register / resolve_for / list_hooks / fire / `get_hook_registry` / `initialize_hook_registry`）。
3. `builtins/`：`tool_audit_hook.py` + `bootstrap_hooks`。
4. tools 层契约：`tool_execute/tool_call_interceptor.py`（Protocol + `InterceptDecision`）。
5. `ToolScheduler` 接入可选 interceptor；`ToolSystem.build_tool_system` 增加 interceptor 参数。
6. core 侧实现：`hook_tool_interceptor.py`。
7. 挂接：`api/app.py` lifespan（`initialize_hook_registry` + 注入 interceptor + SessionStart/End）；`runner.run_turn`（UserPromptSubmit / Stop）。
8. 单元测试。
9. 独立审查 Agent + 独立测试 Agent 闭环，直至双通过。

---

## 十一、遗留风险与显式限制

| # | 限制 | 处理 |
|---|---|---|
| R1 | `UserPromptSubmit` 的 deny 不阻断执行 | §4.2 显式声明；需要阻断时作为独立变更设计终态收敛 |
| R2 | `PreCompact` 无触发方 | 仅交付枚举与契约，compaction 落地时接入；不留 TODO 死代码 |
| R3 | 无超时保护能力 | 已删除 `timeout_seconds` 误导性字段；Hook 须自我约束为快速纯计算 |
| R4 | 6 类事件首版零内置 Hook | 有意为之：挂接点已通，避免造无价值的日志 Hook |
| R5 | `ToolScheduler` / `build_tool_system` 新增可选参数 | 默认 `None` 时行为与现状一致，由测试用例锁定 |
| R6 | Hook 仅在父进程执行 | §4.1 显式声明；`process` 隔离子进程内无 Hook |
| R7 | registry 运行期只读、不支持动态注册 | 启动期单线程播种；动态注册需求出现时须先设计并发方案 |
| R8 | `HookRegistry` 兼任索引与执行 | 已设定拆分阈值（§二末），触发即拆 |
| R9 | `additional_context` 首版无消费方 | §6.3 显式声明；实现时若连日志都不记则直接删除该字段 |
| R10 | `matcher` 仅对工具类事件有效 | §6.1 显式声明并写入 docstring |
| R11 | `lifespan` 的 `runtime_override` 分支不注入 interceptor | §4.5 显式声明；测试需 Hook 时自行传入带 interceptor 的 ToolSystem |
| R12 | `HookContext` 含工具专有字段 | §5.2 说明取舍并设定按事件族拆分的阈值 |

---

## 十二、审查记录

| 轮次 | 结论 | 处理 |
|---|---|---|
| 第 1 轮 | **不通过**，2 阻断 | 阻断① `tool_guard_hook` 违反 `terminal` 包跨层禁止 + 与 handler 既有拦截重复 → 删除该 Hook（§7.1）；阻断② 装配路径不可行（`ToolScheduler` 由 frozen 的 `ToolSystem` 内部构造）→ 改为 `build_tool_system` 增参（§4.5）。另修 `timeout_seconds` 误导性字段、`metadata` 可变默认值、线程安全等建议项 |
| 第 2 轮 | **通过**，0 阻断 | 吸收 5 条建议：S1 `after_tool_call` 触发语义补 `except RuntimeError` 路径（§4.1）；S2 `additional_context` 消费方状态显式化（§6.3）；S3 `HookContext` 耦合取舍与拆分阈值（§5.2）；S4 `matcher` 适用范围（§6.1）；S5 lifespan 装配顺序与 `runtime_override` 分支（§4.5） |

> 两轮审查均由独立审查 Agent 执行。审查 Agent 无文件写入权限，报告内容随其回复返回，未落盘。
