# 文件工具职责拆分与 unified diff 改造方案

> 状态：已实施；实施边界和验收记录见本方案及相关代码测试。
>
> 范围：收窄 `ApplyPatchTool`，新增 `DeleteTool` 与 `MoveTool`，并调整相关安全协调、ChangeSet、工具描述、Agent 工具清单和 UI 展示。
>
> 明确保持不变：`WriteFileTool` 的 handler、参数和执行行为不改造；新建文件继续由 `WriteFileTool` 负责。

## 1. 目标职责

| Python 类 | 注册名 | 唯一职责 | 输入 |
| --- | --- | --- | --- |
| `WriteFileTool` | `write_file` | 用完整内容新增或覆盖单个文件；本方案不改造 | `path`、`content` |
| `ApplyPatchTool` | `apply_patch` | 对一个或多个已存在的 workspace 文本文件应用 Git 风格 unified diff | `patch` |
| `DeleteTool` | `delete_file` | 删除一个 workspace 内的文件；拒绝目录 | `path` |
| `MoveTool` | `move_file` | 将一个 workspace 内的文件移动到另一个 workspace 路径；拒绝目录 | `source_path`、`destination_path` |

`ApplyPatchTool` 不再接受新增、删除或移动操作。新增文件使用 `write_file`；文件删除和移动分别使用 `delete_file`、`move_file`。删除与移动第一阶段沿用现有 V4A 文件操作的 UTF-8 文本文件范围；若要支持二进制文件，应另行扩展其展示契约和验证范围。

## 2. Git 风格 unified diff 输入契约

每个文件段采用 Git patch 形式：

```diff
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -10,3 +10,4 @@
 context line
-old line
+new line
+another line
```

一次 `apply_patch` 可包含多个文件段。解析后的 `a/` 与 `b/` 路径必须指向同一个已存在文件；每个段必须包含标准 `---`、`+++` 和 `@@` hunks。允许标准 Git patch 的非操作性元数据（例如 `index` 行），由选定的 unified-diff parser 解析，不手写脆弱的行号或空格拆分逻辑。

只接受对已存在文本文件内容的修改。以下输入必须拒绝，不能部分应用：

- `/dev/null`、`new file mode` 或其它新增文件段；
- `deleted file mode`、`rename from/to`、copy 元数据；
- binary patch 和 combined diff；
- `a/` 与 `b/` 路径不同、路径为空、路径越界或解析出歧义的段；
- 目标文件不存在、不是 UTF-8 文本，或 hunk 无法匹配当前文件内容。

标准 unified diff 只定义差异表达和 hunk，不改变工具的执行边界。后端仍以 Python 本地 handler 校验并应用文件变化；不要求 workspace 是 Git 仓库，也不依赖调用 `git apply` 子进程。[Git diff patch 格式](https://git-scm.com/docs/git-diff) [git apply 的 unified diff 输入](https://git-scm.com/docs/git-apply)

## 3. Tool 定义、description 与参数描述

三个 handler 都必须继承 `HandlerBase`，提供独立的 `ToolDefinition` 工厂、严格参数模型和 `ToolDisplayHints`。参数模型继续使用 `strict=True` 与 `extra="forbid"`。所有 description 和 Field description 必须明确作用范围、禁止事项和下一步该用哪个工具；不能只依赖类名或模型自行推断。

### 3.1 `ApplyPatchTool`

工具名：`apply_patch`。必填参数只有 `patch: str`。

**描述分工（2026-09-21 定稿）**：工具 description 只讲工具的职责与能力边界（做什么、不能做什么、该换哪个工具、路径作用域）；diff 格式契约（结构规则、行前缀、hunk 计数规则、最小样例）的唯一事实源是 `patch` 的 Field description。两段不得互相复述对方的内容，工具 description 对格式只用一句话指向参数描述。

工具 description（职责与边界）：

> Apply a Git-style unified diff to modify the contents of existing UTF-8 text files inside the active workspace. It only changes existing files: it cannot create, delete, or move files, so use `write_file` to create or replace a whole file, `delete_file` to delete one, and `move_file` to move or rename one. The `patch` parameter holds the diff text; its description defines the accepted format.

`patch` 参数描述（格式契约，含最小样例）：

> Required Git-style unified diff text: the value must contain nothing but the diff, with no `*** Begin Patch` / `*** End Patch` markers and no prose around the hunks. It holds one or more `diff --git a/<path> b/<path>` sections and accepts content hunks only: each file section must carry exactly one `---` header, exactly one `+++`, and at least one `@@` hunk, must name the same workspace-relative path in its Git, `---`, and `+++` headers, and must change file content. Sections that carry no content hunk (mode-only, rename-only, or binary) are rejected, and combined `diff --cc` / `diff --combined` sections are not supported. Inside a hunk every line starts with ` ` (context), `-` (removed), or `+` (added), and the numbers in `@@ -start,count +start,count @@` must match that hunk body exactly: the source count is that hunk's context plus `-` lines, the target count is its context plus `+` lines, and a count may be omitted only when it is 1. A header that disagrees with its own body is rejected before any file is touched, so fix the counts instead of resending the same patch. Minimal accepted example: `diff --git a/pkg/mod.py b/pkg/mod.py` / `--- a/pkg/mod.py` / `+++ b/pkg/mod.py` / `@@ -10,3 +10,3 @@` / ` import os` / `-old_line()` / `+new_line()` / ` keep()`。

### 3.2 `DeleteTool`

工具名：`delete_file`。必填参数只有 `path: str`。

建议的工具 description：

> Delete exactly one existing UTF-8 text file inside the active workspace. Directories are not supported. The path must resolve to a file within the workspace. Use this tool for file deletion; do not encode deletion in `apply_patch`.

建议的 `path` 参数描述：

> Workspace-relative path to one existing UTF-8 text file. The resolved target must remain inside the active workspace. Directory paths and paths outside the workspace are rejected.

执行前明确验证目标存在且为文件；目录不得递归删除，也不得通过 `unlink` 或其它路径绕过目录检查。

### 3.3 `MoveTool`

工具名：`move_file`。必填参数为 `source_path: str` 与 `destination_path: str`。

建议的工具 description：

> Move one existing UTF-8 text file to a new path inside the active workspace. Directories are not supported. The source must be a file, the destination must not already exist, and both paths must resolve inside the workspace. The destination parent directory must already exist. This tool moves a file without editing its contents; use `apply_patch` for content changes.

建议的参数描述：

- `source_path`：`Workspace-relative path to one existing UTF-8 text file. The resolved target must remain inside the active workspace; directories are not supported.`
- `destination_path`：`Workspace-relative destination for the file. It must remain inside the active workspace, its parent directory must exist, and the destination must not already exist.`

## 4. Workspace 安全与执行语义

路径约束属于后端 handler 与资源协调层的共同责任，工具 description 不能代替运行时校验。

- 所有路径以本次 `ToolExecutionContext.workspace_root` 为唯一根，不依赖进程 CWD、前端传入的任意 root 或 Git 仓库状态。
- 解析 Git patch 中的 `a/`、`b/` 文件路径后，先拒绝绝对路径、空路径、NUL、`..` 越界和设备路径，再经 `PathResolver.resolve_within_workspace` 做规范化 containment 校验。
- `delete_file` 解析并锁定目标路径；`move_file` 同时解析、校验和锁定源路径与目标路径。目标目录、符号链接/reparse point 和竞态路径变化按现有 PathResolver、FileMutationGuard 的边界处理；不得因路径重新解析而绕过 containment 或快照保护。
- Delete/Move 操作只接受文件，不接受 workspace 根、目录或目录树。Move 的目标必须不存在，目标父目录必须存在；不隐式覆盖或创建目录。
- `apply_patch` 在任何写入前解析和验证全部文件段、目标路径及 hunks，再按文件依次应用。维持当前“预校验后顺序应用”的语义；这不是跨文件事务。执行中若发生部分落盘，必须返回不可原样重放的错误并依靠既有 ChangeSet 收口实际磁盘状态。
- 文件修改仍使用现有 cancellation、workspace/path 锁、stale 检查、原子写入、`before_file_delete` / `before_file_move` 守卫和 FileMutationService before-image。删除、移动、新 patch handler 都不得绕过 ToolExecutor 的公共执行管线。

## 5. 复用边界与目标数据流

```text
ApplyPatchTool
  Git unified diff parser
  → 转为内部 UPDATE hunks
  → 全量路径/hunk 预校验
  → 复用 fuzzy_match、atomic_write_text、语法检查
  → FileDiffResult(status="modified")

DeleteTool
  path containment + file-only 检查
  → before_file_delete + unlink
  → FileDiffResult(status="deleted")

MoveTool
  source/destination containment + file-only 检查
  → before_file_move + os.replace
  → FileDiffResult(status="moved", path=source, new_path=destination)

三类操作 → 既有 ToolObservation 工厂与 FileMutationService
         → display_data.kind="file-changes"
         → 既有 ToolPart → DiffTool
```

具体复用点：

- 保留 `PathResolver` 的 workspace containment、设备路径限制和规范路径处理。
- 保留 `fuzzy_match` 对 patch hunk 的内容定位能力，以及 `atomic_write_text` 的原子落盘和文本换行/BOM 处理。
- 保留 `FileDiffResult`、`build_file_change_display_data` 与 `patch_diff` 的 diff 生成和统计；执行结果应描述实际 before/after，不把输入 patch 当作最终事实。
- 保留 `tool_success`、`tool_error`、`tool_cancelled`、取消 registry、语法检查和现有 ToolExecutor/FileMutationService 流程。
- Delete/Move 新工具继续写入同一 ChangeSet operation/path-state 结构，确保已有回退与审阅服务能按 `DELETE` / `MOVE` 处理。
- `WriteFileTool` 后端 handler、参数模型和行为保持原样。它仍可返回 `status="added"` 或 `status="modified"` 的同一展示结构。

选型 unified-diff parser 时先核对 Python 生态中成熟 parser 对 Git header、多个文件段、quoted path、hunk range 校验的支持；如需依赖，应使用 parser 做语法与结构解析，继续复用本项目的 hunk 匹配、路径安全与原子写入，而不是把文件系统权限交给 parser 或外部 Git 命令。当前工具层没有发现现成 unified-diff parser 的使用点。

## 6. ChangeSet、资源锁与工具注册改造

### 工具与 Agent 注册

- `tool_system.py` 注册 `delete_file` 与 `move_file` definition，并更新注册清单说明。
- 面向代码编辑的 Agent profile 增加 `delete_file`、`move_file`；所有其它显式 allowlist 同步审查，不向只读 Agent 增权。
- 工具 schema 只公开新的独立职责；旧 `apply_patch` schema 不再向模型暴露 Add/Delete/Move。
- 更新终端危险命令拦截文案：受控删除现在引导使用 `delete_file`，而不是 `apply_patch` 的 Delete File 语法；终端原始删除仍保持拦截。

### 文件资源与 ChangeSet

- `FileResourceResolver`：`apply_patch` 只从 unified diff 中提取修改路径；`delete_file` 读取 `path`；`move_file` 同时返回 source/destination 写路径及其 workspace ancestor lock paths。
- `FileToolStateCoordinator`：ApplyPatch 使用 patch 内容语义的 stale 错误；Delete/Move 使用文件状态 stale 检查。锁必须覆盖实际读写的全部路径。
- `FileMutationService._TRACKED_TOOLS` 加入 `delete_file`、`move_file`。将 `_move_pairs` 从解析 `apply_patch` 请求改为读取 `move_file` 的 `source_path` / `destination_path`，否则移动会在 ChangeSet 中被误记为 DELETE + ADD，破坏文件身份连续性和回退语义。
- `tool_error` 的受控状态短提示增加删除、移动分类；限制仍遵守 `STATUS_HINT_MAX_LENGTH`。模型诊断与 UI 短提示继续分离。
- 更新语法诊断的工具建议：patch 修改使用 `apply_patch`；新建或整文件覆盖使用 `write_file`。删除、移动不做语法检查。

## 7. 死代码清理要求

完成调用方迁移后再删除旧分支；清理前通过代码搜索确认没有真实调用者，并删除对应测试和文档，不留下无法达到的兼容路径。

- 删除 V4A `patch_parser` 中 Add/Delete/Move 操作头、枚举成员及只服务这些输入语法的解析/校验代码；将仍需复用的 hunk 值对象或纯文本 hunk 转换逻辑保留在职责清晰的模块。
- 将 `patch_apply.py` 收窄为 UPDATE 校验与应用；移除只服务 Add/Delete/Move 的 `validate_all`、`_apply_operation` 分支及旧 patch operation 的参数/返回结构。Delete/Move 的实际文件操作归属各自 handler 或共享的窄文件操作 helper。
- 删除 V4A delete/move 调用后，移除 `FileResourceResolver`、`FileMutationService._move_pairs` 和工具模型中的旧 patch 操作解析分支；各层改为使用独立工具参数。
- 移除 `APPLY_PATCH_DESCRIPTION`、`ApplyPatchArgs`、测试、agent prompt、错误提示和架构文档中宣称 `apply_patch` 可新增/删除/移动文件的旧契约。
- 不删除仍服务于 ChangeSet 的持久化 `ADD` / `DELETE` / `MOVE` operation 类型、before-image、文件身份和回退代码。它们是审阅/回退事实，不是 V4A parser 的死代码。
- 不删除 `format_git_diff` 中仍被 WriteFile、Delete、Move 或 ChangeSet 使用的状态投影。先逐项检查 import/调用点再清理，不做目录级盲删。

## 8. UI 样式方案

继续使用 `file-changes` 与 `expand_layout="diff"`，沿用 `routeToolPart`、`DiffTool`、`react-diff-view`、`DisclosureRow` 和 `Collapsible`。无需新建 UI 工具路由，也不让 React 直接执行文件操作。assistant-ui 的自定义 Tool UI 与 disclosure 模式继续留在当前前端 renderer 边界。[Tool UI](https://assistant-ui.com/docs/tools/tool-ui) [Tool Call](https://assistant-ui.com/elements/tool-call)

### 摘要行

- `apply_patch`：显示“应用补丁”、文件数和总 `+insertions / −deletions`；多文件操作收在同一可展开行中。
- `delete_file`：显示“删除文件”、受控状态和目标文件名；不显示误导性的 `+0 −0`。
- `move_file`：显示“移动文件”、源路径 `→` 目标路径；不显示成“没有变化”。
- 成功结果默认收起；执行中、成功、失败、取消继续以 ToolObservation/Transport 状态为准。失败行只显示受控短提示，不混入成功 Diff 数据。

### 文件变更条目

| `changes[].status` | 标记与路径 | 展开内容 |
| --- | --- | --- |
| `modified` | 中性“修改”标记与文件路径；显示该文件 `+ / −` 统计 | 用 `react-diff-view` split hunks 展示修改和上下文 |
| `added` | 绿色“新增”标记与文件路径；适用于不改造的 WriteFileTool | 显示新增文本 Diff |
| `deleted` | 红色“已删除”标记与原路径 | 文本文件显示删除 Diff；无可读 patch 时显示删除摘要，不显示泛化的“Diff 数据不可用” |
| `moved` | “已移动”标记与 `source → destination` | 展示移动说明/rename 元数据，不构造逐行 diff |

状态必须同时用文字和图标表达，不能只用颜色。路径长时截断显示，完整相对路径保留在可访问名称或 title 中。Disclosure 继续支持键盘打开/关闭，不默认自动展开大型 patch；扩展面板只显示后端 `display_data`，不从模型正文或工具参数反推文件状态。

### 展示数据调整

保持 `display_data.kind="file-changes"`，每条变更仍以 `path`、`new_path`、`status`、`patch`、行数为核心。后端 status 是 UI 选择文件级样式的唯一依据。

- `modified` 与 `added` 继续携带可解析 Git-style patch。
- `moved` 的 patch 使用已有 `rename from` / `rename to` 元数据；前端根据 `status="moved"` 渲染路径箭头和移动状态，不把 rename 当成空内容错误。
- `deleted` 携带后端由删除前文本生成的 Git-style 删除 patch。
- 顶层统计保留实际文件数和文本增删行数；移动只计文件数，行数为零。DeleteTool 摘要独立表达删除事实。

需同步更新 `tool_ui_display_contract.md`：工具列表、失败提示、文件操作类别以及 `file-changes` 的 UI 语义；`ToolPart` 仍通过 `kind` / `expand_layout` 路由，不按工具名增加专用分支。

## 9. 实施顺序

1. 定义 Git unified diff 的受支持子集、quoted path 处理和 parser 依赖；先补 parser 契约测试。
2. 将 ApplyPatch 的内部输入收窄为 UPDATE hunks，保持现有预校验、hunk 匹配、原子写入、语法检查、差异生成和部分落盘错误处理。
3. 新增 `DeleteTool` / `MoveTool`、各自参数模型和 description；接入 workspace 安全、变更守卫、取消、资源锁和 FileMutationService。
4. 注册工具并更新 Agent allowlist、终端拒绝提示、受控错误短提示和所有旧 V4A 操作描述。
5. 改造共享 DiffTool 的摘要与 per-file renderer，实现上述四种状态视觉样式；WriteFileTool handler 不变。
6. 删除确认无人使用的 V4A Add/Delete/Move parser/application 分支及关联测试；同步工具契约与本设计文档。
7. 运行针对性 Python 单测、前端 Vitest、类型检查和 lint，再审查所有新工具是否都经过同一个 ToolExecutor 与 ChangeSet 生命周期。

## 10. 验收标准

### ApplyPatch

- 接受一个或多个 Git-style unified diff 文件段，只修改已存在的 workspace UTF-8 文本文件。
- 拒绝 Add/Delete/Move/binary/combined diff、路径不匹配、路径越界、文件不存在、非 UTF-8 内容和无法匹配的 hunks。
- 任一格式、路径或 hunk 预校验失败时不改动任何文件；执行阶段部分落盘时状态不可被报告成完整成功，并且不得提示原样重放。
- Diff UI 使用真实落盘 before/after 生成的 Git patch；模型输出预算不能截断该 patch。

### Delete 与 Move

- workspace 内文件可删除、移动；目录、workspace root、越界路径、blocked device 均被拒绝。
- Move 锁定源与目标，拒绝已有目标，成功 ChangeSet 保留 `MOVE` 和文件身份，回退后内容与路径都恢复。
- Delete 成功后 ChangeSet 可恢复被删文本文件；目录删除路径永远不可达。

### UI 与系统集成

- ApplyPatch、Delete、Move 都由 `DiffTool` 按 `file-changes` 渲染，Move 显示 `source → destination`，删除有明确的删除标记。
- 用户可通过键盘展开/收起；失败状态不显示成功结果或原始堆栈。
- 新工具在 ToolSystem 与授权 Agent schema 中可见；只读 Agent 不获得写权限；终端原始删除命令仍被拦截并引导到 `delete_file`。
- WriteFileTool handler、参数和行为无改动，仍可新增/覆盖文件并继续显示统一文件变更 UI。
- 旧 ChangeSet JSON 与回退记录继续可读；数据库事实仍由 FileMutationService/ChangeSet 拥有，Assistant UI snapshot 不变成第二事实源。

## 11. 主要代码触点

- 当前工具实现与注册：`apps/backend/app/core/tools/tool_handler/apply_patch_tool.py`、`write_file.py`、`tool_system.py`。
- 旧 patch grammar/application：`apps/backend/app/core/tools/tool_handler/patch_write/patch_parser.py`、`patch_apply.py`、`patch_diff.py`、`fuzzy_match.py`。
- workspace 安全与并发协调：`apps/backend/app/core/tools/tool_handler/security/path_resolver.py`、`apps/backend/app/core/tools/guard/file_resource_paths.py`、`file_tool_state_coordinator.py`、`file_mutation_guard.py`。
- 文件审阅与回退：`apps/backend/app/service/task/change_set/file_mutation_service.py`、`task_change_set_service.py`。
- 工具错误和安全提示：`apps/backend/app/core/tools/tool_execute/tool_error.py`、`apps/backend/app/core/tools/tool_handler/terminal/dangerous_command.py`。
- Agent 授权与测试：`apps/backend/app/core/agents/define_agents.py`、`apps/backend/tests/test_apply_patch_tool_*.py`、`test_task_change_set_service.py` 与文件资源协调测试。
- UI 与展示契约：`apps/desktop/components/assistant-ui/tools/tool-part.tsx`、`diff-tool.tsx`、`diff-tool.test.tsx`，`apps/backend/app/core/tools/tool_ui_display_contract.md`。
