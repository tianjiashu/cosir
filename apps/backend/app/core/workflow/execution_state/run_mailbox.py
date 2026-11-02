"""Task 级的投递通道：进入一次执行的全部输入，在下一次模型请求前取光。"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable
from typing import cast

from app.core.workflow.message_queue.task_message import NoticeBody, TaskMessage


class RunMailbox:
    """进入 Run 的唯一通道，不是事实源：由 Task 的 actor 持有，每次开始执行时 :meth:`reset`，不跨重启。

    两种条目，按有没有 ``id`` 区分：

    - 外部队列行的副本（:meth:`post`，有 ``id``）：事实仍在外部队列，``prepare_input`` 并入时才
      确认删除；执行结束、取消或进程退出时未确认的留在外部队列，下次执行开始时重新投递。别的 Task、
      工具与 HTTP 交来的输入一律先进外部队列，再由调度方投到这里。
    - 本次执行自己产生的通知（:meth:`notify`，没有 ``id``）：工具准入、旧终端失效、续写提示只对
      这次执行的下一次请求有意义，不落库、不需要确认，随 :meth:`reset` 丢弃。只收 ``NoticeBody``，
      所以执行内无法绕过外部队列投递别的输入。

    挂起判定看 :meth:`has_wakeable`（消息体的 ``WAKES``，与调度规则同一条），不看是否为空：通知
    不论哪种来源都不值得为它推进。工具产出的图片不经这里：它是调用结果的一部分，要紧跟本批 tool
    消息（见 ``write_tool_images``）。

    出队按消息体的固定优先级（``PRIORITY``），同优先级按投递顺序：用户输入、后台结果与决定之间不
    重排，保持对话时间线。通知由 prepare_input 经 :meth:`take_notices` 一次取光、合成一条写入；
    需要绕过模型直接执行的决定经 :meth:`take_first` 先挑出来——这条判定要查工具注册表，不放在这里。

    派发方在事件循环线程投递，``prepare_input`` 在工作线程取出，因此用锁保护；取出不阻塞。同一行
    在一次执行内只收一次：派发方每次都按外部队列全量投递，去重放在这里。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (优先级, 投递序号, 消息)：序号保证同优先级先进先出，且不比较消息本身。
        self._heap: list[tuple[int, int, TaskMessage]] = []
        self._order = itertools.count()
        # 本次执行已收过的外部队列行 id。
        self._seen: set[int] = set()

    def reset(self) -> None:
        """开始一次新执行：丢弃残留消息（含没写入的执行内通知）与去重记录。"""
        with self._lock:
            self._heap.clear()
            self._seen.clear()

    def post(self, message: TaskMessage) -> None:
        """投递一条外部队列行的副本；同一行在本次执行内只收一次。"""
        row_id = cast(int, message.id)
        with self._lock:
            if row_id in self._seen:
                return
            self._seen.add(row_id)
            self._push(message)

    def notify(self, notice: NoticeBody) -> None:
        """投递一条本次执行产生的通知（没有队列行，不需要确认）。"""
        with self._lock:
            self._push(TaskMessage.of(notice))

    def take_notices(self) -> list[TaskMessage]:
        """按投递顺序取出全部通知（两种来源都在内），其余消息顺序不变。"""
        with self._lock:
            notices = sorted(entry for entry in self._heap if entry[2].kind == "system_notice")
            if notices:
                self._heap = [entry for entry in self._heap if entry[2].kind != "system_notice"]
                heapq.heapify(self._heap)
            return [entry[2] for entry in notices]

    def take_first(self, predicate: Callable[[TaskMessage], bool]) -> TaskMessage | None:
        """按出队顺序取出第一条满足条件的消息，其余消息顺序不变；没有时返回 ``None``。"""
        with self._lock:
            for entry in sorted(self._heap):
                if predicate(entry[2]):
                    self._heap.remove(entry)
                    heapq.heapify(self._heap)
                    return entry[2]
            return None

    def pop(self) -> TaskMessage:
        with self._lock:
            return heapq.heappop(self._heap)[2]

    def isEmpty(self) -> bool:
        with self._lock:
            return not self._heap

    def has_wakeable(self) -> bool:
        """是否有值得为它推进的消息（``WAKES``）；挂起前据此决定直接继续还是挂起。"""
        with self._lock:
            return any(type(entry[2].body).WAKES for entry in self._heap)

    def _push(self, message: TaskMessage) -> None:
        heapq.heappush(self._heap, (type(message.body).PRIORITY, next(self._order), message))
