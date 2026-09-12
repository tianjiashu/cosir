# Task 4 报告：移除持久化 snapshot，统一 canonical state 冷读

## 结果

状态：DONE

实现提交：`2d337a7`（`feat: rebuild assistant state from canonical context`）。

ConversationStateSnapshot 现在只作为 Assistant Transport 进程内 working state 存在。冷读
统一从 `TaskRecord`、该 Task 的 `ConversationRunRecord[]` 与
`ConversationTaskContextRecord[]` 重建；LangGraph checkpoint 仍只保留给显式 business resume。

## TDD 记录

- RED：先新增 `apps/backend/tests/test_task4_state_lifecycle.py`，运行
  `uv --cache-dir H:\\coding-agent\\.uv-cache-task4 run pytest tests/test_task4_state_lifecycle.py -q`
  在收集阶段按预期失败：`ModuleNotFoundError`，目标 state service 尚未实现。
- GREEN：同一测试在实现后为 `5 passed`。
- 相关回归：
  `uv --cache-dir H:\\coding-agent\\.uv-cache-task4 run pytest tests/test_task4_state_lifecycle.py tests/test_conversation_task_state_rebuilder.py tests/test_conversation_event_projector.py tests/test_assistant_transport_api.py tests/test_conversation_fact_models.py tests/test_task3_canonical_write_paths.py tests/test_task_workspace_deletion.py -q --basetemp H:\\coding-agent\\apps\\backend\\.pytest-basetemp-task4`
  为 `151 passed`。
- 完整后端回归：同一 uv cache/basetemp 配置下为 `356 passed, 1 skipped, 1 failed`。唯一失败是
  Windows terminal worker 集成测试找不到 `apps/terminal-worker/target/debug/terminal-worker.exe`；
  与本任务无关。默认 uv cache 目录也因权限不足不可用，改用 workspace cache 后测试正常运行。
- 静态：本次修改文件 Ruff 通过；`uv ... run python -m compileall -q app` 通过；新增
  `conversation_task_state_service.py` 使用 `mypy --config-file mypy.strict.ini --follow-imports=skip`
  通过。完整 strict MyPy 会触达存量跨模块错误，未作为本任务门禁。
- 静态 grep：应用代码与测试中不再存在 conversation snapshot model/CRUD/service、
  `state_json`、`ensure_state_snapshot`、旧 snapshot getter 或 snapshot CRUD 调用；fresh-schema
  负断言仍保留在 `test_conversation_fact_models.py`。

## 修改内容

- 新增 `apps/backend/app/assistant_transport/service/conversation_task_state_service.py`：
  - 提供唯一 `get_state`/`read` 冷读边界；无 working copy 时调用纯
    `ConversationTaskStateRebuilder`，有 working copy 时仅保留活动期 live message delta，
    并用 canonical Run status/end reason/usage 与 Task current-run/context usage 做最终校正。
  - 保留进程内 projector mutation、SSE subscriber、首帧与订阅注册同锁；disconnect 只注销订阅。
  - `validate_snapshot` 作为返回前最后校验；rebuild start/completed/failed 使用结构化日志，
    canonical metadata 错误直接失败，不降级为空 state。
  - 后端 storage 生命周期结束时清理 working copy、subscriber 与 deleted-task tombstone。
- 删除 `conversation_task_snapshot_model.py`、`conversation_task_snapshot_crud.py` 与旧的
  持久化 `ConversationTaskSnapshotService`；移除旧 fork/edit/delete/cleanup 与 API 依赖。
- 切换 GET `/tasks/{task_id}/assistant/state`、SSE 首帧/终态读取、attach/resume 读取、Run
  command、fork/edit、Task/workspace deletion 和 restart/orphan recovery 到 canonical state
  边界；显式 resume 的 checkpoint continuation 未改变。
- `ConversationEventProjector` 现在只调用进程内 state owner，不接受或创建数据库事务，也不创建
  canonical message facts；Task 3 的 database-first/post-commit 事件边界保持不变。
- 更新 Assistant Transport、Projector、删除、事实模型相关测试，并删除不再使用的旧 fork 异常与
  snapshot transaction port。

## 边界与审查要点

- 没有旧数据库迁移、legacy fallback、双 schema 或空 state fallback。
- 没有从冷读路径导入/查询 checkpoint、ToolRegistry 或 frontend runtime。
- `docs/plan/conversation-state-rebuild-from-context-plan.md`、进度账本中的既有用户修改，以及
  `apps/backend/app/assistant_transport/request/assistant_transport_request.py~` 未纳入本次提交。

## 提交

- 报告更新提交：实现提交后写回哈希并单独提交。
