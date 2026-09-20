# `.cosir` 路径集中与只读保护设计

**Goal:** 把 workspace 级 `<workspace_root>/.cosir` 与系统级 `.cosir` 的路径计算收敛为**单一事实源**，并对 `.cosir` 施加「可读、禁止写/改/删/移动（终端除外）」的保护；同时把内部工具输出目录从 `.coding-agent` 并入 `.cosir`，消除两个内部目录口径不一致。

## 2026-09-20 实施修订

系统级运行数据的最终根目录为 ``<app_data_dir>/.cosir``。Tauri 在开发版和打包版均向后端
注入 ``CODING_AGENT_DATA_DIR=<app_data_dir>``；后端固定路径模块统一推导：

- ``.env`` / ``.env.local``：``.cosir/``
- 主库与 LangGraph checkpoint：``.cosir/storage/``
- JSONL、桌面、前端和 backend console 日志：``.cosir/logs/``
- bootstate 与 uv cache：``.cosir/runtime/``

工作区自身的 ``<workspace>/.cosir`` 仍独立保留。旧方案中把日志、storage、runtime 放在
系统 ``app_data_dir()`` 直属目录的描述已失效；当前实现不再读取仓库或 ``apps/backend`` 下的
运行时 ``.env``。

**Architecture:** 新增 leaf 模块 `app/utils/cosir_paths.py` 承载全部 `.cosir` 路径计算与保留子树判定；系统级 `.cosir` 的**目录名固定**为常量（`cosir_paths.COSIR_DIR_NAME`），其**数据根**为固定路径常量 `paths.DATA_DIR`（`app/config/paths.py`，由 `CODING_AGENT_DATA_DIR` 推导）。只读保护收口在 `PathResolver.resolve_within_workspace`（写/改/删/移动的唯一路径校验点），新增 `allow_reserved` 开关，默认拒绝 `.cosir` 子树，仅终端会话 cwd 豁免。

**Tech Stack:** Python 3.11+、FastAPI service 层、pytest、Ruff、mypy。

**Global Constraints:**

- 只改后端 `apps/backend`；不动前端 `apps/desktop` 与 Tauri。
- 不提交 Git commit。
- 完整类型注解 + 中文四段式 docstring（目的/参数/返回/异常副作用）；每个新函数必须有 docstring。
- 先写失败测试，再写生产代码。
- 交付走「独立审查 Agent + 独立测试 Agent」闭环；开发 Agent 不自宣完成。
- 日志使用 `app.config.logging.logger.log` 的结构化接口（`extra={"msg","data"}`），不得输出敏感信息。

---

## 现状（调研事实，2026-09-20 复核）

- **构造/解析 `.cosir` 路径的位置有 3 处，各自拼基名、无收口**：`workspace_service._init_cosir_metadata`（`<root>/.cosir`，行 135）、`attachment_service._attachment_directory`（`<root>/.cosir/Attachment`，行 75-77）、`image_utils._normalize_cosir_root`（受信前缀判定，行 90-104）。
- **`cosir` 令牌**（`cosir-attachment://`、`cosir-file:`、`cosir_image_ref`）只是附件命名空间，不构造目录。承载它们的模块（`conversation_run_extra`、`run_event`、`transport_assistant_service`、`conversation_state_snapshot`、`assistant_image_part`、`runtime_context_manager`、`vision_input`、`conversation_run_service`）**均已复核仍存在**，不在本次改造范围。
- **只读缺口（已核实）**：文件工具全部走 `PathResolver.resolve_within_workspace`（`security/path_resolver.py:128`），它只校验 workspace containment + 拦截 OS 设备路径，**不拦截 `.cosir`**；系统提示词（`system_prompt_builder.py:131`）仅「劝告」模型不要碰 `.cosir`，无强制。
- **`resolve_within_workspace` 的完整调用面（逐点复核）**：`write_file.py:119`、`replace_tool.py:150`、`move_tool.py:164,167`、`delete_tool.py:99`、`patch_write/patch_apply.py:31,168`、`apply_patch_tool.py:171`（写后语法检查）、`file_operation_paths.py:66`（delete/move 共用入口）、`guard/tool_output_budget.py:128`、`guard/file_resource_paths.py:274`、`service/terminal/terminal_session_service.py:780`（`_resolve_cwd`）。
- **只读工具**（`read_file.py:174`、`list_directory.py:125`、`search_content.py:71`、`find_files.py:63`、`file_resource_paths.py:351`）走 `resolve_without_boundary`，不强制 containment，不受本次保护影响。
- **第二个内部目录**：工具输出落盘写 `{workspace_root}/.coding-agent/tool-artifacts/<uuid>.txt`（`tool_output_budget.py:125`）；`.coding-agent` 被 `file_walker.IGNORED_DIRS`（行 19-40）与 `system_prompt_builder._IGNORED_DIRS`（行 38-55）隐藏，而 `.cosir` 两处都不在跳过集（口径不一致）。
- **前端不引用 `.cosir` / `.coding-agent` 目录**：前端只出现 CSS 类名（`cosir-inline-file-token`）、Tauri identifier（`com.cosir.desktop`）与环境变量名，无目录路径。全部后端侧。

## 代码基线变更（2026-09-20 复核结论）

复核期发现代码库发生过多次重构（均已提交、工作区干净），逐项核对对本设计的影响：

| 变更 | 对本设计的影响 |
| --- | --- |
| 移除变更集与文件快照功能（`TaskChangeSetService` / `FileMutationService` / `changes_api` / before-image / `reconcile_*` 全数移除） | **有影响**：`guard/file_resource_paths` 职责收窄，不再服务变更集；新增 `guard/file_state/`（`file_path_lock_registry` 路径锁、`file_revision_registry` 内存 revision、`repeated_call_registry` 重复调用检测）。决策 3 的「保持默认拒绝」理由需改写（见下）。 |
| 移除 SQLite 日志，改为固定 JSONL 文件；桌面日志目录统一到 `app_data_dir()/logs/` | **有影响**：`Settings` 删除 `LOG_DATABASE_FILE`/`SQLITE_LOGGING_ENABLED`/`LOG_QUERY_LIMIT_MAX`/`LOG_QUEUE_SIZE`/`LOG_BATCH_SIZE`/`LOG_FLUSH_INTERVAL_MS`；`app.py` lifespan 改为 `install_logging_for_current_process(log_dir=...)` + `shutdown_logging()`；`logs_api` 路由移除。仅影响「系统 `.cosir` 启动创建」的插入点描述（见决策 5），不影响数据根推导。 |
| 移除工具层 `artifact_data` 构造 | **无影响**：`ToolObservation.artifact_data` 字段与 `tool_output_budget` 的填充仍在位，`.coding-agent → .cosir` 迁移照旧。 |
| workflows 节点包迁移 `core/workflows/react/nodes`；移除 CodeGraph；移除 `trace_infra`；`ids` 模块迁移 | **无影响**：本设计不涉及这些模块。 |

**核心假设复核结论**：`.cosir` 三处落盘点、`PathResolver.resolve_within_workspace` 单一收口点、五个文件工具的调用面、只读工具的 `resolve_without_boundary` 用法、`.coding-agent` 的写入点与跳过集 —— **全部未变，设计成立**。

---

## 设计决策

### 1. 单一路径事实源：`app/utils/cosir_paths.py`

leaf 层纯路径计算模块，零业务依赖。**唯一**允许拼 `.cosir` 基名与子目录的位置。

公开接口：

| 符号 | 语义 |
| --- | --- |
| `COSIR_DIR_NAME = ".cosir"` | 内部目录基名（唯一常量） |
| `COSIR_ATTACHMENT_DIR_NAME = "Attachment"` | 附件子目录名 |
| `COSIR_TOOL_ARTIFACT_DIR_NAME = "tool-artifacts"` | 工具输出 artifact 子目录名 |
| `workspace_cosir_dir(workspace_root) -> Path` | `<root>/.cosir` |
| `workspace_attachment_dir(workspace_root) -> Path` | `<root>/.cosir/Attachment` |
| `workspace_attachment_staging_dir(workspace_root) -> Path` | `<root>/.cosir/Attachment/.uploading` |
| `workspace_tool_artifact_dir(workspace_root) -> Path` | `<root>/.cosir/tool-artifacts` |
| `is_within_cosir(path, workspace_root) -> bool` | 保留子树判定（realpath+normcase，按路径分量比较，抗符号链接/大小写/`.cosir2` 误匹配） |
| `system_cosir_dir() -> Path` | `<paths.DATA_DIR>/.cosir`（目录名固定为 `COSIR_DIR_NAME`；顶层 import `app.config.paths`，无 import 期副作用） |

**落点理由**：`app/utils` 是 leaf 层，可被 `service` / `core/tools` / `core/context` 复用；已复核 `app/utils` 与 `app/config` 之间当前**仍无任何依赖边**（0 匹配），故用函数内延迟 import 收口系统级路径而不引入顶层耦合。

### 2. 系统级 `.cosir`：目录名固定，只有数据根来自配置

**目录名 `.cosir` 是固定常量**（`cosir_paths.COSIR_DIR_NAME`），不做配置项。只有**数据根**来自进程配置。

数据根现由固定路径模块 `app/config/paths.py` 产出（原方案「`Settings` 新增 `DATA_DIR` 类属性」已被下方「修订（2026-09-20）」替代，**勿再按原方案实现**）：

```python
DATA_DIR = _env_path("CODING_AGENT_DATA_DIR") or repository_root()
```

`system_cosir_dir()` = `paths.DATA_DIR / COSIR_DIR_NAME`，即 `<数据根>/.cosir`。**路径模块中不出现 `.cosir` 字面量**，避免目录名在 paths 与 cosir_paths 两处漂移。

**复核新增事实（重要）**：`CODING_AGENT_DATA_DIR` 由 Tauri **仅在打包版（FrozenExecutable）注入**——`backend_supervisor.rs:184-186` 为 `uses_packaged_data_dir().then_some(app_data_dir)`，而 `uses_packaged_data_dir()` 仅对 `FrozenExecutable` 为真；`CODING_AGENT_LOG_DIR` 则**恒**为 `app_data_dir()/logs`。因此：

- 打包版：数据根 = `app_data_dir()`，系统 `.cosir` = `app_data_dir()/.cosir`（Windows 为 `%LOCALAPPDATA%\com.cosir.desktop\.cosir`）。
- 开发态（含桌面 dev 与 `uv run -m app`）：数据根回落仓库根，系统 `.cosir` = `<repo>/.cosir`，与 `storage/` 同源。

> **修订（2026-09-20 晚，以代码为准）**：上面这段已被 `44b19a0c chore(cosir): 统一并保护系统级和工作区级 .cosir 路径策略` 推翻——`backend_supervisor.rs` 把 `data_dir: backend_runtime.uses_packaged_data_dir().then_some(app_data_dir.as_path())` 改为**无条件** `data_dir: Some(app_data_dir.as_path())`。因此**经桌面宿主启动（含 `tauri dev`）时数据根恒为 `app_data_dir()`（Windows 实测 `%APPDATA%\com.cosir.desktop`，Roaming）**；只有绕过 Tauri 直跑后端（`uv run -m app` / pytest）才回落仓库根。相应地，桌面态下 `<repo>/.cosir/.env` 与 `apps/backend/.env` 均**不被读取**（`env_files()` 只认 `<DATA_DIR>/.cosir/.env` 与 `.env.local`）。

- `DATA_DIR` 可经 `paths.override(DATA_DIR=...)` 供测试隔离（用例收尾必须 `paths.reset()` 还原）。
- **已确认（2026-09-20）**：采用本锚点。开发态落仓库根属接受范围；「仓库本身被当作 workspace 打开」时系统 `.cosir` 与 workspace `.cosir` 重合，一并接受（见「风险与取舍」）。
- **禁止**把系统 `.cosir` 改成相对 `Path(".cosir")`：后端进程 cwd 是 `apps/backend`（Tauri `current_dir(backend_dir)`），会落到 `apps/backend/.cosir`，打包版甚至可能落入只读资源目录导致创建失败。

**修订（2026-09-20）**：`DATA_DIR` / `LOG_DIR` / `DATABASE_FILE` / `CHECKPOINT_FILE` 已从 `Settings` 删除，改由**固定路径模块 `app/config/paths.py`** 承载模块级常量（`DATA_DIR = CODING_AGENT_DATA_DIR or 仓库根`；`LOG_DIR = CODING_AGENT_LOG_DIR or <DATA_DIR>/logs`；`DATABASE_FILE` / `CHECKPOINT_FILE` = `<DATA_DIR>/storage/…`，不再有独立 env）。上文 §2「`Settings` 新增 `DATA_DIR`」的方案已被替代：`system_cosir_dir()` 顶层 import `paths` 读 `paths.DATA_DIR`；数据根推导与回落仓库根规则、系统 `.cosir` 落点均不变。`Settings.load()` 会调 `paths.reset()` 按 `.env`+环境重算；测试隔离改用 `paths.override(...)` / `paths.reset()`。

### 3. 只读保护收口：`PathResolver.resolve_within_workspace`

```python
def resolve_within_workspace(self, path: str, *, allow_reserved: bool = False) -> tuple[Path | None, str]:
    ...
```

- 解析成功后，若 `not allow_reserved and is_within_cosir(resolved, self.workspace_root)` → 返回 `(None, reason)`。
- `reason` 为面向模型的英文短句：`.cosir` 是只读保留区，写/改/删/移动必须指向 `.cosir` 之外。
- 默认拒绝 ⇒ 所有写/改/删/移动工具（含经 `resolve_workspace_relative_path` 的 delete/move、经 `patch_apply` 的 apply_patch）**零调用点改动**即获得保护。
- **豁免点（显式传 `allow_reserved=True`）只有两处**：`terminal_session_service._resolve_cwd`（对齐用户「终端除外」——原始 shell 通道无法可靠拦截，故不拦截）与 `guard/tool_output_budget._write_artifact`（内部子系统写 `.cosir/tool-artifacts`，见决策 4）。
- **保持默认拒绝**：`guard/file_resource_paths._resolve_workspace_path`（对写路径做**调度期**解析，供 `guard/file_state/` 的路径锁、revision 登记与重复调用检测使用，**已不再服务变更集**）—— 让 `.cosir` 在调度期即被拦截，且不进入文件锁 / revision 登记。
- **天然不受影响**：只读工具（`resolve_without_boundary`）；`attachment_service` 自身写 `.cosir` 不经 `PathResolver`。
- **无新增 `blocked_device_reason` 分支**：`.cosir` 保护是「保留子树」语义，不属设备/伪文件规则，保持两类规则单一职责。

### 4. 内部目录统一：`.coding-agent` → `.cosir`

- `guard/tool_output_budget._write_artifact`：落盘相对路径由 `.coding-agent/tool-artifacts/<uuid>.txt` 改为 `cosir_paths.workspace_tool_artifact_dir` 派生的 `.cosir/tool-artifacts/<uuid>.txt`；该写入属内部子系统，需显式传 `allow_reserved=True`。
- `file_walker.IGNORED_DIRS` 与 `system_prompt_builder._IGNORED_DIRS` 移除 `.coding-agent`（该目录不再创建）。
- `.cosir` **不加入**搜索/提示词跳过集（用户要求「允许读」）；递归搜索会遍历 `.cosir`，属已确认取舍。

### 5. 系统 `.cosir` 启动创建

后端 lifespan（`app/app.py::_lifespan_impl`）在**第二次 `install_logging_for_current_process(...)` 之后**幂等创建 `<paths.DATA_DIR>/.cosir`；失败降级（记 error 日志）不阻断启动。与 workspace 级 `.cosir` 创建（`workspace_service._init_cosir_metadata`，失败降级）保持同一降级策略。

> 复核说明：lifespan 已从「`Settings.load` → `initialize_service_dependencies` → 单次 `install_logging_for_current_process(log_dir, log_database_file, sqlite_logging_enabled, ...)`」改为「先按 env 建 JSONL 管线 → `Settings.load` → `initialize_service_dependencies` → 按最终配置重建管线 → `shutdown_logging()`」。插入点取「重建管线之后」，保证降级 error 日志已可用。

### 6. 提示词文案

`.cosir/` 文案由 "holds runtime metadata: do not read or modify it" 改为只读语义：可读，禁止写/改/删。

---

## 影响文件清单

新增：

- `apps/backend/app/utils/cosir_paths.py`
- `apps/backend/tests/test_cosir_paths.py`
- `apps/backend/tests/test_path_resolver_cosir_guard.py`

修改（路径均已复核存在）：

- `apps/backend/app/config/settings.py`（新增通用 `DATA_DIR`；`data_root` 在 `load()` 内）
- `apps/backend/app/service/task/workspace_service.py`（`_init_cosir_metadata` 改用模块）
- `apps/backend/app/service/attachment/attachment_service.py`（`_attachment_directory` 改用模块）
- `apps/backend/app/utils/image_utils.py`（`_normalize_cosir_root` 改用模块）
- `apps/backend/app/core/tools/tool_handler/security/path_resolver.py`（`allow_reserved` + `.cosir` 保护）
- `apps/backend/app/service/terminal/terminal_session_service.py`（`_resolve_cwd` 传 `allow_reserved=True`）
- `apps/backend/app/core/tools/guard/tool_output_budget.py`（artifact 落盘改 `.cosir`，写入传 `allow_reserved=True`）
- `apps/backend/app/core/tools/tool_handler/search/file_walker.py`（移除 `.coding-agent`）
- `apps/backend/app/core/context/system_prompt_builder.py`（移除 `.coding-agent`、改 `.cosir` 文案）
- `apps/backend/app/app.py`（lifespan 创建系统 `.cosir`）
- `.gitignore`（`.coding-agent/` → `.cosir/`；开发态系统 `.cosir` 落在仓库根，必须忽略）

不改动（明确排除）：

- `cosir` 令牌相关模块（见「现状」第二条）—— 只引用令牌，不构造目录。
- `guard/file_state/`（`file_path_lock_registry` / `file_revision_registry` / `repeated_call_registry`）—— 进程内内存状态，不写 workspace 文件，与 `.cosir` 无关。
- 前端与 Tauri。
- `.cosir/trash`（历史方案未落地，`guard/trash_staging.py` 仍为空文件，本次不做）。

---

## 测试要点

| 用例 | 断言 |
| --- | --- |
| `cosir_paths` 纯函数 | 各路径拼接正确；`is_within_cosir` 对 `<root>/.cosir/x`、`<root>/.cosir` 判 True，对 `<root>/.cosir2/x`、`<root>/x/.cosir` 判 False；符号链接指向 `.cosir` 判 True |
| `resolve_within_workspace` 保护 | 写路径落在 `.cosir` → `(None, reason)`；`allow_reserved=True` → 通过；`.cosir` 外路径 → 通过 |
| 五个文件工具 | `write_file`/`patch_write`/`apply_patch`/`delete_file`/`move_file` 指向 `.cosir` 均返回 error 且**磁盘未被修改** |
| 只读工具 | `read_file`/`list_directory`/`search_content`/`find_files` 读 `.cosir` 仍成功 |
| 终端豁免 | `_resolve_cwd` 落在 `.cosir` 不抛错 |
| 调度期资源路径 | `file_resource_paths` 指向 `.cosir` 仍拒绝（路径锁 / revision 登记不接收 `.cosir`） |
| artifact 落盘 | `tool_output_budget` 写 `.cosir/tool-artifacts/`，返回的 workspace 相对路径前缀正确 |
| 系统 `.cosir` | `Settings.load` 后 `DATA_DIR == data_root 且 system_cosir_dir() == data_root/".cosir"`；启动创建幂等、失败降级 |
| 既有回归 | `test_workspace_service_create_reuse.py`、`test_attachment_dedup.py`、`test_image_normalizer.py`、`test_terminal_output_streaming.py`（artifact 断言不硬编码目录名，迁移后仍应通过） |

---

## 风险与取舍

- **开发态系统 `.cosir` 落在仓库根**：`CODING_AGENT_DATA_DIR` 仅在打包版注入，故开发态（含桌面 dev）数据根 == `<repo>`、系统 `.cosir` == `<repo>/.cosir`。`.gitignore` 现仅忽略 `.coding-agent/`，需同步改为忽略 `.cosir/`，否则仓库根 `.cosir` 会污染 `git status`（`.gitignore` 已纳入影响文件清单）。
- **系统 `.cosir` 与 workspace `.cosir` 可能重合**：当被打开的 workspace 恰好是 `data_root`（开发态即仓库根）时，两级 `.cosir` 指向同一目录。后果：工作区附件与系统配置混放；只读保护会把该目录一并保护（不致误写）。属边界场景，**已确认接受（2026-09-20）**。
- **递归搜索会遍历 `.cosir`**（含 `tool-artifacts` 与 `Attachment`）：用户明确选择「可读」，属已确认取舍。
- **`.coding-agent` 迁移**：历史 workspace 若残留 `.coding-agent`，不再被跳过集隐藏；绿地项目不需要兼容，接受。
