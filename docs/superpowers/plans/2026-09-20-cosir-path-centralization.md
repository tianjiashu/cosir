# `.cosir` 路径集中与只读保护 Implementation Plan

> 实施结果：系统级运行数据统一位于 ``<app_data_dir>/.cosir``。Tauri 开发版与打包版均注入
> ``CODING_AGENT_DATA_DIR=<app_data_dir>``；后端从该根推导 ``.env``、SQLite、checkpoint、
> 日志和 runtime。工作区级 ``.cosir`` 不受影响。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 workspace 级与系统级 `.cosir` 路径收敛为单一事实源，并对 `.cosir` 施加「可读、禁止写/改/删/移动（终端除外）」的保护；同时把工具输出 artifact 目录从 `.coding-agent` 并入 `.cosir`。

**Architecture:** 新增 leaf 模块 `app/utils/cosir_paths.py` 承载全部 `.cosir` 路径计算与保留子树判定；系统级 `.cosir` 的**目录名固定**为 `cosir_paths.COSIR_DIR_NAME`，其**数据根**新增为通用配置 `Settings.DATA_DIR`。只读保护收口在 `PathResolver.resolve_within_workspace`（写/改/删/移动的唯一校验点），新增 `allow_reserved` 开关，默认拒绝 `.cosir` 子树，仅两处内部调用显式豁免。

> **修订（2026-09-20）**：`DATA_DIR` 已不再来自 `Settings`（该字段与 `LOG_DIR` / `DATABASE_FILE` / `CHECKPOINT_FILE` 一并从 `Settings` 删除），改由**固定路径模块 `app/config/paths.py`** 承载（`DATA_DIR = CODING_AGENT_DATA_DIR or 仓库根`）。本计划凡出现 `Settings.DATA_DIR` 处，一律按 `paths.DATA_DIR` 理解；其余路径落点与只读保护结论不变。

**Tech Stack:** Python 3.11+、pytest、Ruff、mypy。

**Spec:** `docs/superpowers/specs/2026-09-19-cosir-path-centralization-design.md`

## Global Constraints

- 只改后端 `apps/backend` 与仓库根 `.gitignore`；不动前端、不动 Tauri。
- 不提交 Git commit。
- 完整类型注解 + 中文四段式 docstring（目的/参数/返回/异常副作用）；函数签名变更同步更新 docstring。
- 先写失败测试，再写生产代码。
- 交付以「独立审查 Agent + 独立测试 Agent」的通过结论为准；开发 Agent 不自宣完成。

---

## 任务

### Task 1：`app/utils/cosir_paths.py` + `Settings.DATA_DIR`

- Create: `apps/backend/app/utils/cosir_paths.py`
- Modify: `apps/backend/app/config/settings.py`（类属性 + `load()` 中 `data_root / ".cosir"`）
- Test: `apps/backend/tests/test_cosir_paths.py`

接口：`COSIR_DIR_NAME` / `COSIR_ATTACHMENT_DIR_NAME` / `COSIR_TOOL_ARTIFACT_DIR_NAME`、`workspace_cosir_dir(root)`、`workspace_attachment_dir(root)`、`workspace_attachment_staging_dir(root)`、`workspace_tool_artifact_dir(root)`、`is_within_cosir(path, root)`、`system_cosir_dir()`（函数内延迟 import `Settings`）。

### Task 2：`PathResolver` `.cosir` 保护 + 终端豁免

- Modify: `apps/backend/app/core/tools/tool_handler/security/path_resolver.py`（`resolve_within_workspace(path, *, allow_reserved=False)`）
- Modify: `apps/backend/app/service/terminal/terminal_session_service.py`（`_resolve_cwd` 转 `@staticmethod` + 传 `allow_reserved=True`）
- Test: `apps/backend/tests/test_path_resolver_cosir_guard.py`

### Task 3：现有散落点改用模块

- Modify: `workspace_service.py`（`_init_cosir_metadata`）、`attachment_service.py`（`_attachment_directory`）、`image_utils.py`（`is_trusted_cosir_path` 委托 `is_within_cosir`，删除 `_normalize_cosir_root` 与 `_COSIR_DIR_NAME`）
- Test: 既有 `test_workspace_service_create_reuse.py` / `test_attachment_dedup.py` / `test_image_normalizer.py` 回归

### Task 4：`.coding-agent` → `.cosir`（含跳过集/提示词/`.gitignore`）

- Modify: `guard/tool_output_budget.py`（落盘 `.cosir/tool-artifacts/`，写入传 `allow_reserved=True`）
- Modify: `tool_handler/search/file_walker.py`、`core/context/system_prompt_builder.py`（移除 `.coding-agent`；提示词 `.cosir` 改只读语义）
- Modify: `.gitignore`（`.coding-agent/` → `.cosir/`）
- Test: `tests/test_terminal_output_streaming.py` 增补 artifact 路径前缀断言

### Task 5：系统 `.cosir` 启动创建

- Modify: `apps/backend/app/app.py`（`_ensure_system_cosir_dir()` + lifespan 在第二次 `install_logging_for_current_process` 之后调用）

### Task 6：验证

- `uv run --project apps/backend pytest apps/backend/tests/... -q`（新增 + 既有回归）
- `uv run --project apps/backend ruff check` / `mypy`：只判我改动的文件无新增错误
- 派 `独立审查 Agent`（对照本计划与规范）+ `独立测试Agent`（对抗性验证），以两者通过为准

---

## 实施与验收结果（2026-09-20）

**产出文件**

- 新增：`app/utils/cosir_paths.py`、`tests/test_cosir_paths.py`、`tests/test_path_resolver_cosir_guard.py`
- 修改：`app/config/settings.py`、`app/core/tools/tool_handler/security/path_resolver.py`、
  `app/service/terminal/terminal_session_service.py`、`app/core/tools/guard/tool_output_budget.py`、
  `app/core/tools/tool_handler/search/file_walker.py`、`app/core/context/system_prompt_builder.py`、
  `app/service/task/workspace_service.py`、`app/service/attachment/attachment_service.py`、
  `app/utils/image_utils.py`、`app/app.py`、`.gitignore`
- 子 Agent 追加的对抗回归用例：`tests/test_cosir_guard_probe_adversarial.py`、
  `tests/test_cosir_attachment_regression_adversarial.py`

**实施期间的额外重构（审查驱动）**

- `attachment_service` 新增 `_TaskAttachmentDirs(workspace_root, attachment_dir)` 与 `_dirs(task_id)`，
  `workspace_root` 直接取自 workspace 记录，**移除 3 处 `directory.parent.parent` 反推**
  （第一轮审查不符合项 A）。
- `image_utils.is_trusted_cosir_path` docstring 补「异常」段（第一轮审查不符合项 B）。
- `tool_output_budget._write_artifact` 统一使用已 `resolve()` 的局部 `root`（消除已解析 root 与原始
  `workspace_root` 混用）。
- `is_within_cosir` 增加空白路径短路，避免 `realpath("")` 落到当前目录语义。

**后续修订（用户要求，2026-09-20）**：`.cosir` 是**固定目录名**，不应做成 `.cosir` 专用配置项 → 移除
`Settings.SYSTEM_COSIR_DIR`，改为**通用** `Settings.DATA_DIR`（`= CODING_AGENT_DATA_DIR or 仓库根`）；
`cosir_paths.system_cosir_dir() = Settings.DATA_DIR / COSIR_DIR_NAME`，Settings 中不再出现 `.cosir`
字面量。落点行为不变（`<数据根>/.cosir`）。**禁止**改成相对 `Path(".cosir")`：后端进程 cwd 为
`apps/backend`（Tauri `current_dir(backend_dir)`），会落到错误位置且打包版可能只读。

**验收（开发-审查-测试闭环，共两轮）**

| 轮次 | 独立审查 Agent | 独立测试Agent |
| --- | --- | --- |
| 第 1 轮 | **不符合**（2 条：`parent.parent` 反推、docstring 缺异常段） | **通过**（79 对抗用例，无缺陷） |
| 修复后第 2 轮 | **符合**（确认原问题真正消除、无新增不符合项） | **通过**（45 回归用例；符号链接根下显式断言等价性，无新缺陷） |

**最终验证证据**

- 本次相关测试：`183 passed, 1 skipped`（含我的 28 个 + 子 Agent 124 个对抗/回归用例）。
- 全量（忽略 7 个既有收集失败文件）：`28 failed, 776 passed, 3 skipped, 2 xfailed` —— 失败数与
  改动前基线**完全一致**，无新增失败。
- `ruff`：新增/修改文件无新增问题（`attachment_service.py` 的 14 条 E501 为既有）。
- `mypy`：本次改动文件**零报错**。
- 既有基线（**非本次引入**）：7 个测试文件收集失败（`app.core.workflows.react.nodes.helper.tool_call_lifecycle`
  符号导入失败，源自更早的 workflows 重构）；28 个既有失败（日志断言 / 取消原因文案过期）。
