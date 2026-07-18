"""榛樿鐨勩€佺函鏂囨湰 ReAct 椋庢牸宸ヤ綔娴佺瓥鐣ャ€?"""

from dataclasses import dataclass
import json
from typing import AsyncIterator, List, Optional

from app.events.types import EventType, RuntimeEvent
from app.models.base import RuntimeMessage
from app.core.runtime.operations import RuntimeOperations
from app.storage.records import StepRecord, TaskRecord
from app.tools.schemas import ToolCall, ToolObservation
from app.core.workflows.step_controller import LangGraphStepController


@dataclass
class ReactLikeState:
    """杩借釜涓€娆?ReAct 椋庢牸宸ヤ綔娴佽繍琛岀殑銆佸彲鍙樼殑杩愯鐘舵€併€?

    鍙傛暟:
        messages: 涓烘ā鍨嬭皟鐢ㄧ疮绉殑杩愯鏃舵秷鎭€?
        step_count: 宸茬粡灏濊瘯杩囩殑妯″瀷姝ラ鏁般€?
        tool_error_count: 鏈杩愯鐪嬪埌鐨勮繛缁伐鍏烽敊璇暟銆?
        requested_tool: 褰撳墠妯″瀷姝ラ鏄惁璇锋眰浜嗗伐鍏枫€?
        final_response: 褰撳墠妯″瀷姝ラ鏄惁浜х敓浜嗘渶缁堝搷搴斻€?
        terminal: 宸ヤ綔娴佹槸鍚﹀埌杈句簡缁堟€併€?

    杩斿洖:
        涓€娆″伐浣滄祦杩愯鐨勫彲鍙樼姸鎬佸€笺€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    messages: List[RuntimeMessage]
    step_count: int = 0
    tool_error_count: int = 0
    requested_tool: bool = False
    final_response: bool = False
    terminal: bool = False


class ReactLikeWorkflow:
    """杩愯绗竴涓函鏂囨湰鐨?ReAct 椋庢牸宸ヤ綔娴併€?"""

    def __init__(self, step_controller: Optional[LangGraphStepController] = None) -> None:
        """鍒濆鍖栧伐浣滄祦鎺у埗渚濊禆椤广€?

        鍙傛暟:
            step_controller: 鐢ㄤ簬宸ヤ綔娴佹楠ゅ喅瀹氱殑鍙€夋帶鍒跺櫒銆?

        杩斿洖:
            鏃犮€?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鍦ㄥ彲鐢ㄦ椂缂栬瘧榛樿鐨?LangGraph 姝ラ鎺у埗鍣ㄣ€?
        """

        self._step_controller = step_controller or LangGraphStepController()

    async def run(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
    ) -> AsyncIterator[RuntimeEvent]:
        """閫氳繃榛樿鐨?ReAct 椋庢牸绛栫暐杩愯涓€涓换鍔°€?

        鍙傛暟:
            task: 鐢辫繍琛屾椂閫変腑鐨勪换鍔¤褰曘€?
            operations: 闈㈠悜鐘舵€併€佷簨浠躲€佹ā鍨嬪拰宸ュ叿鐨勩€佽繍琛屾椂鎷ユ湁鐨勬搷浣滈棬闈€?

        鐢熸垚:
            鎻忚堪妯″瀷澧為噺銆佸伐鍏锋椿鍔ㄤ笌缁堟€佺殑杩愯鏃朵簨浠躲€?

        寮傚父:
            RuntimeError: 濡傛灉妯″瀷娴佸湪娌℃湁宸ュ叿璋冪敤鎴栨渶缁堣緭鍑虹殑鎯呭喌涓嬬粨鏉熴€?

        鍓綔鐢?
            鏇存柊浠诲姟/姝ラ鐘舵€併€佽皟鐢ㄦā鍨嬮€傞厤鍣ㄣ€侀€氳繃璋冨害鍣ㄦ墽琛屽伐鍏凤紝骞惰褰曡繍琛屾椂浜嬩欢銆?
        """

        turn = operations.get_turn_for_task(task.task_id)
        state = ReactLikeState(messages=operations.build_messages(task))

        while True:
            decision = await self._step_controller.next_step(
                step_count=state.step_count,
                max_steps=operations.settings.max_steps,
            )
            state.step_count = decision.step_count
            if not decision.can_continue:
                break

            model_step = operations.create_step(
                turn_id=turn.turn_id,
                step_type="model_call",
                status="running",
                input_summary=f"model step {state.step_count}",
            )
            yield operations.record_event(
                EventType.STEP_STARTED,
                task.task_id,
                {"step_type": "model_call", "step_index": state.step_count},
            )

            async for event in self._run_model_step(task, operations, model_step, state):
                yield event

            if state.terminal:
                return
            if state.requested_tool:
                continue
            if state.final_response:
                return

            operations.update_step(
                model_step.step_id,
                "failed",
                error="invalid_model_output",
            )
            raise RuntimeError("invalid_model_output")

        operations.log_exception(
            "task_failed",
            extra={
                "msg": f"杈惧埌鏈€澶ф鏁颁笂闄愶紝浠诲姟鎵ц澶辫触锛宼ask_id={task.task_id}",
                "data": {
                    "task_id": task.task_id,
                    "reason": "max_steps_reached",
                    "max_steps": operations.settings.max_steps,
                },
            },
        )
        operations.update_task_status(task.task_id, "failed")
        operations.close_running_steps(task.task_id, "failed", "max_steps_reached")
        yield operations.record_event(
            EventType.RUN_FAILED,
            task.task_id,
            {"status": "failed", "error": "max_steps_reached"},
        )

    async def _run_model_step(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """杩愯涓€涓ā鍨嬫楠ゅ苟鍒嗗彂妯″瀷澧為噺銆?

        鍙傛暟:
            task: 姝ｅ湪鎵ц鐨勪换鍔°€?
            operations: 杩愯鏃舵搷浣滈棬闈€?
            model_step: 鏈杩唬鐨勩€佹寔涔呭寲鐨勬ā鍨嬫楠よ褰曘€?
            state: 闇€瑕佹洿鏂扮殑鍙彉宸ヤ綔娴佺姸鎬併€?

        鐢熸垚:
            鐢辨ā鍨嬭緭鍑恒€佸伐鍏疯皟鐢ㄦ垨鏈€缁堝搷搴斾骇鐢熺殑杩愯鏃朵簨浠躲€?

        寮傚父:
            RuntimeError: 濡傛灉妯″瀷閫傞厤鍣ㄥ湪娴佸紡浼犺緭鏃跺け璐ャ€?

        鍓綔鐢?
            娴佸紡浼犺緭妯″瀷杈撳嚭锛屽苟鍙兘鎵ц宸ュ叿鎴栧畬鎴愪换鍔°€?
        """

        state.requested_tool = False
        state.final_response = False
        tool_calls: List[ToolCall] = []
        yield operations.record_event(
            EventType.MODEL_REQUESTED,
            task.task_id,
            {"step_id": model_step.step_id, "step_index": state.step_count},
        )

        async for delta in operations.stream_model(state.messages):
            if operations.has_task_status(task.task_id, "cancelled"):
                operations.close_running_steps(task.task_id, "cancelled", "run_cancelled")
                yield operations.record_event(
                    EventType.RUN_CANCELLED,
                    task.task_id,
                    {"status": "cancelled"},
                )
                state.terminal = True
                return

            if delta.text:
                yield operations.record_event(
                    EventType.MODEL_OUTPUT_DELTA,
                    task.task_id,
                    {"delta": delta.text},
                )

            if delta.tool_call is not None:
                state.requested_tool = True
                tool_calls.append(delta.tool_call)
                continue

            if delta.is_final:
                yield operations.record_event(
                    EventType.MODEL_COMPLETED,
                    task.task_id,
                    {"step_id": model_step.step_id, "step_index": state.step_count},
                )
                if tool_calls:
                    async for event in self._handle_tool_calls(
                        task, operations, model_step, tool_calls, state
                    ):
                        yield event
                    return
                state.final_response = True
                async for event in self._handle_final_response(task, operations, model_step):
                    yield event
                state.terminal = True
                return

        if tool_calls:
            yield operations.record_event(
                EventType.MODEL_COMPLETED,
                task.task_id,
                {"step_id": model_step.step_id, "step_index": state.step_count},
            )
            async for event in self._handle_tool_calls(
                task, operations, model_step, tool_calls, state
            ):
                yield event

    async def _handle_tool_calls(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        tool_calls: List[ToolCall],
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """鎵ц鍚屼竴妯″瀷鍝嶅簲涓殑涓€缁勫伐鍏疯皟鐢ㄥ苟淇濈暀鍏惰緭鍏ラ『搴忋€?

        鍙傛暟:
            task: 姝ｅ湪杩愯鐨勪换鍔°€?
            operations: Runtime 宸ュ叿鎵ц闂ㄩ潰銆?
            model_step: 浜х敓鏈粍璋冪敤鐨勬ā鍨嬫楠ゃ€?
            tool_calls: 宸茶仛鍚堢殑妯″瀷宸ュ叿璋冪敤銆?
            state: 闇€瑕佸洖濉娴嬫秷鎭殑宸ヤ綔娴佺姸鎬併€?

        鐢熸垚:
            宸ュ叿璇锋眰銆佸紑濮嬨€佸畬鎴愩€佸鎵瑰拰瑙傛祴浜嬩欢銆?

        寮傚父:
            鏃犮€傚崟璋冪敤閿欒鐢?Tool Runtime 褰掍竴鍖栥€?

        鍓綔鐢?
            鍒涘缓宸ュ叿姝ラ銆佽皟鐢ㄥ苟鍙戝伐鍏锋墽琛岄摼骞舵洿鏂颁换鍔＄姸鎬併€?
        """

        operations.update_step(model_step.step_id, "completed", output_summary="tool_calls")
        observations = operations.execute_tools(tool_calls, model_step.step_id)
        tool_group_id = f"{model_step.step_id}:tools"
        for index, (call, observation) in enumerate(zip(tool_calls, observations)):
            tool_call_id = observation.tool_call_id or call.call_id
            yield operations.record_event(
                EventType.TOOL_CALL_REQUESTED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )
            yield operations.record_event(
                EventType.TOOL_CALL_STARTED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": call.tool_name,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )
        for index, (call, observation) in enumerate(zip(tool_calls, observations)):
            tool_call_id = observation.tool_call_id or call.call_id
            yield operations.record_event(
                EventType.TOOL_CALL_FINISHED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": observation.tool_name,
                    "status": observation.status,
                    "error": observation.error,
                    "artifact_id": observation.artifact_id,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )

            if self._tool_error_limit_reached(observation, state, operations):
                operations.update_task_status(task.task_id, "failed")
                operations.close_running_steps(task.task_id, "failed", "tool_error_limit_reached")
                yield operations.record_event(
                    EventType.RUN_FAILED,
                    task.task_id,
                    {"status": "failed", "error": "tool_error_limit_reached", "tool_name": observation.tool_name},
                )
                state.terminal = True
                return
            self._append_tool_exchange(state, model_step, call, observation)
            yield operations.record_event(EventType.OBSERVATION_ADDED, task.task_id, {"tool_name": observation.tool_name, "status": observation.status})

    async def _handle_tool_call(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        tool_call: ToolCall,
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """鎵ц妯″瀷璇锋眰鐨勪竴涓伐鍏疯皟鐢ㄣ€?

        鍙傛暟:
            task: 姝ｅ湪鎵ц鐨勪换鍔°€?
            operations: 杩愯鏃舵搷浣滈棬闈€?
            model_step: 浜х敓璇ュ伐鍏疯皟鐢ㄧ殑妯″瀷姝ラ銆?
            tool_call: 妯″瀷璇锋眰鐨勫伐鍏疯皟鐢ㄣ€?
            state: 闇€瑕佺敤瑙傛祴缁撴灉鏇存柊鐨勫彲鍙樺伐浣滄祦鐘舵€併€?

        鐢熸垚:
            鐢ㄤ簬宸ュ叿璇锋眰銆佹墽琛屼笌瑙傛祴缁撴灉鐨勮繍琛屾椂浜嬩欢銆?

        寮傚父:
            鏃犮€傚伐鍏峰け璐ヤ細琚綊涓€鍖栦负瑙傛祴缁撴灉銆?

        鍓綔鐢?
            閫氳繃 RuntimeOperations 鎵ц涓€涓伐鍏峰苟鏇存柊鎸佷箙鍖栫殑姝ラ銆?
        """

        operations.update_step(
            model_step.step_id,
            "completed",
            output_summary=f"tool_call:{tool_call.tool_name}",
        )

        tool_step = operations.create_step(
            turn_id=model_step.turn_id,
            step_type="tool_call",
            status="running",
            input_summary=tool_call.tool_name,
        )

        observation = operations.execute_tool(tool_call, tool_step.step_id)
        tool_call_id = observation.tool_call_id or tool_call.call_id
        tool_group_id = f"{model_step.step_id}:tools"
        yield operations.record_event(
            EventType.TOOL_CALL_REQUESTED,
            task.task_id,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.arguments,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )
        yield operations.record_event(
            EventType.TOOL_CALL_STARTED,
            task.task_id,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_call.tool_name,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )
        operations.update_step(
            tool_step.step_id,
            "completed" if observation.status == "success" else "failed",
            output_summary=observation.content[:200],
            error=observation.error or None,
        )
        yield operations.record_event(
            EventType.TOOL_CALL_FINISHED,
            task.task_id,
            {
                "tool_name": observation.tool_name,
                "tool_call_id": tool_call_id,
                "status": observation.status,
                "error": observation.error,
                "artifact_id": observation.artifact_id,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )



        if self._tool_error_limit_reached(observation, state, operations):
            operations.log_exception(
                "task_failed",
                extra={
                    "msg": f"杩炵画宸ュ叿閿欒杈惧埌涓婇檺锛屼换鍔″け璐ワ紝task_id={task.task_id}锛宼ool_name={observation.tool_name}",
                    "data": {
                        "task_id": task.task_id,
                        "reason": "tool_error_limit_reached",
                        "tool_name": observation.tool_name,
                        "limit": operations.settings.tool_error_limit,
                    },
                },
            )
            operations.update_task_status(task.task_id, "failed")
            operations.close_running_steps(task.task_id, "failed", "tool_error_limit_reached")
            yield operations.record_event(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "tool_error_limit_reached",
                    "tool_name": observation.tool_name,
                },
            )
            state.terminal = True
            return

        self._append_tool_exchange(state, model_step, tool_call, observation)
        yield operations.record_event(
            EventType.OBSERVATION_ADDED,
            task.task_id,
            {"tool_name": observation.tool_name, "status": observation.status},
        )

    async def _handle_final_response(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
    ) -> AsyncIterator[RuntimeEvent]:
        """鍦ㄦā鍨嬪彂鍑烘渶缁堝搷搴斾箣鍚庡畬鎴愪换鍔°€?

        鍙傛暟:
            task: 姝ｅ湪鎵ц鐨勪换鍔°€?
            operations: 杩愯鏃舵搷浣滈棬闈€?
            model_step: 鍙戝嚭鏈€缁堝搷搴旂殑妯″瀷姝ラ銆?

        鐢熸垚:
            鏈€缁堝搷搴斾笌杩愯缁撴潫浜嬩欢銆?

        寮傚父:
            KeyError: 濡傛灉浠诲姟鎴栨楠ょ姸鎬佹棤娉曡鏇存柊銆?

        鍓綔鐢?
            灏嗘ā鍨嬫楠や笌浠诲姟鏍囪涓哄凡瀹屾垚銆?
        """

        operations.update_step(
            model_step.step_id,
            "completed",
            output_summary="final_response",
        )
        yield operations.record_event(
            EventType.FINAL_RESPONSE,
            task.task_id,
            {"status": "completed"},
        )
        operations.update_task_status(task.task_id, "completed")
        yield operations.record_event(
            EventType.RUN_FINISHED,
            task.task_id,
            {"status": "completed"},
        )

    def _tool_error_limit_reached(
        self,
        observation: ToolObservation,
        state: ReactLikeState,
        operations: RuntimeOperations,
    ) -> bool:
        """鏇存柊宸ュ叿閿欒璁℃暟骞惰繑鍥炴槸鍚﹁揪鍒颁笂闄愩€?

        鍙傛暟:
            observation: 鐢辫皟搴﹀櫒杩斿洖鐨勫伐鍏疯娴嬬粨鏋溿€?
            state: 鍖呭惈褰撳墠閿欒璁℃暟鐨勫彲鍙樺伐浣滄祦鐘舵€併€?
            operations: 鍖呭惈鎵€閰嶇疆闄愬埗鐨勮繍琛屾椂鎿嶄綔闂ㄩ潰銆?

        杩斿洖:
            褰撹繛缁伐鍏烽敊璇揪鍒版墍閰嶇疆鐨勪笂闄愭椂涓?True銆?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            淇敼 ``state.tool_error_count``銆?
        """

        if observation.status == "error":
            state.tool_error_count += 1
        else:
            state.tool_error_count = 0
        return state.tool_error_count >= operations.settings.tool_error_limit

    def _append_tool_exchange(
        self,
        state: ReactLikeState,
        model_step: StepRecord,
        tool_call: ToolCall,
        observation: ToolObservation,
    ) -> None:
        """杩藉姞鍔╂墜宸ュ叿璋冪敤涓庡伐鍏疯娴嬫秷鎭€?

        鍙傛暟:
            state: 鍖呭惈妯″瀷娑堟伅鐨勫彲鍙樺伐浣滄祦鐘舵€併€?
            model_step: 浜х敓璇ュ伐鍏疯皟鐢ㄧ殑妯″瀷姝ラ銆?
            tool_call: 妯″瀷璇锋眰鐨勫伐鍏疯皟鐢ㄣ€?
            observation: 闇€瑕佸姞鍏ユā鍨嬩笂涓嬫枃鐨勫伐鍏疯娴嬬粨鏋溿€?

        杩斿洖:
            鏃犮€?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鍚?``state.messages`` 杩藉姞鍔╂墜涓庡伐鍏锋秷鎭€?
        """

        tool_call_id = tool_call.call_id or f"local_{model_step.step_id}"
        arguments_json = json.dumps(tool_call.arguments, ensure_ascii=False, sort_keys=True)
        state.messages.append(
            RuntimeMessage(
                role="assistant",
                content_text="",
                metadata={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_call.tool_name,
                    "tool_arguments_json": arguments_json,
                },
            )
        )
        state.messages.append(
            RuntimeMessage(
                role="tool",
                content_text=observation.content or observation.error,
                metadata={
                    "tool_call_id": tool_call_id,
                    "tool_name": observation.tool_name,
                    "status": observation.status,
                },
            )
        )
