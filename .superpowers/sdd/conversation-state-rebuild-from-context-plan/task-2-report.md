# Task 2 报告：三模型重建器与 Agent context loader

## 结果

状态：DONE_WITH_CONCERNS

提交：本次 Task 2 提交（最终哈希以 `git log` 为准）

## TDD 记录

- RED：`cd apps/backend && uv run pytest tests/test_conversation_task_state_rebuilder.py -q`
  首次被 uv 缓存目录权限拦截；使用授权重试后，pytest 在收集阶段按预期失败：
  `ModuleNotFoundError: No module named 'app.assistant_transport.service.conversation_task_state_rebuilder'`。
- GREEN/相关回归：`uv run pytest tests/test_conversation_task_state_rebuilder.py tests/test_conversation_fact_models.py tests/test_runtime_context_manager.py tests/test_conversation_event_projector.py -q`
  结果：`88 passed`。
- 静态检查：Task 2 修改文件 Ruff 通过；新增重建器与 loader 使用严格 MyPy 聚焦检查通过。
- 完整后端回归：`uv run pytest -q` 结果为 `311 passed, 1 skipped, 7 failed`。

## 修改内容

- 新增 `apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py`：
  - 从 `TaskRecord`、`ConversationRunRecord`、`ConversationTaskContextRecord` 纯重建
    `ConversationStateSnapshot`。
  - Run 按 `created_at`、id 稳定排序；current run、context usage、ratio、Run status、
    end reason、usage 均按 brief 的事实源映射；顶层 approvals 固定为空、error 固定为 None。
  - HumanMessage 映射 user；同一 Run 的 AI rows 按 sequence 合并，assistant id 使用首个
    AI row 的 `str(id)`；ToolMessage 仅按 tool_call_id 回填 AI tool-call part；SystemMessage
    不进入 UI runs。
  - 严格校验 task/run 归属、row id、schema version、metadata、孤儿/重复 tool call，并
    使用 `validate_snapshot`；失败统一抛出带 code/task/run/row 信息的结构化异常。
- 新增 `apps/backend/app/core/context/agent_context_loader.py`：
  - 无缓存、无数据库写入、无 RuntimeContextManager 依赖；按 include flag 和 sequence
    返回深拷贝的 LangChain BaseMessage，包含 SystemMessage，不附加 UI metadata。
- 修改 `apps/backend/app/models/json_helpers.py`：
  - 暴露不做 wire 默认填充的 `validate_transport_metadata`，供纯重建路径复用既有严格校验。
  - `tool_result` 支持 brief 要求的可选 `errorCode`、`isError`，保留原有必需字段和未知字段拒绝。
- 新增 `apps/backend/tests/test_conversation_task_state_rebuilder.py`，覆盖空 Task、消息映射、
  AI 合并、tool result、usage/current run、排序、schema/metadata、跨 Task、孤儿 Run、
  孤儿 ToolMessage、重复 tool_call_id、SystemMessage loader 和 snapshot validation。

## 接口决定

- 主接口为 `ConversationTaskStateRebuilder.rebuild(task, runs, context_rows)`。
- Loader 接口为 `AgentContextLoader.load(task_id, context_rows)`，并提供等价函数
  `load_agent_context(task_id, context_rows)`。
- 未接入 API、SSE、Projector read/write path，也未删除或改造 snapshot service。
- 未读取 database session、snapshot、LangGraph checkpoint、ToolRegistry、前端 runtime 或
  内存 Transport state。

## Concerns

- 完整回归的 7 个失败与本次新增模块没有直接调用关系：一个既有 snapshot recovery 测试断言未满足，
  五个 task/workspace deletion 测试报告 SQLite 中缺少 `conversation_task_snapshots` 表，另一个
  Windows terminal worker 集成测试缺少 `terminal-worker.exe`。相关 Task 2 与 snapshot/context/
  projector 回归均通过，建议后续单独处理这些基线/环境问题。

## Fix round 1（scoped review）

- RED：先更新 `test_conversation_task_state_rebuilder.py`，新增 terminal 未匹配 tool-call
  收口、success/failure/cancelled 结果映射、短提示、嵌套 `display_data` 隔离和五类错位
  metadata 测试；运行 `uv run pytest tests/test_conversation_task_state_rebuilder.py -q`，
  结果为 `11 failed, 14 passed`，失败均对应 review finding。
- GREEN：实现后同一 focused 命令结果为 `25 passed`；相关回归命令结果为 `99 passed`。
- 修复内容：终态 Run 的孤儿 tool-call 映射为 cancelled/failed；ToolMessage 结果只把受控
  `status_hint`（取消固定为“已取消”）写入 UI error，full error 不进入 snapshot，并保持
  status/isError 一致、保留 errorCode；所有 result display_data 深拷贝；按消息类型拒绝
  tool_result、非空 ToolMessage parts、缺失 tool_result 和 UI tool part 错位。
- Fix round 1 修改文件：
  `apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py`、
  `apps/backend/tests/test_conversation_task_state_rebuilder.py`、本报告。
- 未扩展到 Task 3：未修改 canonical context/tool/run 写路径、API/SSE、Projector、snapshot
  removal 或其它持久化集成。

## Fix round 2（scoped re-review）

- RED：先更新 `test_conversation_task_state_rebuilder.py`，加入终态未匹配 tool-call 清除旧
  `display_data`/`errorCode`、失败/取消丢弃成功态展示数据、Human/System 任意非空 parts 拒绝、
  ToolMessage 缺省 `errorCode` 清除，以及长/不可打印 `status_hint` 测试。第一轮新增断言运行结果为
  `9 failed, 19 passed`；再加入不可打印提示回归后运行结果为 `1 failed, 28 passed`。
- GREEN：修复后 focused `uv run pytest tests/test_conversation_task_state_rebuilder.py -q`
  结果为 `29 passed`。
- 相关回归：重建器、事实模型、RuntimeContextManager、Projector、工具错误工厂与生命周期测试
  结果为 `113 passed`。
- 静态检查：修改文件 Ruff 通过；
  `uv run mypy --config-file mypy.strict.ini --follow-imports=skip
  app/assistant_transport/service/conversation_task_state_rebuilder.py
  app/core/context/agent_context_loader.py` 结果为 `Success: no issues found in 2 source files`。
- 修复内容：失败结果只投影受控短 `status_hint`，取消结果固定为“已取消”，二者均不保留成功态
  `kind`/path/list/diff/result 数据；成功结果保持嵌套深拷贝。终态未匹配调用清除旧展示数据和
  错误码。Human/System 只有空 parts 才合法。`errorCode` 只从 ToolMessage 的 tool result 写入，
  缺省时显式清除旧值。`tool_error` 与重建器共用长度、空白和可打印性归一化规则。
- Fix round 2 修改文件：
  `apps/backend/app/assistant_transport/service/conversation_task_state_rebuilder.py`、
  `apps/backend/app/core/tools/tool_execute/tool_error.py`、
  `apps/backend/tests/test_conversation_task_state_rebuilder.py`、本报告。
- 未扩展到 Task 3：未修改 snapshot removal、canonical writers、API/SSE、Projector read/write
  path 或其它持久化集成。
