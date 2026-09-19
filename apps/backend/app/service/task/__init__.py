"""Task/Workspace/Conversation Run 领域服务编排包。

单一职责：承载 task、workspace 与 Conversation Run（含运行流编排）的
服务层编排。各服务类经 ``app.service.depends`` 取得底层 CRUD 与存储单例，不直接
持有 SQL 逻辑。

职责边界：
- 负责：任务创建与生命周期管理（``TaskService``）、工作区编排（``WorkspaceService``）、
  Conversation Run 状态与执行编排（``ConversationRunExecutor`` / ``ConversationRunService``）、
- 不负责：直接数据库读写（委托给 ``app.storage.crud``）；单任务 / 工作区级联删除的
  内部编排细节（归 ``TaskService`` / ``WorkspaceService``）。
"""
