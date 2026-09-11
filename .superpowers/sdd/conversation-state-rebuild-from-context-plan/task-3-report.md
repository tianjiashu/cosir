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
