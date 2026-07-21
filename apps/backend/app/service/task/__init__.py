"""Task orchestration service.

单一职责：编排任务的创建与状态管理，协调 task CRUD 与 turn CRUD。

职责边界：
- 负责：创建任务（同时创建首个轮次）、更新任务状态、查询任务。
- 不负责：直接操作数据库（委托给 ``SQLiteTaskStore``）。
"""
