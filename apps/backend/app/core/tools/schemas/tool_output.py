"""进程隔离工具与运行期输出消费者之间的接口契约。"""

from abc import ABC, abstractmethod
from collections.abc import Callable

OutputSink = Callable[[str], None]
"""接收原始输出片段的 handler 回调。"""


class ProcessToolOutputChannel(ABC):
    """消费一次进程隔离工具调用产生的原始文本。

    后端父进程排空工作进程队列时调用 ``emit``。工具观察结果返回前调用 ``finish``，
    确保已接收文本对应的运行期事件先于工具终态事件投递。
    """

    @abstractmethod
    def emit(self, text: str) -> None:
        """接收一段原始输出文本；通道故障不得改变工具执行结果。"""

    @abstractmethod
    def finish(self) -> None:
        """投递完已接收的文本片段并释放本次调用的资源。"""


class ProcessToolOutputChannelFactory(ABC):
    """为受支持的进程工具调用创建输出通道。"""

    @abstractmethod
    def create(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_call_id: str,
        tool_name: str,
    ) -> ProcessToolOutputChannel | None:
        """在调用方事件循环线程创建输出通道；工具无运行期输出视图时返回 ``None``。"""


__all__ = ["OutputSink", "ProcessToolOutputChannel", "ProcessToolOutputChannelFactory"]
