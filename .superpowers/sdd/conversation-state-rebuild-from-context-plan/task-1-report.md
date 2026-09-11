# Task 1 report — 三类事实模型与序列化契约

## 修改文件

- `apps/backend/app/storage/model/task_model.py`
  - 增加 Task 级 `current_run_id`、`context_window_total`；保留 `context_usage_used`。
  - `current_run_id` 使用可空外键并在 Run 删除时置空。
- `apps/backend/app/storage/model/conversation_run_model.py`
  - 增加可空 `usage_json`、`error_json` 文本列。
- `apps/backend/app/storage/model/conversation_task_context_model.py`
  - 增加 `transport_metadata_json` 与 `message_schema_version`；未增加 `transport_message_id`。
- `apps/backend/app/models/json_helpers.py`
  - 新增严格 JSON object 序列化/反序列化 helper，拒绝非法 JSON、非 object 结构、非标准数值常量及不可序列化值。
- `apps/backend/app/models/conversation_task_context.py`
  - Context Record 往返映射 row `id`、typed Transport metadata 与 schema version。
- `apps/backend/app/models/conversation_run_record.py`
  - 新增六键 `ConversationRunUsage` typed contract、usage/error 严格解析与 `to_model` 映射。
- `apps/backend/app/models/task_record.py`
  - 新增 Task 字段的 `to_dict`、`from_model`、`to_model` 映射。
- `apps/backend/app/storage/crud/conversation_run_crud.py`
  - Run create 与 clone 透传并严格序列化 usage/error。
- `apps/backend/app/storage/crud/task_crud.py`
  - 新 Task 显式初始化空 current run 与空 context window。
- `apps/backend/app/storage/init_schema.py`
  - fresh schema 的 `APP_MODELS` 不再注册/创建 `conversation_task_snapshots`。
- `apps/backend/tests/test_conversation_fact_models.py`
  - 新增 Context、Run、Task 的真实 Record/ORM 映射、CRUD defaults/clone、非法 metadata 与 fresh schema 测试。

未修改 Assistant Transport 读路径、workflow projector 或 SSE 行为；未添加旧库迁移、legacy fallback 或兼容字段。

## TDD 证据

### RED

命令：

```text
uv run pytest tests/test_conversation_fact_models.py -q
```

该命令先因 uv 用户缓存目录权限不足而无法启动；随后使用同一项目环境执行：

```text
.venv\Scripts\python.exe -m pytest tests/test_conversation_fact_models.py -q
```

结果：`5 failed`。失败原因为目标生产能力确实不存在：Context Record 不接受 `id`、Context ORM 不接受 metadata/version 列、Run Record 不接受 usage、Task ORM 不接受 current run/window 列，且 fresh schema 仍创建 snapshot 表；不是测试拼写或断言错误。

### GREEN

新增及相关模型/schema 回归：

```text
.venv\Scripts\python.exe -m pytest tests/test_conversation_fact_models.py tests/test_sqlite_schema.py tests/test_context_usage_compute_listener.py tests/test_conversation_run_usage_stats.py -q
```

结果：`15 passed in 0.74s`。

静态检查：

```text
.venv\Scripts\ruff.exe check [本任务修改的 backend 源码及测试文件]
```

结果：`All checks passed!`。

最终提交后再次执行同一 15-test 命令，仍为 `15 passed`；Ruff 再次为 `All checks passed!`。

全量后端测试结果：`260 passed, 1 skipped, 7 failed`。其中 1 个 terminal worker PowerShell 测试因环境缺少 `terminal-worker.exe`；1 个 Assistant Transport 测试为当前工作树既有行为不一致；5 个 Task/Workspace deletion 回归失败于旧 TaskService 仍无条件调用已按本 brief 从 fresh schema 移除的 `conversation_task_snapshots` 表。

Mypy 检查本任务相关文件时未发现新增类型错误；`init_schema.py` 仍报告原有 4 个 `ReflectedIndex`/`Index.create` 类型错误。

## Commit

实现 commit：`3585cbbcd26d1b282e6776ff10852e06aac15956` (`feat: add conversation fact model contracts`)

该 commit 只包含本任务 12 个文件。已有用户 staged 文件 `apps/backend/app/assistant_transport/request/assistant_transport_request.py~` 与已修改的 `docs/plan/conversation-state-rebuild-from-context-plan.md` 未并入。

## 未解决问题 / concern

`conversation_task_snapshots` 已按本任务要求不再由 fresh schema 创建，但现有 `TaskService._delete_single_task_in_session` 仍调用 snapshot CRUD，导致删除相关回归出现 `no such table`。本任务 brief 明确要求不要移除 snapshot service 或修改 workflow 写路径，也禁止添加 legacy fallback，因此未在本任务内绕过该冲突；后续 snapshot 清理任务需要同步收口该调用边界。

## 本轮复核修复（Task 1 Important findings）

### 修改文件

- `apps/backend/app/models/json_helpers.py`
  - 将通用 JSON object 校验扩展为递归 JSON 值校验。
  - 定义并校验 `TransportMetadata`：顶层必须恰有 `schema_version`、`parts`、`tool_result`；parts 按 text/reasoning/tool-call 结构校验；tool_result 只能为 JSON object 或 null。
  - 定义并校验受控 `ConversationRunError`：仅允许 `code`、`message`、`retryable`，因此拒绝 provider response、stack、prompt 等非受控字段及错误形状。
- `apps/backend/app/models/conversation_run_record.py`
  - 仅允许 `cache_miss_tokens` 为 null，其余五个 usage 键拒绝 null；Run error 改用受控 helper。
- `apps/backend/app/models/conversation_task_context.py`
  - Context metadata 改用结构化 typed helper，并为新记录提供合法空 metadata 基线。
- `apps/backend/app/storage/crud/conversation_run_crud.py`
  - create/clone 使用受控 Run error contract。
- `apps/backend/app/service/task/conversation_task_context_service.py`
  - fork clone 深复制 `transport_metadata` 并复制 `message_schema_version`。
- `apps/backend/app/models/__init__.py`
  - 导出 `ConversationRunError`。
- `apps/backend/tests/test_conversation_fact_models.py`
  - 增加五个不可空 usage 键的参数化拒绝测试、malformed metadata/JSON、受控 error 字段与 malformed JSON 测试；增加真实 ORM row 的 fork clone 字段验证。

### 本轮 TDD 证据

RED 命令：

```text
cd apps/backend
$env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/test_conversation_fact_models.py -q --basetemp='H:\\coding-agent\\apps\\backend\\.pytest-task1-red'
```

结果：`13 failed, 9 passed in 1.06s`。失败集中在五个非 cache-miss usage 键仍接受 null、metadata 结构仍只按 object 接受、error 仍接受 provider_response/stack/prompt、以及 fork clone 落库为 `{}`/schema version 1，证明生产能力缺失。

GREEN 命令：

```text
cd apps/backend
$env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/test_conversation_fact_models.py -q --basetemp='H:\\coding-agent\\apps\\backend\\.pytest-task1-green'
```

结果：`22 passed in 0.61s`。

相关回归命令：

```text
cd apps/backend
$env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/test_conversation_fact_models.py tests/test_sqlite_schema.py tests/test_context_usage_compute_listener.py tests/test_conversation_run_usage_stats.py -q --basetemp='H:\\coding-agent\\apps\\backend\\.pytest-task1-related'
```

结果：`30 passed in 0.74s`。

静态检查：指定修改文件的 Ruff 为 `All checks passed!`，`git diff --check` 无输出。按本轮范围执行 mypy 时仍会经过既有 Assistant Transport、snapshot service、terminal/web_extract 及 `init_schema.py` 类型问题；未为此越界修改 Task 4 或 Transport 代码。

本轮未处理 snapshot 删除路径，也未恢复 snapshot 表。
