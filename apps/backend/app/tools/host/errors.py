"""宿主调用的错误类型与链路层错误码。

错误码分两层：这里的链路层错误码由链路本身产生（宿主未连接、超时、取消、方法不存在等），由
``host_error_observation`` 统一转成工具观察；能力层错误码由各能力在宿主侧定义（如浏览器的
``TAB_NOT_FOUND``），同样以 :class:`HostCallError` 上浮，由使用它的工具自己解释。
"""

from typing import Final

UNAVAILABLE: Final = "UNAVAILABLE"
TIMEOUT: Final = "TIMEOUT"
CANCELLED: Final = "CANCELLED"
METHOD_NOT_FOUND: Final = "METHOD_NOT_FOUND"
INVALID_PARAMS: Final = "INVALID_PARAMS"
INTERNAL: Final = "INTERNAL"


class HostCallError(Exception):
    """一次宿主调用的失败；``code`` 是稳定的错误码，``message`` 是宿主给出的说明。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
