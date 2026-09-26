# 子 Agent JSON 配置化技术方案

## 1. 目标与范围

本方案将**子 Agent**（`AgentProfileType.CHILD`）的 profile 与系统提示词从 Python 构造代码和仓库内提示词文件迁移到本机 JSON 配置。系统级配置对所有 workspace 可见；workspace 级配置只对其所属 workspace 可见。

主 Agent（`main_agent`）、隐藏 Agent、一个代码内置的通用子 Agent、workflow 实现、工具实现和 Run 生命周期不在本次配置化范围内。通用子 Agent 是唯一不进入 JSON 的 CHILD profile，由 `define_agents.py` 构造并对所有 workspace 可用；其能力和系统提示词继续由代码/现有提示词文件维护。除此例外，其他可委派子 Agent 通过系统级或 workspace 级 JSON 配置。Assistant Transport wire schema 与业务协议保持不变；仅调整快照重建时的子 Agent role 来源，使其遵循 workspace 作用域。配置界面留待后续阶段，本方案不新增前端界面、HTTP 配置 API 或数据库表。

## 2. 运行拓扑与数据位置

- 配置加载、profile 构建和 Agent 执行均在现有 Python/FastAPI 后端子进程内完成，不新建进程或服务。
- 系统级配置位于 `<SYSTEM_COSIR_DIR>/agents/`。`SYSTEM_COSIR_DIR` 本身已经是 `<DATA_DIR>/.cosir`；桌面模式的 `DATA_DIR` 由 Tauri 注入的 `CODING_AGENT_DATA_DIR` 推导，绕过 Tauri 直跑后端时沿用仓库根回退规则。
- workspace 级配置位于 `<workspace_root>/.cosir/agents/`。
- JSON 文件是 Agent 定义的配置事实源；SQLite 仍保存现有 Task、Run 等业务事实，不保存配置副本。
- Tauri 仍是后端进程生命周期的唯一所有者：显示 WebView 后后台启动后端，退出时清理进程树；后端崩溃时由现有有限、串行恢复策略处理。配置化不改变启动、停止、readiness 或恢复契约。

系统 Agent 配置目录与系统 `.cosir/AGENTS.md` 是两种不同用途：前者定义可委派的 Agent，后者继续作为所有 Agent 都可读取的全局指令层。

### 默认配置初始化

配置型内置子 Agent 的默认 JSON（不含代码内通用子 Agent）随后端程序打包在只读资源目录（建议 `apps/backend/app/core/agents/defaults/`），初始化时复制到 `<SYSTEM_COSIR_DIR>/agents/`。资源文件必须随桌面构建产物一起分发。用该目录中的初始化标记区分“默认文件尚未完成初始化”和“用户已完成配置”：标记缺失时补入缺少的默认文件但不覆盖已有文件，先复制到临时文件并原子替换，全部复制成功后才写标记；中断后可重试，用户以后删除默认 Agent 也不会被重新创建。初始化失败（包括无法创建目录或写标记）应阻止 backend ready 并记录路径和错误。资源目录只用于首次默认配置导入，运行时不作为第三种配置作用域。未来新增默认 Agent 的升级迁移策略需另行设计，首版不静默覆盖或重置用户配置。

## 3. 文件布局与 JSON 契约

每个 JSON 文件定义一个子 Agent，文件名建议使用稳定的 `agent_id`，如 `code-explorer.json`。加载器以 `*.json` 为唯一候选，不递归扫描子目录。

目录示例：

```text
<SYSTEM_COSIR_DIR>/agents/
  code-explorer.json
  delegate_reviewer.json
<workspace_root>/.cosir/agents/
  api-specialist.json
```

建议的首版字段：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `agent_id` | string | 必填、非空；作为委派参数、Run 绑定和目录解析的稳定标识 |
| `role` | string | 必填、非空；用于 Agent 运行时身份描述 |
| `description` | string | 必填；面向父 Agent 的选择说明，不代替执行提示词 |
| `system_prompt` | string | 必填；子 Agent 的执行协议，完整文本保存在 JSON 中 |
| `allowed_tools` | string[] | 必填；元素必须是已注册的工具名 |
| `max_steps` | positive integer | 可选；缺省沿用 `AgentProfile` 现有缺省值 |
| `provider_id` | integer \| null | 可选；缺省时沿用父 Run 的 provider 路由 |
| `model_name` | string \| null | 可选；缺省时沿用父 Run 的模型路由 |
| `model_settings` | object | 可选；仅接受 `ModelSettings` 已声明的覆盖字段 |

示例：

```json
{
  "agent_id": "code-explorer",
  "role": "code-explorer",
  "description": "只读探索代码结构、调用链、数据流和配置入口。",
  "system_prompt": "你负责基于本地代码回答问题。先定位入口和调用链，再给出证据与文件位置。不得修改文件或运行命令。",
  "allowed_tools": [
    "read_file",
    "list_directory",
    "search_content",
    "find_files"
  ],
  "max_steps": 100,
  "model_name": null,
  "provider_id": null,
  "model_settings": {}
}
```

`agent_type` 在装配时固定为 `CHILD`，不得由文件选择为 `MAIN` 或 `HIDDEN`。`workflow` 固定使用现有默认 workflow；`run` 等运行时字段不属于 JSON 契约。系统提示词作为字符串载入 profile，不通过 JSON 中的路径引用外部文件，从而避免路径逃逸、额外文件依赖和提示词来源分散。

`allowed_tools` 表达该 profile 的工具能力；现有 child Run 禁用委派/父子通信和交互终端工具的 `CHILD_BANNED_TOOLS` 规则继续生效。配置不能取消该规则。加载时依据 `tool_names.py` 中的静态规范工具名清单拒绝未知名称；是否因运行期装配或 provider 状态实际可用，仍由当前 ToolRegistry 和 `AgentProfile.select_tools()` 决定，避免配置加载依赖 ToolSystem 的启动顺序。

## 4. 作用域解析与冲突策略

子 Agent 配置由单一进程内 `AgentProfileRegistry` 持有，按 `(workspace, agent_id)` 索引；系统作用域固定为 `system`，workspace 作用域使用规范化后的 workspace 根路径。可见子 Agent 为：

```text
有效子 Agent = 代码内置通用子 Agent + 系统级 JSON 配置 + 当前 workspace JSON 配置
```

代码内置通用子 Agent 和系统级 JSON profile 对所有 workspace 可见；workspace JSON profile 只在其所属 workspace 可见。后端启动时由 `AgentProfileRegistry` 一次读取系统目录和数据库中已登记的全部 workspace 配置，并保存在同一个内存索引中。运行期的 `resolve`、列表、候选摘要与授权检查只查内存，不再逐 Run 读取文件。workspace 配置无效时，Registry 标记该 workspace 无效；主 Agent 仍可运行其他工具，但该 workspace 的 `delegate_task` 不可用，子 Agent profile 解析也失败。候选清单、参数 schema、实际委派解析和 child Run profile 解析都传入相同 workspace 作用域。

若 JSON 配置与代码内置通用子 Agent 出现相同 `agent_id`，或同一作用域的多个文件声明相同 `agent_id`，或系统级与 workspace 级出现相同 `agent_id`，均将该 workspace 的有效配置目录判定为冲突并明确报错；不采用依赖文件遍历顺序的覆盖或隐式优先级。要求通用 Agent 与全局、workspace JSON 的有效组合内 ID 唯一，避免配置无提示地改变内置 Agent 的提示词或工具权限。文件名与 `agent_id` 不一致时也作为配置错误处理，便于用户定位。

通用 Agent 在进程启动时由 `define_agents.py` 构造并注册；系统级默认 JSON 初始化后，系统和 workspace JSON 在同一启动阶段装入 Registry。系统级配置错误阻止启动并提供文件路径和字段错误；workspace 配置错误只禁用对应 workspace 的委派并记录错误。配置有效但没有 workspace 专属子 Agent 时，仍可委派给代码内通用 Agent 和系统级 Agent。workspace 配置目录或其中的文件缺失表示该作用域没有额外配置，不是错误。当前阶段进程启动后不监视文件变化，也不实现 `reload()`；配置文件变更需重启后端才生效，后续配置管理功能再显式调用 Registry 刷新。

Run 仍为当前 Agent 派生独立 profile 副本，避免并发运行态互相覆盖。当前 Registry 在进程启动后保持只读，因此同一进程的候选 schema 与执行期授权查询一致；未来实现 `reload()` 时，必须定义运行中 Run 使用配置版本的规则，确保 schema 与授权不会跨版本。

## 5. Agent 系统提示词装配

`AgentProfile` 统一使用必填字符串字段 `system_prompt` 保存完整提示词正文，不保存提示词路径。配置型 CHILD 从 JSON 读取正文；主 Agent 和代码内通用 CHILD 在 profile 构造时读取随应用分发的 Markdown 正文并写入同一字段。内置资源缺失、不可读、编码无效或为空时，profile 装配失败并阻止 backend ready，避免 Agent 在缺少系统预设时运行。

`SystemPromptBuilder` 不再读取 Agent 提示词文件，只消费 `system_prompt` 并应用现有字节和 token 预算限制。超限时继续截断并记录 Agent ID，不记录提示词正文。默认 Markdown 提示词仍作为内置源码资源随桌面应用打包，但其路径只存在于 profile 装配代码中。

系统级全局指令层和 workspace 项目指令层仍按现有方式叠加到所有 Agent 提示词中。因此，JSON 中的 `system_prompt` 只描述配置型子 Agent 专属执行协议；跨 Agent 通用规则仍放在全局或 workspace 指令，不复制到每个配置文件。主 Agent 与通用子 Agent 的提示词源码仍分别保存在现有 Markdown 资源中，但运行期 profile 统一持有正文。

子 Agent 目录不进入 `delegate_task` 的工具描述，改由**工具能力目录层**（`<tool_layer>`）下发：仅当 `AgentProfile.allowed_tools` 包含 `delegate_task` 时，拼接进程级 `AgentProfileRegistry.child_agent_summary(workspace_root)`；委派工具不可用（子 Agent 已由 `ban_tools` 收窄、该 workspace 无 CHILD 候选）时整层不出现。该层只消费传入的工具名集合，不自行推导工具可用性；提示词在建 `RuntimeContextManager` 时构建一次（跨 Run 复用沿用构造时快照）。

JSON 空提示词是无效配置；内置 Markdown 资源为空也视为 profile 装配错误。超出既有预算时沿用预算截断规则，并记录 profile ID 与截断情况，不记录提示词正文。

## 6. 委派候选与后端强制校验

现有 `delegate_task` 候选 ID 来自进程级 registry，schema 在工具定义构造时生成。配置按 workspace 隔离后，不能继续使用启动时的一份全局 ID 列表。

`delegate_task` 的工具定义不再按 workspace 做 Run 级投影（`project_delegate_task_definition` 已删除）：进程级基础定义即为模型可见契约，`child_agent_id` 既无候选 `enum` 也不列举 ID 清单，只有结构字段与长度校验。候选发现由系统提示词的工具能力目录层承担（见 §5），候选合法性由执行期解析裁决。`DelegateTaskArgs` 不读取进程级 registry、不做动态 `model_json_schema()`、不设置基于全局候选集的 `model_validator`。执行期唯一授权裁决由 `DelegateTaskTool.execute` 使用当前 workspace 作用域查询内存 Registry。

启动时 workspace 配置目录解析失败只影响该作用域：`lifespan` 记 `workspace_agent_profile_config_invalid` warning，该 workspace 的 skill/agent 候选缺失，但主 Agent 仍可完成其他工作。工具集层面唯一的 fail closed 规则是运行期拿不到进程内 Registry（`AgentRuntime._build_operations` 的 `agent_profile_registry is None`）时移除 `delegate_task`——没有目录就无法解析委派目标。JSON 目录为空不属于无候选，因为代码内通用子 Agent 在有效目录中提供至少一个候选。

模型提交的 ID 不能因 schema enum 而被信任。`DelegateTaskTool.execute` 必须从父 Run 的运行期依赖取得 Registry，并以当前 workspace 作用域再次解析目标 profile；找不到 ID 时拒绝创建 child Task/Run。child Run 随后按其 task 的 workspace 再次解析 profile。这样候选提示、服务端准入与执行解析有同一作用域，不会因模型构造请求、其他 workspace 配置或 UI 状态绕过边界。

workspace 配置有误时，当前 Run 的主 Agent 仍可运行，但工具列表不含 `delegate_task`。当前阶段配置修复后需重启后端，Registry 才会重新载入。child profile 若无法从 child Run 对应 workspace 的有效目录中解析，则该 child Run 不可执行，不得回退到其他 workspace。

所有创建 child Run 的入口都必须遵循该解析规则。除 `delegate_task` 外，`child_agent_send` 会为既有 child Task 新建 Run；它应按该 child Task 所属 workspace 的内存 Registry 作用域确认原 `agent_id` 仍存在，确认成功后才创建新 Run。配置在进程启动时缺失或无效时，返回明确工具错误且不创建 Run。实现和测试不得只覆盖首次委派路径。

## 7. 配置错误、日志与安全边界

- JSON 语法错误、缺字段、字段类型错误、未知键/工具、重复 ID、无效模型参数均报告配置文件路径和字段名；日志不输出 `system_prompt` 正文。
- 同一目录内重复 `agent_id`、文件名与 `agent_id` 不一致，以及系统/workspace 同 ID 均是配置错误；不得按遍历顺序覆盖。
- 系统级配置或首次默认文件初始化错误阻止 backend ready；workspace 配置错误只让对应 Run 缺少 `delegate_task` 工具，主 Agent 仍可运行，并通过现有后端结构化日志记录稳定事件名和安全的错误摘要。
- 只读取配置目录的直接子项 `*.json`；校验最终路径仍位于预期 agents 目录内，不跟随逃逸的符号链接。
- 本机 workspace 配置是用户控制的配置，不构成安全沙箱；配置的工具权限仍受现有工具注册、workspace 执行边界和 child 禁用清单约束。
- workspace `.cosir` 当前是文件变更工具的保留只读区。未来配置 UI 写入应由后端显式配置管理接口负责；React 不能通过普通文件工具或绕过现有 IPC/HTTP 边界直接写入该目录。本阶段不实现此接口。
- `conversation_task_state_rebuilder` 对历史委派结果的重建应优先信任已持久化 `display_data.role`；若旧数据没有 role，再按 child task 所属 workspace 解析 profile，不得从进程级 registry 跨作用域猜测。

## 8. 建议实施顺序

1. 固定 JSON schema、默认值、未知字段策略及配置错误隔离规则。
2. 在 `cosir_paths` 收口系统和 workspace agents 目录路径；实现无副作用路径函数、首次安装默认 JSON 初始化与独立 JSON loader/validator。
3. 扩展 `AgentProfile` 的提示词来源表达，使内存配置文本可被 `SystemPromptBuilder` 使用，同时保持主 Agent 旧路径。
4. 将除通用子 Agent 外的现有内置 CHILD 定义和提示词迁移到系统级 JSON；删除这些配置型 Agent 的 Python 构造入口，避免双重事实源。保留 `generic_child_agent()`、`main_agent` 与隐藏 Agent 的代码装配。
5. 在启动阶段将系统和所有已登记 workspace 的 JSON profile 一次载入 Registry；所有运行期查询传入 workspace 作用域，并贯通 `delegate_task` 的 ToolDefinition 投影、执行期目标校验和 child Run profile 解析。
6. 加入配置解析与作用域行为的自动化测试，覆盖系统/ workspace 合并、跨 workspace 隔离、冲突、无效文件、工具权限和提示词层装配。
7. 更新架构文档与配置示例；未来配置 UI 单独设计后端读写 API 和编辑冲突策略。

## 9. 验收标准

1. 代码内通用子 Agent 在所有 workspace 可用；系统级 JSON 子 Agent 也可在所有 workspace 被发现和委派。
2. workspace JSON 子 Agent 只在所属 workspace 可发现、可委派；其他 workspace 无法通过 ID 委派它。
3. 委派候选由系统提示词工具层按同一 workspace 作用域的进程内 Registry 投影；实际执行可解析 ID 也来自该作用域。JSON 配置为空时仍可委派给代码内通用子 Agent。
4. 手工构造不在当前有效目录的 `child_agent_id` 会在创建 child Task/Run 前被拒绝。
5. 所有 Agent profile 统一持有 `system_prompt` 字符串；配置型子 Agent 正文来自 JSON，主 Agent 和通用子 Agent 正文在装配时从现有 Markdown 资源载入，并与系统全局指令及 workspace 指令按既有提示词层规则组合。
6. 配置定义的工具不能超出有效注册工具集，且 child 禁用工具约束仍然生效。
7. 系统级坏配置或首次默认文件初始化失败能阻止启动并可从日志定位；workspace 坏配置不会影响其他 workspace，且只让该作用域的候选集合缺少对应 profile。
8. 快照重建保留已持久化的委派 role；旧记录缺少 role 时按 child workspace 解析，不跨 workspace 查找。
9. `child_agent_send` 仅在 child Task 所属 workspace 的 Registry 查询结果（代码内通用 Agent、系统配置和该 workspace 配置）仍包含原 Agent 时创建 follow-up Run；目标缺失或 workspace 配置无效时不创建 Run。
10. 后端重启、恢复和退出行为不变；配置不新增 SQLite 事实或独立服务。

## 10. 主要代码影响面

- `apps/backend/app/utils/cosir_paths.py`：系统/workspace Agent 配置目录路径。
- `apps/backend/app/core/agents/agent_profile.py`、`agent_profile_registry.py`：统一的 profile 提示词正文、文件校验和按 workspace 索引的进程内目录。
- `apps/backend/app/config/configuration.py`、`apps/backend/app/lifespan.py`：系统配置启动加载与装配。
- `apps/backend/app/core/context/system_prompt_builder.py`：统一消费 profile 提示词正文并构建 Agent 系统预设层；按 `AgentProfile.allowed_tools` 构建工具能力目录层（`<tool_layer>`）。
- `apps/backend/app/core/tools/tool_handler/child_task/child_agent_create.py`、`tool_models/child_task/delegate_task_args.py`、`apps/backend/app/core/runtime/runner.py`：工具基础定义与结构参数校验、进程内 Registry 不可用时 fail closed 移除委派工具、执行期授权解析和按 child Run workspace 解析 profile。
- `apps/backend/app/core/tools/tool_handler/child_task/child_agent_send.py`：创建 follow-up child Run 前按其 workspace 校验 profile 仍可解析。
- `apps/backend/app/core/tools/schemas/tool_runtime_dependencies.py`：为当前 Run 的委派执行携带共享 Registry；作用域由工具执行上下文提供。
- `apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py`：冷重建时恢复持久化 role 或按 child workspace 解析。
- `apps/backend/app/core/agents/define_agents.py` 与 `apps/backend/app/core/context/system_prompt/`：移除配置型子 Agent 的旧事实源；保留通用子 Agent、主 Agent 和隐藏 Agent 的代码及其提示词来源。

`delegate_task` 当前在 `ToolSystem.build_tool_system()` 启动装配时创建全局 `ToolDefinition`；而 `AgentRuntime._build_operations` 每个 Run 构造自己的模型工具列表，workflow 随后按该列表绑定模型工具。因此保留全局 handler/基础定义，在 Run 级列表中按 workspace 查询内存 Registry 并投影 workspace 专属副本。`DelegateTaskArgs` 的现有全局 registry validator/schema override 必须同步移除或重构，否则会与 workspace schema 冲突。
