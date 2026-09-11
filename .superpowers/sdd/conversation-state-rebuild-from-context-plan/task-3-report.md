# Task 3 报告：canonical context/tool/run 写路径

## 结果

状态：DONE_WITH_CONCERNS

本次提交：以 `git log` 中的 Task 3 提交为准。

## TDD 记录

- RED：新增 `tests/test_task3_canonical_write_paths.py` 后，首次聚焦运行结果为
  `5 failed`；补充 system prompt、model parts、tool presentation 和 Run 初始化用例后，
  结果为 `9 failed`；补充 fresh Run 重载 canonical user 用例后，该用例单独运行仍为
  `1 failed`。失败均对应尚未实现的目标行为。
- GREEN：`pytest tests/test_task3_canonical_write_paths.py -q` 结果为 `10 passed`。
- 相关回归：context manager、tool lifecycle、usage listener 与 Task 3 用例结果为
  `29 passed`；tool repair/observation 回归结果为 `8 passed`。
- 静态检查：Task 3 修改文件 Ruff 通过，Python `compileall` 通过。聚焦 MyPy 触达两个
  未修改的既有错误：`app/assistant_transport/event/run_event.py` 的 TypedDict 动态键，
  以及 `app/assistant_transport/event/message_event.py` 的列表类型重绑定。
- 完整后端回归：`pytest -q --tb=short` 结果为 `336 passed, 1 skipped, 7 failed`。

## 修改内容

- `ConversationTaskContextService` 成为 canonical message/Transport metadata writer：
  统一调用已有 typed metadata serializer，提供初始 HumanMessage 与 Task system prompt
  的幂等写入，并按 tool call id 做持久化幂等。
- `RuntimeContextManager` 不再裁剪 AIMessage 字段；system prompt 从持久化事实加载，
  fresh/resume 重新建立正确的 working copy；流式 chunks 仍只在内存中存在。
- `ModelChunkProcessor` 生成有序、冻结 presentation 的 text/reasoning/tool-call parts；
  model node 在完整 AIMessage 边界一次写入。
- `ToolCallLifecycleManager` 保存静态 presentation，ToolMessage 的 structured result
  metadata 先写入 context，再发布终态 Transport event；重复 settle 不重复写入或发事件，
  artifact data 不进入 Assistant metadata。
- Run 创建在同一数据库事务中写入 Run、canonical user message 和 Task current run，提交后
  才投影初始化事件；completed/failed/cancelled/restart recovery 持久化六键 usage、受控
  error、end reason 和 final output。
- context usage 先持久化 Task 的 used/window，再发布 usage event。
- 更新受新 metadata 参数影响的相关测试桩与断言。

## 边界

- 未删除 snapshot 表或 snapshot projector/read path，未切换 Task 4 的读取路径。
- 未引入新的服务、缓存、checkpoint 或事实表。

## Concerns

- 完整回归的 6 个失败来自现有 fresh schema 与旧 snapshot cleanup 测试之间的已知边界：
  `conversation_task_snapshots` 表不存在，另有一个旧 snapshot recovery 断言仍期待已移除的
  task-space projection hook。这些属于 Task 4，不在本次修改。
- 终端 worker 集成测试需要桌面 sidecar `terminal-worker.exe`，当前环境未提供。

## Fix round 1（independent acceptance findings）

状态：修复完成，等待独立复审；Task 4 snapshot 删除/读路径切换未触碰。

### TDD RED/GREEN

- RED（先测后改）：新增/更新六类行为测试后运行
  `apps/backend/.venv/Scripts/python.exe -m pytest tests/test_task3_canonical_write_paths.py tests/test_conversation_fact_models.py -q`，结果为 `7 failed, 51 passed`；失败分别暴露 fresh user 身份、完整 AIMessage、流式工具顺序、旁路异常、恢复事务/事件和数据库唯一约束问题。
- RED（exactly-once 追加覆盖）：补充 canonical append 返回 duplicate 时不发终态事件的测试，单测结果为 `1 failed`。
- GREEN：同一 focused 命令结果为 `60 passed`。
- 相关回归：context manager、tool lifecycle、usage listener、repair/observation、Assistant API 结果为 `51 passed, 1 failed`；唯一失败是既有 Task 4 snapshot 读测试仍断言已移除的 task-space projection hook。
- 全量回归（workspace basetemp）：`pytest -q --basetemp H:\coding-agent\.pytest-task3-fix-round1-20260912` 结果为 `344 passed, 1 skipped, 7 failed`。7 个失败均为已知边界：1 个旧 snapshot recovery 断言、5 个 Task 4 snapshot cleanup 缺表测试、1 个缺少 `terminal-worker.exe` 的 Windows sidecar 集成测试。
- 静态检查：本轮修改文件 Ruff 通过；`python -m compileall -q app` 通过；`git diff --check` 通过。

### 修复内容与变更文件

- `apps/backend/app/core/workflows/nodes/helper/model_chunk.py`：完整消息使用 LangChain 序列化字段恢复，保留结构化 content/name/id/metadata/tool-call 字段；Transport tool-call part 按 chunk 原始位置冻结并回填完整参数。
- `apps/backend/app/core/context/runtime_context_manager.py`：canonical append 返回 created/duplicate；DB 成功后 listener 失败结构化记录并降级；fresh reset 保留 canonical HumanMessage 行身份。
- `apps/backend/app/core/workflows/nodes/helper/tool_call_lifecycle.py`：仅首个持久化 ToolMessage 发终态事件；发送失败非致命；失败事件与持久化使用同一 sanitized display payload。
- `apps/backend/app/models/conversation_task_context.py`、`apps/backend/app/storage/model/conversation_task_context_model.py`、`apps/backend/app/storage/crud/conversation_task_context_crud.py`：增加 `tool_call_id` 持久化列、`(task_id, run_id, tool_call_id)` 唯一性、savepoint duplicate 返回值和 fresh 生成消息清理。
- `apps/backend/app/service/task/conversation_task_context_service.py`：接通 duplicate 返回值；恢复在外部事务内修复 interrupted ToolMessage 并写 typed tool_result metadata。
- `apps/backend/app/storage/init_schema.py`：为已有 SQLite 主库补列、回填 tool_call_id 并建立唯一索引。
- `apps/backend/app/storage/crud/conversation_run_crud.py`、`apps/backend/app/service/task/conversation_run_service.py`：orphan recovery 在同一事务提交 Run 终态和 ToolMessage 修复，提交后再发状态 projector 事件并降级旁路异常。
- `apps/backend/tests/test_task3_canonical_write_paths.py`、`apps/backend/tests/test_conversation_fact_models.py`：新增上述 RED/GREEN、真实 SQLite CRUD、真实 orphan recovery 和身份幂等测试。

### 提交

- 本轮修复提交：`8940855`（`fix: harden task 3 canonical recovery writes`）。
