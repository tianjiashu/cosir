# `.cosir` 路径集中与只读保护设计

**Goal:** 把 workspace 级 `<workspace_root>/.cosir` 与系统级 `.cosir` 的路径计算收敛为**单一事实源**，并对 `.cosir` 施加「可读、禁止写/改/删/移动（终端除外）」的保护；同时把内部工具输出目录从 `.coding-agent` 并入 `.cosir`，消除两个内部目录口径不一致。

**Architecture:** 新增 leaf 模块 `app/utils/cosir_paths.py` 承载全部 `.cosir` 路径计算与保留子树判定；系统级 `.cosir` 落点作为进程级配置新增到 `Settings.SYSTEM_COSIR_DIR`（`load()` 中由 `CODING_AGENT_DATA_DIR` 推导）。只读保护收口在 `PathResolver.resolve_within_workspace`（写/改/删/移动的唯一路径校验点），新增 `allow_reserved` 开关，默认拒绝 `.cosir` 子树，仅终端会话 cwd 豁免。

**Tech Stack:** Python 3.11+、FastAPI service 层、pytest、Ruff、mypy。

**Global Constraints:**

- 只改后端 `apps/backend`；不动前端 `apps/desktop` 与 Tauri。
- 不提交 Git commit。
- 完整类型注解 + 中文四段式 docstring（目的/参数/返回/异常副作用）；每个新函数必须有 docstring。
- 先写失败测试，再写生产代码。
- 交付走「独立审查 Agent + 独立测试 Agent」闭环；开发 Agent 不自宣完成。
- 日志使用 `app.config.logging.logger.log` 的结构化接口（`extra={"msg","data"}`），不得输出敏感信息。

---

## 现状（调研事实）

- **真实落盘 `.cosir` 的位置只有 3 处**：`workspace_service._init_cosir_metadata`（创建 `.cosir` 与 `.cosir/Attachment`）、`attachment_service._attachment_directory`（`<root>/.cosir/Attachment` + `.uploading`）、`image_utils._normalize_cosir_root`（受信前缀判定）。三处各自拼基名，无收口。
- **`cosir` 令牌**（`cosir-attachment://`、`cosir-file:`、`cosir_image_ref`）只是附件命名空间，不构造目录，不在本次改造范围。
- **只读缺口（已核实）**：文件工具全部走 `PathResolver.resolve_within_workspace`，它只校验 workspace containment + 拦截 OS 设备路径，**不拦截 `.cosir`**；系统提示词仅「劝告」模型不要碰 `.cosir`，无强制。
- **`resolve_within_workspace` 的完整调用面**：`write_file`、`replace_tool`(patch_write)、`patch_apply`(apply_patch)、`delete_tool`、`move_tool`、`apply_patch_tool`(写后语法检查)、`file_operation_paths.resolve_workspace_relative_path`、`guard/tool_output_budget`、`guard/file_resource_paths`、`service/terminal/terminal_session_service._resolve_cwd`。
- **只读工具**（`read_file`/`list_directory`/`search_content`/`find_files`）走 `resolve_without_boundary`，不受影响。
- **第二个内部目录**：工具输出落盘写 `{workspace_root}/.coding-agent/tool-artifacts/<uuid>.txt`；`.coding-agent` 被 `file_walker.IGNORED_DIRS` 与 `system_prompt_builder._IGNORED_DIRS` 隐藏，而 `.cosir` 两处都不在跳过集（口径不一致）。

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
| `system_cosir_dir() -> Path` | `Settings.SYSTEM_COSIR_DIR`（函数内延迟 import `Settings`，避免 `utils→config` 顶层耦合与 import 期副作用） |

**落点理由**：`app/utils` 是 leaf 层，可被 `service` / `core/tools` / `core/context` 复用；已核实 `app/utils` 与 `app/config` 之间当前**无任何依赖边**（0 匹配），故用函数内延迟 import 收口系统级路径而不引入顶层耦合。

### 2. 系统级 `.cosir` 是进程级配置

`Settings` 新增 `SYSTEM_COSIR_DIR: ClassVar[Path]`，在 `load()` 中：

```python
data_root = Path(os.environ.get("CODING_AGENT_DATA_DIR", str(root)))
cls.SYSTEM_COSIR_DIR = data_root / ".cosir"
```

- 桌面端 `CODING_AGENT_DATA_DIR` 由 Tauri 注入 `app_data_dir()` → Windows/macOS 差异由宿主处理，Python 无需自识别 OS。
- 开发态回落到仓库根（与 `storage/`、`logs/` 同源），可接受。
- 作为类级注解属性，天然可经 `Settings.override(SYSTEM_COSIR_DIR=...)` 供测试隔离。

### 3. 只读保护收口：`PathResolver.resolve_within_workspace`

```python
def resolve_within_workspace(self, path: str, *, allow_reserved: bool = False) -> tuple[Path | None, str]:
    ...
```

- 解析成功后，若 `not allow_reserved and is_within_cosir(resolved, self.workspace_root)` → 返回 `(None, reason)`。
- `reason` 为面向模型的英文短句：`.cosir` 是只读保留区，写/改/删/移动必须指向 `.cosir` 之外。
- 默认拒绝 ⇒ 所有写/改/删/移动工具（含经 `resolve_workspace_relative_path` 的 delete/move、经 `patch_apply` 的 apply_patch）**零调用点改动**即获得保护。
- **豁免且仅豁免**：`terminal_session_service._resolve_cwd` 传 `allow_reserved=True`，对齐用户「终端除外」的要求（原始 shell 通道无法可靠拦截，故不拦截）。
- **保持默认拒绝**：`guard/file_resource_paths._resolve_workspace_path`（变更集资源登记）—— 与「`.cosir` 不进变更集」一致。
- **天然不受影响**：只读工具（`resolve_without_boundary`）；`attachment_service` 自身写 `.cosir` 不经 `PathResolver`。
- **无新增 `blocked_device_reason` 分支**：`.cosir` 保护是「保留子树」语义，不属设备/伪文件规则，保持两类规则单一职责。

### 4. 内部目录统一：`.coding-agent` → `.cosir`

- `guard/tool_output_budget._write_artifact`：落盘相对路径由 `.coding-agent/tool-artifacts/<uuid>.txt` 改为 `cosir_paths.workspace_tool_artifact_dir` 派生的 `.cosir/tool-artifacts/<uuid>.txt`。
- `file_walker.IGNORED_DIRS` 与 `system_prompt_builder._IGNORED_DIRS` 移除 `.coding-agent`（该目录不再创建）。
- `.cosir` **不加入**搜索/提示词跳过集（用户要求「允许读」）；递归搜索会遍历 `.cosir`，属已确认取舍。

### 5. 系统 `.cosir` 启动创建

后端 lifespan 启动时幂等创建 `Settings.SYSTEM_COSIR_DIR`；失败降级（记 error 日志）不阻断启动。与 workspace 级 `.cosir` 创建（`workspace_service._init_cosir_metadata`，失败降级）保持同一降级策略。

### 6. 提示词文案

`.cosir/` 文案由 "holds runtime metadata: do not read or modify it" 改为只读语义：可读，禁止写/改/删。

---

## 影响文件清单

新增：

- `apps/backend/app/utils/cosir_paths.py`
- `apps/backend/tests/test_cosir_paths.py`
- `apps/backend/tests/test_path_resolver_cosir_guard.py`

修改：

- `apps/backend/app/config/settings.py`（新增 `SYSTEM_COSIR_DIR`）
- `apps/backend/app/service/task/workspace_service.py`（`_init_cosir_metadata` 改用模块）
- `apps/backend/app/service/attachment/attachment_service.py`（`_attachment_directory` 改用模块）
- `apps/backend/app/utils/image_utils.py`（`_normalize_cosir_root` 改用模块）
- `apps/backend/app/core/tools/tool_handler/security/path_resolver.py`（`allow_reserved` + `.cosir` 保护）
- `apps/backend/app/service/terminal/terminal_session_service.py`（`_resolve_cwd` 传 `allow_reserved=True`）
- `apps/backend/app/core/tools/guard/tool_output_budget.py`（artifact 落盘改 `.cosir`）
- `apps/backend/app/core/tools/tool_handler/search/file_walker.py`（移除 `.coding-agent`）
- `apps/backend/app/core/context/system_prompt_builder.py`（移除 `.coding-agent`、改 `.cosir` 文案）
- `apps/backend/app/app.py`（lifespan 创建系统 `.cosir`）
- `.gitignore`（`.coding-agent/` → `.cosir/`；开发态系统 `.cosir` 落在仓库根，必须忽略）

不改动（明确排除）：

- `cosir` 令牌相关模块（`conversation_run_extra`、`run_event`、`transport_assistant_service`、`conversation_state_snapshot`、`assistant_image_part`、`runtime_context_manager`、`vision_input`）—— 它们只引用令牌，不构造目录。
- 前端与 Tauri。
- `.cosir/trash`（历史方案未落地，本次不做）。

---

## 测试要点

| 用例 | 断言 |
| --- | --- |
| `cosir_paths` 纯函数 | 各路径拼接正确；`is_within_cosir` 对 `<root>/.cosir/x`、`<root>/.cosir` 判 True，对 `<root>/.cosir2/x`、`<root>/x/.cosir` 判 False；符号链接指向 `.cosir` 判 True |
| `resolve_within_workspace` 保护 | 写路径落在 `.cosir` → `(None, reason)`；`allow_reserved=True` → 通过；`.cosir` 外路径 → 通过 |
| 五个文件工具 | `write_file`/`patch_write`/`apply_patch`/`delete_file`/`move_file` 指向 `.cosir` 均返回 error 且**磁盘未被修改** |
| 只读工具 | `read_file`/`list_directory`/`search_content` 读 `.cosir` 仍成功 |
| 终端豁免 | `_resolve_cwd` 落在 `.cosir` 不抛错 |
| 变更集登记 | `file_resource_paths` 指向 `.cosir` 仍拒绝 |
| artifact 落盘 | `tool_output_budget` 写 `.cosir/tool-artifacts/`，`Return` 相对路径前缀正确 |
| 系统 `.cosir` | `Settings.load` 后 `SYSTEM_COSIR_DIR == data_root/".cosir"`；启动创建幂等、失败降级 |
| 既有回归 | `test_workspace_service_create_reuse.py`、`test_attachment_dedup.py`、`test_image_normalizer.py` 仍通过 |

---

## 风险与取舍

- **开发态 `.cosir` 落在仓库根**：与 `storage/`、`logs/` 一致，已确认接受；`.gitignore` 现仅忽略 `.coding-agent/`，需同步改为忽略 `.cosir/`，否则仓库根 `.cosir` 会污染 `git status`（`.gitignore` 已纳入影响文件清单）。
- **递归搜索会遍历 `.cosir`**（含 `tool-artifacts` 与 `Attachment`）：用户明确选择「可读」，属已确认取舍。
- **`.coding-agent` 迁移**：历史 workspace 若残留 `.coding-agent`，不再被跳过集隐藏；绿地项目不需要兼容，接受。
