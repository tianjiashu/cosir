"""轮次事件流编排 service。

单一职责：把一次轮次运行编排成可供传输层消费的运行时事件流——订阅事件总线、
认领 producer、驱动运行时产出事件并发布、断连兜底落 failed、纯订阅转发。

职责边界：
- 负责：事件流的订阅/认领/转发、producer 生命周期、断连兜底落 failed、
  run_failed 终态补发（run 未启动即断开时）。
- 不负责：SSE/HTTP 帧格式化（由 api 层把裸事件格式化为传输帧）、运行时具体执行
  （由注入的 ``TurnRunner`` 提供）。

依赖倒置：本 service 不 import ``core.AgentRuntime``（维持 core → service 单向依赖），
而是依赖 ``TurnRunner`` 协议，由接入层把 ``runtime.run_turn`` 适配后注入。
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress

from app.config.logging.logger import log
from app.models import TurnRecord
from app.models.enums.event_type import TERMINAL_EVENT_TYPES, EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.run_failed_payload import RunFailedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.service.task.turn_service import TurnService

# 轮次执行器协议：service 依赖此抽象而非 core.AgentRuntime，维持 core → service 单向依赖。
# 返回 ``AsyncGenerator`` 而非 ``AsyncIterator``，因为 producer 需要 ``aclose()``
# 确定性关闭底层生成器以触发断开兜底（与 ``runtime.run_turn`` 的实际签名一致）。
TurnRunner = Callable[[TurnRecord], Awaitable[AsyncGenerator[RuntimeEvent] | None]]


class TurnStreamService:
    """编排单轮运行时事件的流式产出（订阅/认领/驱动/发布/兜底）。

    构造注入三个稳定协作者（事件总线 / 轮次 service / 运行时事件 service），
    方法只接收随请求变化的 ``runner`` 与 ``turn``，稳定依赖不在调用点透传。
    """

    def __init__(
        self,
        event_bus: RuntimeEventBus,
        turn_service: TurnService,
        runtime_event_service: RuntimeEventService,
    ) -> None:
        """初始化轮次事件流编排 service。

        参数:
            event_bus: 进程级运行时事件广播总线（订阅 / 认领 producer / 发布 / 关闭）。
            turn_service: 轮次 service（run 未启动即断开的兜底落 failed 用）。
            runtime_event_service: 运行时事件持久化与广播 service（run_failed 补发用）。

        返回:
            无。

        异常:
            无。

        副作用:
            持有三个稳定协作者引用，不创建任何进程级状态。
        """
        self._event_bus = event_bus
        self._turn_service = turn_service
        self._runtime_event_service = runtime_event_service

    async def stream_turn_events(
        self,
        runner: TurnRunner,
        turn: TurnRecord,
    ) -> AsyncIterator[RuntimeEvent]:
        """订阅轮次事件并认领 producer，产出运行时事件供传输层格式化。

        断开兜底（把孤儿 ``running`` 落定为 ``failed``）已下沉到 ``run_turn`` 内部，
        按「本连接是否成功认领本轮」精确判定，避免并发连接互相误标。本方法只负责：
        产出裸 ``RuntimeEvent``、记录流式生命周期日志，并在 finally 中**确定性关闭**
        底层运行生成器，从而在客户端断开时触发 ``run_turn`` 的断开兜底（而非依赖
        不确定的 GC 回收）。SSE 帧格式化由 api 层完成。

        参数:
            runner: 产生轮次事件的执行器（接入层注入的 ``runtime.run_turn`` 适配）。
            turn: 待运行的轮次记录，透传给 runner 以避免重复查询存储
                （仅 pending 轮次会被实际执行）。

        生成:
            裸 ``RuntimeEvent``，逐条产出该轮次的实时运行事件。

        异常:
            不向上抛出：``KeyError``（轮次在流式开始前消失）与其它未预期异常均在此
            记录并终止流，避免异常裸奔中断 HTTP 响应；轮次终态由 ``run_turn`` 兜底。

        副作用:
            认领 producer 并启动后台生产任务；在 finally 中关闭底层运行生成器，触发
            ``run_turn`` 的断开兜底（仅当本连接成功认领且轮次仍 ``running`` 时标记
            ``failed``），避免孤儿 ``running``。
        """
        turn_id = turn.turn_id

        log.info(
            "turn_stream_started",
            extra={
                "msg": f"开始流式推送轮次事件，turn_id={turn_id}",
                "data": {"turn_id": turn_id},
            },
        )

        subscription = self._event_bus.subscribe(turn_id)
        producer: asyncio.Task[None] | None = None
        if self._event_bus.claim_turn_producer(turn_id):
            producer = asyncio.create_task(self._drive_runtime_turn(runner, turn))
            log.info(
                "turn_stream_producer_started",
                extra={
                    "msg": f"轮次 producer 已启动，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )
        else:
            log.info(
                "turn_stream_producer_already_running",
                extra={
                    "msg": f"轮次 producer 已存在，本连接仅订阅事件，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )
        terminal_received = False
        try:
            async for event in subscription:
                yield event
                if event.event_type in TERMINAL_EVENT_TYPES:
                    terminal_received = True
                    # RUN_FINISHED 之后 run_turn 还会发布 file_change_stable（成功路径的
                    # 变更集增量通知）。若立即 break，这些事件会滞留在订阅队列无法送达
                    # 前端。故等待 producer 结束（run_turn 完全 return、事件已入队、
                    # close_turn 已写入关闭哨兵），再继续消费剩余事件至订阅关闭。
                    if event.event_type == EventType.RUN_FINISHED and producer is not None:
                        with suppress(asyncio.CancelledError):
                            await producer
                        continue
                    # RUN_FAILED / RUN_CANCELLED 之后无后续事件，保持原立即结束语义。
                    break
            log.info(
                "turn_stream_completed",
                extra={
                    "msg": f"轮次事件流式推送完成，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )
        except KeyError:
            # 防御性分支：真实事件总线订阅迭代只抛 StopAsyncIteration/TypeError，
            # 正常路径不会走到这里。保留该分支是为「轮次在流式开始前被清理、
            # 或未来总线实现变更」时，仍能记录带上下文的日志而非异常裸奔
            # （producer 侧异常由 finally 中的 producer.result() 记录为
            # turn_stream_producer_failed，两条日志不会同时出现）。
            log.exception(
                "turn_stream_aborted",
                extra={
                    "msg": f"轮次在执行前消失，流式中止，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )
        except Exception:
            # 其它未预期异常：记录后终止流，避免异常裸奔中断响应；轮次终态由 run_turn 兜底。
            log.exception(
                "turn_stream_error",
                extra={
                    "msg": f"轮次事件流式推送异常，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )
        finally:
            self._event_bus.unsubscribe(subscription)
            if producer is not None:
                if terminal_received and not producer.done():
                    with suppress(Exception):
                        await producer
                if not producer.done():
                    producer.cancel()
                    with suppress(asyncio.CancelledError):
                        await producer
                if producer.done():
                    with suppress(asyncio.CancelledError):
                        try:
                            producer.result()
                        except Exception:
                            log.exception(
                                "turn_stream_producer_failed",
                                extra={
                                    "msg": f"轮次事件生产任务异常，turn_id={turn_id}",
                                    "data": {"turn_id": turn_id},
                                },
                            )

    async def stream_subscribed_turn_events(self, turn_id: str) -> AsyncIterator[RuntimeEvent]:
        """纯订阅转发（委派子轮次实时事件推送），不认领 producer。

        参数:
            turn_id: 待订阅事件的 turn 标识。

        生成:
            逐条产生该 turn 的实时运行时事件（裸事件，传输层自行格式化）。

        异常:
            无。订阅关闭或客户端断开时正常结束生成器。

        副作用:
            注册并最终移除事件订阅；不启动、取消或落定 turn。
        """
        subscription = self._event_bus.subscribe(turn_id)
        try:
            async for event in subscription:
                yield event
        finally:
            self._event_bus.unsubscribe(subscription)

    async def _drive_runtime_turn(self, runner: TurnRunner, turn: TurnRecord) -> None:
        """驱动 runner 产出事件并发布到总线（producer 主体）。

        CodeGraph 索引保活（原 prepare 阶段）已迁移到 ``USER_PROMPT_SUBMIT`` Hook，
        在 ``run_turn`` 内部、本轮认领后、RUN_STARTED 之前触发，本方法不再做任何前置
        准备（见 CodeGraphIndexPrepareHook）。本方法只负责：启动 runner 并消费其事件
        发布到 bus，结束时释放 producer 槽位。

        参数:
            runner: 产生轮次事件的执行器（接入层注入）。
            turn: 待运行的轮次记录，透传给 runner。

        返回:
            无。

        异常:
            向上透传 runner 的未预期异常，由持有 producer 的流式层记录；
            被取消时 re-raise ``CancelledError``（兜底落 failed 后不吞）。

        副作用:
            消费 runner 事件发布到 bus；结束时释放 producer 槽位。
        """
        entered_run = False
        turn_id = turn.turn_id

        async def execute() -> None:
            """消费 runner 事件并发布到 bus（producer 主体）。

            参数:
                无（经闭包捕获 runner / turn / entered_run）。

            返回:
                无。

            异常:
                透传 runner 的未预期异常；被取消时向上抛 ``CancelledError``。

            副作用:
                ``entered_run`` 在 ``await runner(turn)`` 返回后置位（run 已真正启动，
                或 runner 早退返回 None），此后断开交由 run_turn 内部兜底；本地兜底
                只覆盖「run 未启动即被取消」的窗口。
            """
            nonlocal entered_run
            events = await runner(turn)
            # run 已真正启动（runner 已返回）：此后断开交由 run_turn 内部兜底，
            # 本地兜底只覆盖「run 未启动即被取消」的窗口。
            entered_run = True
            if events is None:
                self._event_bus.close_turn(turn_id)
                return

            try:
                async for event in events:
                    self._event_bus.publish(event)
            finally:
                try:
                    await events.aclose()
                    self._event_bus.close_turn(turn_id)
                finally:
                    self._event_bus.release_turn_producer(turn_id)

        try:
            await execute()
        except asyncio.CancelledError:
            # 本地兜底只覆盖「run 未启动即被取消」的窗口（entered_run 尚未置位）：
            # 此时 turn 仍可能为 pending，run_turn 内部的 fail_turn_if_running
            # （WHERE status="running"）对 pending 无效，需本地强制落 failed。
            # run 已启动（runner 已返回）后的断开由 run_turn 内部兜底（§九.3/4）。
            if not entered_run:
                try:
                    # 方法偏离说明（§九.4）：fail_turn_if_running 的 WHERE status="running"
                    # 原子约束对 pending 不生效（turn_crud），而本 producer 已 claim 独占
                    # （无并发认领竞态），故用无条件 update_turn_status 强制落 failed。
                    # 兜底后 emit run_failed 提供终态事件。
                    self._turn_service.update_turn_status(
                        turn_id, "failed", end_reason="client_disconnected"
                    )
                    self._emit_run_failed(turn, turn_id)
                except Exception:
                    log.exception(
                        "turn_disconnect_failed",
                        extra={
                            "msg": "run 未启动即断开落 failed 失败",
                            "data": {"turn_id": turn_id},
                        },
                    )
            raise
        finally:
            # 最外层释放 producer 槽位。
            self._event_bus.release_turn_producer(turn_id)

    def _emit_run_failed(self, turn: TurnRecord | None, turn_id: str) -> None:
        """run 未启动即断开（本地兜底路径）时发布一条 run_failed 终态事件。

        与 ``runtime.run_turn`` 内部落 failed 时的终态事件同构，经注入的
        ``RuntimeEventService`` 落库并发布，避免留下无终态事件的孤儿 turn
        （方案二 §4.2.1 / §六 验收 5）。

        参数:
            turn: 预取轮次记录（task_id 来源；为 None 时用空串）。
            turn_id: 待落终态的轮次标识。

        返回:
            无。

        异常:
            RuntimeError: storage 未初始化时抛出（由调用方兜底捕获）。

        副作用:
            向 runtime_events 表写入 run_failed 事件并广播。
        """
        task_id = turn.task_id if turn is not None else ""
        event = RuntimeEvent(
            event_type=EventType.RUN_FAILED,
            task_id=task_id,
            turn_id=turn_id,
            payload=RunFailedPayload(
                error="client_disconnected",
                status="failed",
                end_reason="client_disconnected",
            ),
        )
        self._runtime_event_service.save_and_publish(event)
