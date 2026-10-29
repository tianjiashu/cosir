"""交互终端运行时：外部终端工具的执行底座。

按层组织（依赖自上而下单向）：
- 会话协调：``session_service``（会话集合）、``session_runtime``（单会话）、``session_effects``
  （对应用层的副作用）、``stream``（每会话 ``terminal-v2`` 交互通道）；
- 观察模型（纯内存）：``line_buffer`` → ``observation`` → ``wait`` / ``render``；
- worker 进程边界：``worker_client``、``shell_resolver``；
- 契约：``inputs``（输入与等待参数）、``errors``。

包导入不装配会话服务，避免契约模块引入工具与运行时循环。
"""
