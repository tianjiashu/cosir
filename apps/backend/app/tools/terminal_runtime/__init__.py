"""终端运行时：用户打开的交互终端与 ``execute_terminal`` 命令的执行底座，不依赖任何工具 handler。

按能力域分子包，依赖自上而下单向（``tests/test_terminal_runtime_import_boundary.py`` 守卫）：

- ``session/``：用户打开的交互终端——会话集合（``session_service``）、单会话（``session_runtime``）、
  每会话 ``terminal-v2`` 交互通道（``stream``）与交互 shell 解析（``shell_resolver``）；
- ``command/``：``execute_terminal`` 的一条命令——执行后端（``local_backend``）、输出收集（解码 →
  整理 → ``command_output``）、结局（``execution_result``）与运行期间可读的 ``running_command``；
- ``observation/``：会话与命令共用的读取模型（纯内存）——``line_buffer`` → ``terminal_observation``
  → ``wait`` / ``render``，读取底座 ``observed_terminal``；
- 根：两种模式共用的 Terminal Worker 进程边界 ``worker_client`` 与领域错误 ``errors``。

包导入不装配会话服务，避免契约模块引入工具与运行时循环。
"""
