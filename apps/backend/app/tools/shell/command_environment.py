"""交给用户命令的子进程环境：唯一构造入口。

用户命令（``execute_terminal`` 的命令、交互终端的 shell）应当看到的是用户自己的环境，而不是本应用
的进程配置。后端进程环境里混着两类本应用自己的变量，各有进来的理由，在这里一处去掉：

- ``CODING_AGENT_*``：宿主与后端的契约（端口、数据目录、宿主链路 token 等），后端需要，用户命令
  不需要；被继承还会让 Agent 在 cosir 仓库里拉起的后端或测试指向正在运行的实例。
- ``.cosir/.env`` 注入的键：``os.environ`` 是配置的读取来源，所以 ``Settings`` 把文件值写了进去；
  这里复用它自己的注入记录（``is_file_loaded_value``），不另立一份清单。用户 shell 里本来就有的
  同名变量不受影响。

基底环境（PATH 等）在启动时已合并用户登录 shell 的环境，见 ``login_environment``。MCP stdio 服务
不走这里：SDK 只从进程环境继承固定白名单，本来就不会带上这两类变量。
"""

import os

from app.config.settings import Settings

HOST_VARIABLE_PREFIX = "CODING_AGENT_"


def user_command_environment() -> dict[str, str]:
    """返回当前进程环境去掉本应用变量后的副本，供用户命令子进程的 ``env`` 参数使用。"""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(HOST_VARIABLE_PREFIX) and not Settings.is_file_loaded_value(key)
    }
