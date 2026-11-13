"""一条终端命令作业的结论交付与撤销。

作业跑在命令的作业线程里，只承诺两件事：结论到达时回调恰好一次，以及可以被撤销。结果登记与回投归
``command_results``，前台等待和转后台由 ``ExecuteTerminalTool`` 编排。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from app.config.logging.logger import log


@dataclass(frozen=True)
class CommandConclusion:
    """作业的结论：给模型的 ``result`` 与可选的卡片终态 ``display_data``。

    两者分开是因为受众不同：``result`` 以 ``background_result`` 回投给模型（会过输出预算），
    ``display_data`` 只在结论并入时写回后台占位卡片（UI 展示契约，不进模型、不截断）。结论只走一条
    路径（作业 → 登记表 → 消息），所以卡片数据随结论一起走，不另开通道。没有卡片终态的作业
    留 ``None``，占位沿用原展示数据。
    """

    result: dict[str, object]
    display_data: dict[str, object] | None = None
    cancel_hint: str | None = None


JobCallback = Callable[[CommandConclusion], None]


class CommandJob:
    """在自己线程里跑的一条命令：结论恰好交付一次，可撤销。

    撤销只唤醒执行后端停下命令，结论仍由命令自己返回（带上被停止前的部分输出）。框架不代结：它先交付
    会让前台等待方拿到结论，而执行结果还没写回。
    """

    def __init__(self, wake: Callable[[], None]) -> None:
        self._wake = wake
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._conclusion: CommandConclusion | None = None
        self._callbacks: list[JobCallback] = []

    @classmethod
    def start_in_thread(
        cls, work: Callable[[], CommandConclusion], *, wake: Callable[[], None], name: str,
    ) -> CommandJob:
        """把阻塞函数 ``work`` 放到自己的线程里执行；``wake`` 在撤销时调用、负责停下命令。线程名用于排查。"""
        job = cls(wake)
        threading.Thread(target=job._run, args=(work,), name=name, daemon=True).start()
        return job

    def on_done(self, callback: JobCallback) -> None:
        """注册结论回调：结论（含被撤销后的取消结论）到达时调用恰好一次。

        已经有结论时立即在调用方线程回调，所以调用方不得持有回调会再取的锁。回调可能写 SQLite。
        """
        with self._lock:
            if self._conclusion is None:
                self._callbacks.append(callback)
                return
            conclusion = self._conclusion
        callback(conclusion)

    def wait(self, timeout: float | None) -> CommandConclusion | None:
        """等结论：拿到返回它，超时返回 ``None``。

        ``timeout=None`` 时等到结论为止：撤销之后这一等有上限，执行后端对任何停止请求只限时等收尾
        （``LocalExecutionBackend``），所以撤销方不另设时限。不触发消息投递，不在事件循环线程调用。"""
        if not self._done.wait(timeout):
            return None
        return self._conclusion

    def cancel(self) -> None:
        """撤销：唤醒执行后端停下命令；结论仍经 :meth:`on_done` 交付。幂等（后端只认第一次停止请求）。"""
        self._wake()

    def _run(self, work: Callable[[], CommandConclusion]) -> None:
        try:
            conclusion = work()
        except Exception as exc:
            log.exception("terminal_command_job_failed", extra={
                "msg": "终端命令作业异常", "data": {"thread": threading.current_thread().name},
            })
            conclusion = CommandConclusion({"end_reason": "failed", "error": str(exc)})
        with self._lock:
            self._conclusion = conclusion
            callbacks, self._callbacks = self._callbacks, []
        self._done.set()
        # 作业线程的顶层边界：一个回调失败只记录，不挡住其余回调，也不只留在 stderr。
        for callback in callbacks:
            try:
                callback(conclusion)
            except Exception:
                log.exception("terminal_command_job_callback_failed", extra={
                    "msg": "终端命令结论回调失败",
                    "data": {"thread": threading.current_thread().name},
                })
