"""Task/Workspace/Turn 领域服务编排包。

单一职责：承载 task、workspace、turn（含 turn 流编排）与变更集（change_set）的
服务层编排。各服务类经 ``app.service.depends`` 取得底层 CRUD 与存储单例，不直接
持有 SQL 逻辑。

职责边界：
- 负责：任务创建与生命周期管理（``TaskService``）、工作区编排（``WorkspaceService``）、
  轮次状态与执行编排（``ConversationRunExecutor`` / ``TurnService``）、turn→workspace 路径解析
  （``TurnWorkspaceResolver``）、任务级变更集查询与保留/撤销（``change_set`` 子包）。
- 不负责：直接数据库读写（委托给 ``app.storage.crud`` 与 ``app.storage.cascade_deletion``）；
  原子级联删除（归 ``CascadeDeleter``）。
"""
