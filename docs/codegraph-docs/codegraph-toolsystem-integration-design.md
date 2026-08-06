# CodeGraph 工具面接入 ToolSystem 设计方案

> 状态：计划方案（基于双 Agent 并行调研，待确认后开发）
> 日期：2026-08-04
> 前置：CodeGraph Kernel 常驻已打通（supervisor 启动接线 + node 就位 + 端到端验证）
> 依据：`codegraph-integration-discussion.md` 工具边界决策（第一版配齐 6 个查询工具，生命周期工具不暴露）

## 一、背景与目标

CodeGraph Kernel 已能常驻查询（`client.query` 可用），但 **agent 还不能通过 ToolSystem 调 codegraph 工具**——`tool_system.py` 只注册了 9 个现有工具。目标是：把 CodeGraph 查询能力做成 agent 可见工具，让 agent 在 turn 里真正能调 `codegraph_explore` 等，优先用 CodeGraph 理解代码，逐步替代文件搜索。

**关键前提**（讨论文档确认）：`status/init/sync` 等生命周期工具**不作为 agent 工具暴露**（由后端自动管理）；只暴露**只读查询工具**。

## 二、调研结论（双 Agent）

### 2.1 后端工具接入范式（可复用）
- **HandlerBase 契约**（`tool_base.py`）：类级 `name/description/permission/args_model/timeout_seconds/risk_level` + `execute`/`to_definition`。execute 必须接受 `execution_context: ToolExecutionContext | None = None` 最后一个 kwarg。
- **`execution_context.workspace_root`**：工具执行时**一定拿到当前 workspace 根路径**（`ToolExecutor` 强制注入，thread/process 都保证），来自 `WorkspaceRecord.root_path`。
- **注册范式**：每个工具文件底部 `build_xxx_definition()` 工厂 → `tool_system.py` 的 `build_tool_system()` 里 `registry.register(build_xxx_definition())`。
- **外部单例依赖先例**：web 工具（`web_search`/`web_extract`）通过**构造函数依赖注入**（`__init__(self, provider_registry)`）访问外部对象，`build_web_search_definition(provider_registry=None)` 支持可选注入。codegraph 工具可复用此模式。
- **execution_mode**：codegraph 是只读查询，用默认 `"thread"`（与 search_files 一致）；`"process"` 仅 execute_terminal（需 OS 隔离）。

### 2.2 codegraph 工具面（vendor）
- **7 个查询工具**（`tool-service.ts` QUERY_METHODS，**无 `codegraph_affected`**，尽管讨论文档列了）：`codegraph_explore/search/node/callers/callees/impact/files`。
- **执行契约**：`ToolService.dispatch(method, params)` → `ToolHandler.execute(toolName, args)`，`workspace_path` → `args.projectPath`；后端 `client.query(method, params)` 返回 `QueryResult(content: list[dict], is_error)`，**第一版透传 MCP 文本**。
- **NotIndexed 降级**：`execute` 把 `NotIndexedError` 捕获为**成功形状的文本提示**（「该项目未索引，建议用内置工具」，`isError` 不置位）——不会抛错中断。

### 2.3 工具参数表（vendor `tools.ts`）

| 工具 | 必填 | 可选（含默认） | 说明 |
|------|------|--------------|------|
| `codegraph_explore` | `query` | `maxFiles`(12) | 主工具，自然语言/符号名 → 源码+调用路径 |
| `codegraph_search` | `query` | `kind`(enum)、`limit`(10) | 按名符号搜索，仅位置 |
| `codegraph_node` | 无 | 符号模式 `symbol`/`includeCode`/`line`/`file`；文件模式 `file`/`offset`/`limit`(2000)/`symbolsOnly` | 读文件或读单个符号 |
| `codegraph_callers` | `symbol` | `file`、`limit`(20) | 谁调用它 |
| `codegraph_callees` | `symbol` | `file`、`limit`(20) | 它调用谁 |
| `codegraph_impact` | `symbol` | `file`、`depth`(2) | 改动影响面 |
| `codegraph_files` | 无 | `path`、`pattern`、`format`(tree)、`includeMetadata`、`maxDepth` | 索引文件树 |

公共可选参数 `projectPath`（由后端从 `workspace_root` 注入，不暴露给模型）。

## 三、设计决策

### 3.1 工具数量与命名
实现 **6 个** agent 可见 codegraph 工具（vendor 无 `affected`，与讨论文档的 6 个对齐）：
`codegraph_explore` / `codegraph_search` / `codegraph_node` / `codegraph_callers` / `codegraph_callees` / `codegraph_impact`。

`codegraph_files` **暂不暴露**（是文件树浏览，与现有 `list_directory`/`search_files` 功能重叠，且不是「代码理解」核心；第一版聚焦代码理解工具，避免工具面膨胀）。若后续需要再补。

### 3.2 handler 形态：共享一个 handler + 参数表驱动
**不写 6 个重复 handler**，而是**一个 `CodegraphQueryTool` handler，按 `name` 分发到 vendor 的不同 method**。理由：
- 6 个工具的执行逻辑高度同构（都是 `client.query(method, {..., workspace_path})`），只有 method 名 + 参数模型不同。
- 单一 handler 避免 6 份重复 `to_definition`/`execute` 骨架（不重复造轮子）。

每个工具一个 `args_model`（参数不同），handler 的 `execute` 按 `self.name` 路由到对应 vendor method，组装 params。

### 3.3 client 注入与装配（复用 web 工具先例 + 延迟降级）

**关键决策（独立审查修正）**：handler 构造函数**不调用** `get_kernel_supervisor().get_client()`（Kernel 未就绪时 `get_client()` 会抛错，导致整个 `build_tool_system` 失败，违背「工具仍注册、execute 才降级」）。改为**构造函数接收 client，可为 None，execute 时才降级**：

```python
def __init__(self, name, args_model, description, client: CodeGraphKernelClient | None = None):
    self._client = client          # 可为 None；Kernel 未就绪时工具仍注册，execute 降级
```

**装配策略**：`build_tool_system()` 需能拿到 client。当前它是无参 `@classmethod`（`api/app.py:86` 直接调用）。方案：
- 给 `build_tool_system(client: CodeGraphKernelClient | None = None)` 加可选参数，`api/app.py` 装配时从 `get_kernel_supervisor().get_client()` 取得（Kernel 就绪时注入；不可用时传 None，工具仍注册、execute 降级）。
- 测试直接用 fake client 注入。
- **不改变**「Kernel 不可用时工具仍注册」语义——client 为 None 不抛，execute 才返回降级 `tool_error`。

### 3.4 文件落点
```
apps/backend/app/tools/tool_models/codegraph_explore_args.py   # 6 个查询参数模型（一文件一模型）
apps/backend/app/tools/tool_models/codegraph_search_args.py
apps/backend/app/tools/tool_models/codegraph_node_args.py
apps/backend/app/tools/tool_models/codegraph_callers_args.py
apps/backend/app/tools/tool_models/codegraph_callees_args.py
apps/backend/app/tools/tool_models/codegraph_impact_args.py
apps/backend/app/tools/tool_handler/codegraph_query.py          # CodegraphQueryTool + build_*_definition 工厂
apps/backend/app/tools/tool_system.py                           # 注册 6 个 codegraph 工具
apps/backend/app/core/agents/agent_profile.py                    # DEFAULT_DEVELOPER_TOOLS 追加 6 个工具名（见 3.6）
```

### 3.5 权限 / risk / execution_mode
- `permission = "codegraph_query"`（新权限标签，区别于 file_search/network；只读）。`permission` 是**自由字符串**（无 allowed_permissions 注册集合，独立审查核实），无需额外注册。
- `risk_level = "low"`（只读查询）
- `execution_mode = "thread"`（默认，与 search_files 一致）
- `timeout_seconds`：explore/search/callers/callees/impact = 30s（对齐 search_files）

### 3.6 工具对 agent profile 可见（独立审查修正，最关键）

**问题**：`AgentProfile.select_tools()`（`app/core/agents/agent_profile.py:107`）按**工具名** `tool.name in self.allowed_tools` 过滤，`DEFAULT_DEVELOPER_TOOLS`（18-26 行）不含 codegraph 工具。只注册进 `tool_system.py`，模型仍看不到这 6 个工具（`runner.py` 的 `model_tools = agent_profile.select_tools(...)` 会过滤掉）。

**修复**：把 6 个 codegraph 工具名追加到 `DEFAULT_DEVELOPER_TOOLS`：
```python
DEFAULT_DEVELOPER_TOOLS = [
    ...现有 7 个...,
    "codegraph_explore", "codegraph_search", "codegraph_node",
    "codegraph_callers", "codegraph_callees", "codegraph_impact",
]
```
并按工具名逐个加入（`AgentProfile` 按工具名匹配，非权限名）。**验收必须验证** `agent_profile.select_tools` 后 `model_tools` 包含 6 个 codegraph 工具。

### 3.7 分层豁免：`tools → app/codegraph`（独立审查修正）

**问题**：tools 层依赖白名单为 `config/models/utils/trace_infra`（不依赖 service，`rules/目录组织规范.md` 1.1），`app/codegraph` 不在白名单（被 `service/codegraph/` 依赖，属 service 之下的独立基础设施）。`tool_handler/codegraph_query.py → app.codegraph` 方向虽为「上依赖下」，但未经豁免。

**修复**：像 `config/logging`（D6）/`config/configuration`（D14）那样，**把 `tools → app/codegraph` 显式定性为特例豁免**，写入 `rules/目录组织规范.md`（1.3 红线或特例节），说明：codegraph 工具 handler 依赖 `CodeGraphKernelClient`（只读 RPC 客户端，leaf 性质），经豁免允许。**依赖方向单向（tools → app/codegraph），tools 不反向依赖。**

## 四、详细设计

### 4.1 参数模型（一文件一模型，遵循项目约定）

**遵循项目约定「一文件一模型」**（`tool_models/` 现有文件均如此），6 个参数模型独立成文件：
```
tool_models/codegraph_explore_args.py    # query: str(必填), max_files: int = 12
tool_models/codegraph_search_args.py     # query: str(必填), kind: str|None, limit: int = 10
tool_models/codegraph_node_args.py       # symbol: str|None, file: str|None, offset, limit, include_code, line, symbols_only
tool_models/codegraph_callers_args.py    # symbol: str(必填), file: str|None, limit: int = 20
tool_models/codegraph_callees_args.py    # symbol: str(必填), file: str|None, limit: int = 20
tool_models/codegraph_impact_args.py     # symbol: str(必填), file: str|None, depth: int = 2
```
每个 `model_config = ConfigDict(strict=True, extra="forbid")`，字段 `Field(description=...)`。`tool_models/__init__.py` 统一 re-export。

### 4.2 handler `tool_handler/codegraph_query.py`

**关键签名决策（独立审查修正）**：6 个工具参数模型完全不同，handler 的 execute 必须用 `**kwargs` 接收（`ToolExecutor` 以 `handler(**arguments, execution_context=...)` 解包参数），再按 `self.name` 路由到对应 vendor method。

```python
class CodegraphQueryTool(HandlerBase):
    name = "codegraph_explore"      # 由 for_tool 按工具覆盖
    description = ""
    permission = "codegraph_query"
    args_model = CodegraphExploreArgs  # 由 for_tool 按工具覆盖
    timeout_seconds = 30.0
    risk_level = "low"

    def __init__(self, name: str, args_model: type[BaseModel], description: str, client=None):
        self.name = name
        self.args_model = args_model
        self.description = description
        self._client = client          # 可能为 None（Kernel 不可用时），execute 才降级

    @classmethod
    def for_tool(cls, name, args_model, description, client=None) -> "CodegraphQueryTool": ...

    def execute(self, *args, **kwargs) -> ToolObservation:
        execution_context = kwargs.pop("execution_context", None)
        # 1. workspace 缺失降级：workspace_payload 不在 _WORKSPACE_REQUIRED_PERMISSIONS，
        #    execution_context 可能为 None。无 workspace 时 vendor dispatch 必然失败
        #    （强制要求非空 workspace_path），故先降级。
        if execution_context is None or execution_context.workspace_root is None:
            return tool_error(self.name, "CodeGraph query requires a workspace",
                              reason="当前没有关联 workspace，无法执行 workspace_payload 查询；请在工作区内使用，或改用文件搜索工具",
                              permission=self.permission)
        # 2. Kernel 不可用降级
        if self._client is None:
            return tool_error(self.name, "CodeGraph unavailable",
                              reason="CodeGraph Kernel 未就绪，已降级到文件搜索工具", permission=self.permission)
        workspace_path = str(execution_context.workspace_root)
        # 3. 组装 params：kwargs（pydantic 校验后的参数）+ workspace_path
        params = {**kwargs, "workspace_path": workspace_path}
        try:
            result = self._client.query(self.name, params)
        except CodeGraphKernelUnavailableError:
            return tool_error(self.name, "CodeGraph unavailable",
                              reason="Kernel 不可用，降级到文件搜索工具", permission=self.permission)
        except CodeGraphKernelError as exc:
            return tool_error(self.name, f"CodeGraph query failed: {exc}",
                              reason="查询失败，可重试或改用文件搜索", permission=self.permission)
        # 4. result.content = [{type, text}...]，聚合为文本
        content = "\n".join(block["text"] for block in result.content if block.get("type") == "text")
        return tool_success(self.name, self.permission, content=content,
                            display_data={"tool": self.name, "query_params": kwargs})

    def to_definition(self) -> ToolDefinition: ...
```

### 4.3 工厂函数（6 个）

用 `for_tool(name, args_model, description, client)` 类方法按工具名构造（避免 6 个重复类），每个工具一个薄工厂：

```python
def build_codegraph_explore_definition(client=None):
    return CodegraphQueryTool.for_tool(
        "codegraph_explore", CodegraphExploreArgs, CODE_GRAPH_EXPLORE_DESCRIPTION, client
    ).to_definition()
# 其余 5 个类似（search/node/callers/callees/impact）
```

`for_tool` 内部用 `type(...)` 或显式赋值设置 `name`/`args_model`/`description`，返回 `CodegraphQueryTool` 实例。

### 4.4 ToolSystem 注册
`tool_system.py` `build_tool_system(client: CodeGraphKernelClient | None = None)` 加可选参数 + 6 行注册：
```python
registry.register(build_codegraph_explore_definition(client))
registry.register(build_codegraph_search_definition(client))
registry.register(build_codegraph_node_definition(client))
registry.register(build_codegraph_callers_definition(client))
registry.register(build_codegraph_callees_definition(client))
registry.register(build_codegraph_impact_definition(client))
```
`client` 由 `api/app.py` 装配时从 `get_kernel_supervisor().get_client()` 取得（Kernel 就绪时传入；**不可用时传 None**——`get_client()` 会抛 `CodeGraphKernelUnavailableError`，`api/app.py` 捕获后传 None，工具仍注册、execute 降级）。**`build_tool_system` 不自行调 `get_client()`**（避免构造即抛破坏整个工具系统装配）。

### 4.5 前端展示（ToolDisplayHints）
- `codegraph_explore`：`verb="代码语义查询"`、`icon="network"`、`expandable=True`、`expand_layout="list"`
- 其他：`verb="代码关系查询"` 等，`icon="network"`、`expand_layout="list"`
- `display_data`：聚合文本 + 结构化字段（如 tool/query/结果条数），**不含 content 完整副本**（对齐 `tool_success` 不变量）。

### 4.6 工具描述与参数规范（决定 agent 能否用好 codegraph）

> 用户强调：工具的 `description` 与参数描述必须**清晰完整**，这是 agent 决定「何时用、怎么用、何时回退」的关键。以下描述面向模型撰写（`tool_success.content` 同理用英文；此处为设计说明，落地时 description 用英文对模型最友好）。

#### 4.6.1 CodeGraph 总体使用策略（放系统提示 + explore 的 description）

```
理解代码结构、定位实现、追踪调用链、分析影响范围时，优先用 CodeGraph 系列工具：
  1. codegraph_explore —— 首选，几乎任何代码理解/改动前先用它（自然语言提问，
     一次拿相关源码 + 调用路径，最省 token）
  2. codegraph_node —— 需要「精读某文件或某符号」时，替代 Read（带调用关系）
  3. codegraph_search / codegraph_callers / codegraph_callees / codegraph_impact
     —— 窄查询，探索/定位/关系追踪的补充
仅当 CodeGraph 不可用、未索引、结果不足，或需要纯文本匹配/全文正则时，
才回退到 search_files / read_file。
原则：先 CodeGraph 建立结构视角 → 再精读必要源码 → 再用文件搜索兜底。
```

#### 4.6.2 各工具 description（落地用英文，此处中文解释设计意图）

**codegraph_explore（主工具，PRIMARY）**
```
理解代码结构、定位实现、追踪调用路径、分析影响范围的首选工具，也用于改动前摸底。
Query 支持自然语言问题或符号/文件名集合。一次返回相关符号的逐字源码（按文件分组）
+ 它们之间的调用路径。返回的源码等同已 Read，不要重复用 Read 重新打开。
通常这一个调用就够，比 search/Read/Grep 循环更省 token、更准。
仅当需要纯文本匹配、正则、或 CodeGraph 未覆盖时才回退 search_files/read_file。
```
- `query`：符号名 / 文件名 / 短代码词集合（如 `"AuthService loginUser session-manager"`），或自然语言问题（如「mutateElement 是怎么被调用的」），**不需要先 codegraph_search**。
- `max_files`（默认 12）：最多返回源码的文件数上限。

**codegraph_node（读文件/读单符号，替代 Read）**
```
两种模式：(1) 读整个文件——只传 file（路径或 basename），等同 Read 工具，返回带行号源码
+ 依赖它的文件，可 offset/limit 翻页；(2) 读单个符号——传 symbol，返回位置/签名/源码
（includeCode=true）+ caller/callee 调用链，改动前看它会破坏什么。名字有歧义时返回所有
匹配定义体，可传 file/line 锁定。需要一次看多个相关符号用 explore。
需要纯文本/正则搜索时回退 read_file/search_files。
```
- `file`：文件路径或 basename（如 `"harness.rs"`、`"src/auth/session.ts"`）。单独传 = 读文件；与 symbol 同传 = 消歧。
- `symbol`：要读的符号名（符号模式）。省略且只传 file = 读整个文件。
- `include_code`（默认 false）：符号模式是否包含完整源码体。
- `offset`/`limit`：文件模式翻页（等同 Read 的 offset/limit，上限 2000 行）。
- `symbols_only`（默认 false）：文件模式只返回符号表 + 依赖者（廉价结构概览）。
- `line`：符号模式，配合 file:line 锁定歧义符号。

**codegraph_search（快速符号定位，仅位置无代码）**
```
按名称快速搜索符号，只返回位置（不返回代码）。定位「某符号在哪定义」时用；
想直接读源码/理解整体用 explore。搜索结果往往用来喂给 node 精读。
```
- `query`：符号名或部分名（如 `"auth"`、`"signIn"`、`"UserService"`）。
- `kind`（可选）：按节点类型过滤（function/method/class/interface/type/variable/route/component）。
- `limit`（默认 10）：最大结果数。

**codegraph_callers / codegraph_callees（调用关系）**
```
callers：列出「谁调用」给定符号的函数（改动前看谁依赖它）。
callees：列出给定符号「调用谁」的函数（看它依赖什么）。
关系追踪的窄查询；一次看完整调用流用 explore。
```
- `symbol`：函数/方法/类名。
- `file`（可选）：同名符号多时用文件路径/后缀锁定（如 monorepo 每 app 一个 UserService）。
- `limit`（默认 20）：最大返回数。

**codegraph_impact（影响面分析）**
```
列出「改动某符号会影响哪些符号」，重构/改接口前必用，评估破坏面。
```
- `symbol`：待分析的符号名。
- `file`（可选）：同名符号锁定。
- `depth`（默认 2）：依赖遍历层数。

#### 4.6.3 描述书写原则
1. **先讲「何时用」再讲「怎么用」**：agent 先判断该不该用这个工具。
2. **明确「何时不用/回退」**：CodeGraph 不适用时引导回退 `search_files`/`read_file`，避免 agent 硬用。
3. **explore 标 PRIMARY**、node 标「替代 Read」——让 agent 建立「CodeGraph 优先」的直觉。
4. **参数描述给格式 + 例子 + 默认值**，歧义符号（同名）主动提示用 file 锁定。
5. **描述用英文**（对齐 `tool_success.content` 面向模型的约定），代码注释/中文说明不受限。

## 五、降级策略

| 场景 | handler 行为 |
|------|------------|
| **无 workspace**（execution_context 为 None） | execute 开头降级 `tool_error`（reason 引导「在工作区内使用或改用文件搜索」）——codegraph 不在 `_WORKSPACE_REQUIRED_PERMISSIONS`，execution_context 可能为 None，先降级避免 `None.workspace_root` 崩溃 |
| Kernel 未启动/不可用（client 为 None 或 query 抛 Unavailable） | `tool_error`（reason 引导用文件搜索） |
| workspace 未索引 | vendor `execute` 降级为成功形状文本（「未索引，建议用内置工具」）→ handler 原样透传，`is_error=False` |
| 索引过期 | 由 turn 前 ensure_ready 保证（已实现）；工具层不触发 sync |

## 六、测试计划

- `tests/test_codegraph_tool.py`：mock `CodeGraphKernelClient`，验证 6 个工具的 execute：
  - workspace_path 从 `execution_context.workspace_root` 正确提取并注入 params
  - 成功：`tool_success`（content 聚合文本、display_data 结构）
  - Kernel 不可用（client=None 或 query 抛 Unavailable）：`tool_error` 降级
  - **无 workspace（execution_context=None）：`tool_error` 降级，不崩溃**
  - 各工具按 name 路由到对应 vendor method（6 种参数路由）
- `tests/test_codegraph_tool_args.py`：6 个参数模型的 pydantic 校验（必填/默认/类型）
- 注册测试：`build_tool_system(client=fake)` 后 registry 含 6 个 codegraph 工具；`build_tool_system(None)` 工具仍注册、execute 降级
- **profile 测试：`agent_profile.select_tools()` 后 `model_tools` 含 6 个 codegraph 工具**（否则模型看不到，验收第 2 条静默失败）

## 七、验收清单

1. `build_tool_system(client)` 注册 6 个 codegraph 查询工具；`build_tool_system(None)` 工具仍注册不抛。
2. **`agent_profile.select_tools()` 后 `model_tools` 包含 6 个 codegraph 工具**（`DEFAULT_DEVELOPER_TOOLS` 已追加）。
3. agent 在 turn 里能调 `codegraph_explore` 等，返回 vendor 文本结果。
4. workspace_path 从 `execution_context.workspace_root` 注入，跨 workspace 隔离正确。
5. Kernel 不可用 / 无 workspace：工具降级 `tool_error`，不崩溃，引导用文件搜索。
6. workspace 未索引：透传 vendor 的「未索引」引导文本。
7. 独立审查 + 独立测试闭环通过。

## 八、明确不做

- **不做 `codegraph_files`**（与 list_directory/search_files 重叠，工具面控制）。
- **不做生命周期工具**（status/init/sync 由后端自动管理，不暴露 agent）。
- **不重新定义结构化响应**（第一版透传 MCP 文本，讨论文档 511 行确认）。
- **不触发隐式 sync**（agent 查询不触发写入型 sync；索引新鲜度由 turn 前 ensure_ready 保证）。
