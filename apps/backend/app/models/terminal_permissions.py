"""终端权限的 Run 级持久化模式；单条命令的沙箱升级是 ``execute_terminal`` 的参数，不改变文件工具权限。"""

from typing import Literal

TerminalPermissionMode = Literal["request_approval", "full_access"]
