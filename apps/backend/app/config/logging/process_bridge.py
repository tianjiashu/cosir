"""跨进程日志桥接：让 spawn 子进程日志汇入父进程文件管线。

设计要点：
- 子进程通过 SubprocessQueueHandler 把 LogRecord 放入 multiprocessing.Queue，
  发送前会把不可 pickle 的 exc_info（traceback 对象）转换为 stack 文本，避免跨进程失败。
- 父进程 QueueListener 在固定 JSONL 文件 handler 上消费，复用父进程已配置的
  截断与上下文关联，不重复创建写入管线。
- 子进程创建日志记录时，当前上下文中的 trace_id 会随 LogRecord 跨队列传递；
  业务实体 ID（如 run_id、task_id）应放在 extra.data 中，不由日志层反查。

契约：
- 子进程侧调用 install_logging_for_current_process(log_queue=queue)，把日志导向队列；
- 子进程日志的 extra 字段必须可 pickle；
- 父进程侧由 install_logging_for_current_process 统一分发：无队列时走 configure_logging，
  并调用 install_log_queue_bridge 启动监听器。
"""

import logging
import multiprocessing
import traceback
from logging.handlers import QueueHandler, QueueListener
from multiprocessing.queues import Queue

from app.models.mapped_log_record import MappedLogRecord

_LOG_QUEUE: Queue | None = None
_QUEUE_LISTENER: QueueListener | None = None


def create_log_queue() -> Queue:
    """创建跨进程日志传输队列。

    参数:
        无。

    返回:
        可供父进程监听、子进程写入的 multiprocessing 队列。

    异常:
        无。

    副作用:
        创建一个进程间队列。
    """
    return multiprocessing.Queue(-1)


def install_queue_handler(queue: Queue) -> None:
    """在子进程侧把 coding_agent.backend 日志导向跨进程队列。

    参数:
        queue: 父进程创建的日志队列。

    返回:
        无。

    异常:
        无。安装失败由调用方兜底（退回 stderr）。

    副作用:
        给子进程 coding_agent.backend logger 增加 SubprocessQueueHandler，
        并关闭向上传播以免重复输出到 stderr。
    """
    logger = logging.getLogger("coding_agent.backend")
    logger.propagate = False
    if any(isinstance(handler, SubprocessQueueHandler) for handler in logger.handlers):
        return
    logger.addHandler(SubprocessQueueHandler(queue))


def install_log_queue_bridge(logger: logging.Logger) -> None:
    """在父进程侧启动队列监听器，复用已有 handler 消费子进程日志。

    参数:
        logger: 已挂载固定 JSONL 文件 handler 的后端主日志器。

    返回:
        无。

    异常:
        无。

    副作用:
        创建日志队列并启动 QueueListener；覆盖上一次桥接（幂等）。
    """
    global _LOG_QUEUE, _QUEUE_LISTENER
    if _QUEUE_LISTENER is not None:
        _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None
    queue = create_log_queue()
    listener = start_queue_listener(queue, list(logger.handlers))
    _LOG_QUEUE = queue
    _QUEUE_LISTENER = listener


def start_queue_listener(queue: Queue, handlers: list[logging.Handler]) -> QueueListener:
    """启动队列监听器。

    参数:
        queue: 日志队列。
        handlers: 父进程已配置好的 JSONL 文件 handler 列表。

    返回:
        已启动的 QueueListener。

    异常:
        无。

    副作用:
        启动一个监听线程持续消费队列。
    """
    listener = QueueListener(queue, *handlers, respect_handler_level=True)
    listener.start()
    return listener


def stop_queue_listener() -> None:
    """停止监听器并 flush 残留日志。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        停止监听线程。QueueListener.stop 会在入队哨兵前消费完队列中已有记录，
        因此正常关闭流程（父进程已 join 子进程、子进程不再写入）下不会丢失日志；
        随后清空进程内队列引用。
    """
    global _LOG_QUEUE, _QUEUE_LISTENER
    if _QUEUE_LISTENER is not None:
        _QUEUE_LISTENER.stop()
        _QUEUE_LISTENER = None
    _LOG_QUEUE = None


def get_log_queue() -> Queue | None:
    """返回当前父进程日志队列，供子进程入口使用。

    参数:
        无。

    返回:
        已创建的日志队列；未配置时返回 None。

    异常:
        无。

    副作用:
        无。
    """
    return _LOG_QUEUE


class SubprocessQueueHandler(QueueHandler):
    """子进程侧队列 handler，发送前剥离不可 pickle 的 traceback。"""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """在入队前把 exc_info 转换为可 pickle 的 stack 文本。

        参数:
            record: 待入队的日志记录。

        返回:
            已清除 exc_info 的日志记录副本；无 exc_info 时返回原 record。

        异常:
            无。

        副作用:
            无；不修改原始 record。
        """
        cloned = logging.makeLogRecord(record.__dict__.copy())
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            try:
                stack_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            except Exception:
                # traceback 格式化失败时降级为可读占位，避免静默丢失堆栈
                stack_text = (
                    f"<stack unavailable: {exc_type.__name__}: {exc_value}>"
                    if exc_type
                    else "<stack unavailable>"
                )
            _set_log_field(cloned, "stack", stack_text)
            _set_log_field(cloned, "error_type", exc_type.__name__ if exc_type else "")
            _set_log_field(cloned, "error_message", str(exc_value) if exc_value is not None else "")
        cloned.exc_info = None
        cloned.exc_text = None
        # The JSONL formatter reads record.msg directly. Keep the event key while
        # dropping arbitrary args that may not be pickleable across spawn.
        cloned.args = ()
        return cloned


def _set_log_field(record: logging.LogRecord, name: str, value: str) -> None:
    """把结构化错误字段写入 record，供父进程 formatter 回退读取。

    参数:
        record: 待写入的日志记录。
        name: 字段名。
        value: 字段值。

    返回:
        无。

    异常:
        无。

    副作用:
        给 record 增加属性。
    """
    if value:
        setattr(record, name, MappedLogRecord._truncate_text(value))
