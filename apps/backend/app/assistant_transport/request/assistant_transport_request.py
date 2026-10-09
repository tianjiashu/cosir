"""Assistant Transport wire schema。

本模块只描述桌面端与后端之间的 Assistant UI request 请求结构，不依赖领域模型、
LangGraph 或 storage。wire 字段遵循 assistant-ui 的 camelCase 约定；进入 service 层
后由 API 边界映射为后端 snake_case 领域参数。
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant_transport.request.command.add_message_command import AddMessageCommand
from app.assistant_transport.request.command.ban_tools_command import BanToolsCommand
from app.assistant_transport.request.command.propose_agent_configuration_command import (
    ProposeAgentConfigurationCommand,
)
from app.assistant_transport.request.command.propose_agent_team_configuration_command import (
    ProposeAgentTeamConfigurationCommand,
)
from app.assistant_transport.request.command.user_input_decision_command import (
    UserInputDecisionCommand,
)
from app.config.logging.logger import log
from app.core.tools.schemas import UserDecision

# custom 命令共用 wire ``type`` discriminator，因此每种受支持的项目命令都按明确 schema
# 定义，并通过字面量 ``name`` 区分。
AssistantCommand = (
    AddMessageCommand
    | BanToolsCommand
    | ProposeAgentConfigurationCommand
    | ProposeAgentTeamConfigurationCommand
    | UserInputDecisionCommand
)


class AssistantTransportRequest(BaseModel):
    """校验 Assistant Transport 请求。

    当前受支持的命令共同构成一次 Run 请求；对话历史由 canonical facts 重建。
    """

    # Transport 请求只接受当前契约字段；旧的状态游标不得再被静默吞掉。
    model_config = ConfigDict(extra="forbid")

    commands: list[AssistantCommand] = Field(min_length=0, max_length=32)
    # 每次 Assistant Transport 请求都绑定一个已创建的 Task；新对话由前端先创建
    # provisional Task，再使用同一个 task/thread 身份提交首条命令。
    threadId: str = Field(pattern=r"^task-[1-9][0-9]*$")
    taskId: int = Field(ge=1)
    workspaceId: int | None = Field(default=None, ge=1)
    modelConfigId: int | None = Field(default=None, ge=1)
    reasoningEffort: str | None = Field(default=None)
    # 空 commands 用于恢复指定 run；编辑重跑的 add-message 也必须携带当前 runId。
    runId: int | None = Field(default=None)

    # Assistant UI 会在请求根部附带这些通用 runtime 字段。它们属于兼容性 envelope：
    # 当前后端不使用、不持久化，但显式声明后可以安全接收未来版本的客户端请求；
    # ``extra=forbid`` 仍会拦截真正未知的字段。命令内部的 parentId 则是消息关系，
    # 由 Assistant UI 命令协议保留，不能与这里的根级 parentId 混淆。
    parentId: str | None = None
    state: object | None = None
    system: str | None = None
    tools: dict[str, object] | None = None
    callSettings: dict[str, object] | None = None
    config: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_transport_constraints(self) -> "AssistantTransportRequest":
        """在模型构造后自动校验全部纯 wire 契约约束，不涉及领域持久化状态。

        本校验器由 Pydantic 在请求解析阶段自动触发，覆盖所有可在解析期判定的结构
        性约束，与领域 service 的运行期校验（task 是否存在、run 冲突、run 占用状态）
        严格分离：

        - ``reasoningEffort`` 必须在允许集合 ``{low, high, max}`` 内；
        - ``commands`` 内 ``commandId`` 必须唯一；
        - ``threadId`` 必须与 ``task-{taskId}`` 一致，二者是同一领域身份的两种表达；
        - 一次请求最多包含一个 ``add-message`` 命令（首版运行模型不支持批量消息）；
        - custom 命令必须是与 add-message 同批的工具禁用或配置草稿提案命令；
        - ``UserInputDecisionCommand``（human-in-the-loop 决定）必须单独提交、携带
          ``runId``，且一次请求最多一条；
        - 空命令必须携带 ``runId`` 用于恢复已有 run；add-message 是否重放只由
          ``runId`` 是否存在决定，不能由 ``sourceId`` 推导；
        - 含 ``add-message`` 时 ``modelConfigId`` 必填；模型名由后端配置事实读取；
        - ``taskId`` 始终是必填的已存在 Task 标识；``workspaceId`` 仅用于校验归属。

        参数:
            无；约束字段直接取自当前请求模型。

        返回:
            当前校验通过的 ``AssistantTransportRequest`` 实例（满足 Pydantic
            ``model_validator(mode="after")`` 的契约要求）。

        异常:
            TransportRequestError: 当上述任一纯 wire 约束不满足时抛出，携带稳定的
                机器码、面向用户的安全提示与可重试标记；由全局 exception_handler
                映射为统一的 Assistant Transport HTTP 错误体。

        副作用:
            无；仅做只读断言，不触碰 storage 或 service。
        """
        allowed_effort = {"low", "high", "max"}
        if self.reasoningEffort is not None and self.reasoningEffort not in allowed_effort:
            raise TransportRequestError(
                status_code=400,
                code="REASONING_EFFORT_INVALID",
                message=f"reasoningEffort 必须是 {sorted(allowed_effort)} 之一",
                retryable=False,
            )
        if len({command.commandId for command in self.commands}) != len(self.commands):
            raise TransportRequestError(
                status_code=400,
                code="DUPLICATE_COMMAND_ID",
                message="commands 内 commandId 必须唯一",
                retryable=False,
            )
        if self.threadId != f"task-{self.taskId}":
            raise TransportRequestError(
                status_code=409,
                code="THREAD_TASK_MISMATCH",
                message="threadId 与 taskId 不一致",
                retryable=False,
            )
        message_count = sum(
            1 for command in self.commands if isinstance(command, AddMessageCommand)
        )
        if message_count > 1:
            raise TransportRequestError(
                status_code=400,
                code="MULTIPLE_ADD_MESSAGES_UNSUPPORTED",
                message="一次请求最多包含一个 add-message 命令",
                retryable=False,
            )
        ban_tools_commands = [
            command for command in self.commands if isinstance(command, BanToolsCommand)
        ]
        proposal_commands = [
            command
            for command in self.commands
            if isinstance(
                command,
                ProposeAgentConfigurationCommand | ProposeAgentTeamConfigurationCommand,
            )
        ]
        # 启动对话时只携带模型配置身份，具体模型名由后端配置事实解析。
        has_message = any(isinstance(command, AddMessageCommand) for command in self.commands)
        if ban_tools_commands and (not has_message or len(ban_tools_commands) != 1):
            raise TransportRequestError(
                status_code=400,
                code="BAN_TOOLS_COMMAND_INVALID",
                message="ban-tools 必须与唯一 add-message 命令同批提交",
                retryable=False,
            )
        if proposal_commands and (
            not has_message
            or len(proposal_commands) > 1
        ):
            raise TransportRequestError(
                status_code=400,
                code="AGENT_CONFIGURATION_PROPOSAL_COMMAND_INVALID",
                message="一次请求最多启用一种配置提案模式，且必须与唯一 add-message 命令同批提交",
                retryable=False,
            )
        # 用户决定走的是一条独立的续跑路径：它消费既有 Run 的挂起断点，语义上与新消息互斥
        # （同批会把「新建/编辑」和「恢复」两种 Run 模式混在一次请求里）。
        decision_commands = [
            command
            for command in self.commands
            if isinstance(command, UserInputDecisionCommand)
        ]
        if len(decision_commands) > 1:
            raise TransportRequestError(
                status_code=400,
                code="MULTIPLE_USER_DECISION_COMMANDS",
                message="一次请求最多提交一个用户决定命令",
                retryable=False,
            )
        if decision_commands and has_message:
            raise TransportRequestError(
                status_code=400,
                code="USER_DECISION_COMMAND_INVALID",
                message="用户决定必须单独提交，不能与新消息同批",
                retryable=False,
            )
        if not has_message and self.runId is None:
            raise TransportRequestError(
                status_code=400,
                code="RUN_ID_REQUIRED",
                message="没有新消息时必须提供 runId 以继续已有运行",
                retryable=False,
            )
        if has_message and self.modelConfigId is None:
            raise TransportRequestError(
                status_code=400,
                code="MODEL_SELECTION_REQUIRED",
                message="启动对话必须提供 modelConfigId",
                retryable=False,
            )
        return self

    def user_decisions(self) -> tuple[UserDecision, ...]:
        """取出本请求携带的用户决定（human-in-the-loop）。

        wire 契约保证决定命令至多一条且必须单独提交，因此这里取首条并映射为领域值；没有决定命令时
        返回空序列——空决定不是错误，它表达「用户还没作答」，``wait_user`` 会重新挂起同一请求。

        参数:
            request: 已通过 wire 校验的 Assistant Transport 请求。

        返回:
            领域决定序列（可能为空）。

        异常:
            无。

        副作用:
            无。
        """

        for command in self.commands:
            if isinstance(command, UserInputDecisionCommand):
                decisions = command.to_user_decisions()
                # 只记请求标识与决定种类：``decision.data`` 是用户编辑过的业务正文，不进日志。
                log.info(
                    "user_input_decision_received",
                    extra={
                        "msg": "收到用户决定命令",
                        "data": {
                            "task_id": self.taskId,
                            "run_id": self.runId,
                            "decisions": [
                                {
                                    "request_id": decision.request_id,
                                    "decision": decision.kind.value,
                                }
                                for decision in decisions
                            ],
                        },
                    },
                )
                return decisions
        return ()

class TransportRequestError(Exception):
    """纯 wire 契约校验失败的结构化异常。

    由 ``AssistantTransportRequest.validate_transport_constraints``（``model_validator``）
    在请求解析阶段自动抛出，仅描述与领域持久化无关的结构性约束违反，覆盖：推理深度取值、
    commandId 唯一性、thread/task 一致性、add-message 数量上限与未支持命令类型。
    API 适配层（``app/app.py`` 的全局 exception_handler）负责把它映射为统一的 Assistant
    Transport HTTP 错误体，保持与 ``_raise_transport_error`` 相同的 ``code`` / ``message`` /
    ``retryable`` 形状，使前端 HTTP 错误契约（``lib/http/errors.ts`` 的
    ``StructuredHttpError``）零改动。注意它**不是** Transport snapshot 的
    ``ConversationStateError`` 契约（后者只有 ``code`` / ``message``）。
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        """构造结构化请求校验异常。

        参数:
            status_code: 映射后的 HTTP 状态码。
            code: 稳定的机器可读错误码，与后端错误体 ``error.code`` 对齐。
            message: 面向用户的安全提示，不含密钥或异常堆栈。
            retryable: 客户端是否可在修正条件后重试。

        返回:
            无。

        异常:
            无。

        副作用:
            无；仅初始化不可变错误上下文。
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
