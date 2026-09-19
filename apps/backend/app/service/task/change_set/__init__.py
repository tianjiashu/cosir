"""变更集服务包（变更写入日志 + 整组保留/回退）。

包内模块按职责单一拆分：

- ``file_mutation_service``：结构化文件工具写入的预写快照与收口边界（预写
  before-image、失败收口、启动恢复）。
- ``task_change_set_service``：Task 级净变更投影与整组 Keep/Revert 用例。
- ``blob_store``：内容寻址的 before-image 对象存储（stage/publish/read）。
- ``blob_garbage_collector``：未引用对象的启动回收。
- ``file_state``：workspace 路径的精确捕获与还原原语。

包内模块各自被 ``app.service.depends`` 直接装配，不存在统一门面 re-export。
"""
