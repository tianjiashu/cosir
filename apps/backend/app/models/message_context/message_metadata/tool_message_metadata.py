"""工具结果行的持久化事实：展示事实、等待状态、后台工作记录，以及工具交给运行时的结构化结果。

模型只读观察的 ``content``，卡片只读 ``display_data``；``ToolResultFacts`` 是既不进模型正文、也不进
卡片的那部分后端事实（交付记录、已加载的工具，以及文件事实 ``FileFacts``）。
"""

from pydantic import BaseModel, ConfigDict

from app.models.deliverable import Deliverable
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.external_tool import ExternalToolDefinition
from app.models.file_access.file_facts import FileFacts
from app.models.message_context.message_metadata.base.message_metadata_base import MessageMetadata


class BackgroundWork(BaseModel):
    """一次后台工作的持久化记录，挂在这次调用的后台占位行上，看板与卡片都从它重建。

    后台工作目前只有终端命令（``execute_terminal`` 转后台或直接后台运行），所以不记种类；出现第二种时
    再加，不预留。

    为什么不单独建表：后台工作就是对话里的一次工具调用，占位行已经随对话 fork、撤回、Session
    删除；记录挂在它上面，看板就是对话的一个投影，没有第二个事实源要手工同步。工作是否还在跑
    不在这里（进程内 ``command_results`` 的活条目才是），这里只记重启后还需要的事实。

    时间都是 UTC ISO 字符串，取自工作本身而不是行的时间戳：fork 复制行时会重新生成时间戳。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 启动工作的 Run（只是来历：工作属于 Session，生命周期不看它）。
    run_id: int | None
    # 看板与等待提示的标签，登记时由工具从事实里取（命令的意图、子 Agent 名）；UI 动词、图标与本地化仍归前端。
    label: str
    # 登记后台结果的时刻（转到后台的调用是它开始前台等待的时刻）。
    started_at: str
    # 结论到达、被撤销或被重启中断的时刻；还在等结论时为 ``None``。
    ended_at: str | None = None


class ToolResultFacts(BaseModel):
    """一次前台工具调用交出的后端事实集合，挂在结果行的 ``ToolMessageMetadata.facts`` 上。

    它只给后端规则、投影与清单读：既不进模型正文（模型只看观察的 ``content``），也不进卡片展示
    （那归 ``display_data``）。这些事实也不能从正文反解——正文要过输出预算、可能被截断，也不带执行
    要用的定位信息，后端规则同样不解析正文。

    工具经 ``tool_success(facts=...)`` 交出，随结果行持久化，再随上下文继承（分叉）、移出（撤回）与
    重建（恢复、重启）。整份为空（``facts is None``）表示这次调用不交出任何后端事实。后台命令的结论
    晚于结果行到达，只带文件事实，落在并入时的 user 行（见 ``FileFacts``）。

    每个字段只属于一个工具（见各字段注释），新的同类事实在这里加一个字段即可，构造、投递与持久化
    都不用改。读取方按字段分工：

    - ``deliverables``：交付卡与产出清单的事实源（``assistant_transport/projection/tool_result``、
      ``conversation_state_deliverable``）；
    - ``loaded_external_tools`` / ``loaded_builtin_tools``：下一次请求的工具集与准入
      （``runtime_message_context_manager``、``core/tool/external_tools``）；
    - ``file_facts``：文件读写证据与终端对账，读取方是 ``core/file_access``（见 ``FileFacts``）。

    实例构造后不可变（``frozen`` + ``extra="forbid"``）：事实只能在结果行产生时决定，读取方不得
    原地修改；需要改写时用 ``model_copy(update=...)`` 生成新副本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 交付记录（只有 ``present_deliverables``）：卡片与产出清单的事实源，见 ``app.models.deliverable``。
    deliverables: tuple[Deliverable, ...] | None = None
    # ``tool_search`` 加载的工具：两项合起来是「已加载」的唯一事实（见 ``RequestTools``）。外部工具存定义
    # 快照（见 ``app.models.external_tool``）；内置工具只存名字，定义取 Session 工具面固化的那一份。
    loaded_external_tools: tuple[ExternalToolDefinition, ...] | None = None
    loaded_builtin_tools: tuple[str, ...] | None = None
    # 文件读写工具、终端读取与 ``execute_terminal`` 交出。
    file_facts: FileFacts | None = None


class ToolMessageMetadata(MessageMetadata):
    """工具结果的展示事实与等待状态：``pending`` 的等待占位就是「等你操作」（见 :attr:`awaiting_decision`），
    其余字段是卡片重建所需载荷。"""

    status: ToolCallEventStatus
    display_data: dict[str, object] | None = None
    error: str | None = None
    # 后台结果的后台占位（见 ``ToolObservation.background``）：存在即后台占位，卡片状态与看板条目
    # 的持久化事实源。
    background: BackgroundWork | None = None
    # 工具交给运行时的结构化结果（见 ``ToolResultFacts``）。与 ``background``
    # 一样「有才写」：投影不会因为某次事件没带它而清掉 part 上已有的。
    facts: ToolResultFacts | None = None

    @property
    def file_facts(self) -> FileFacts | None:
        """与 ``UserMessageMetadata.file_facts`` 同名：读取证据的一方不必区分事实从哪种行来。"""
        return self.facts.file_facts if self.facts is not None else None

    @property
    def awaiting_decision(self) -> bool:
        """决定入口与展示投影共用的等待条件。"""
        return self.status == "pending" and self.background is None

    def merged_onto(self, previous: "ToolMessageMetadata") -> "ToolMessageMetadata":
        """结果行 metadata 的更新规则，全仓只此一处：状态与错误取新值，展示数据、后台记录与结果事实「有才写」
        ——新值为空时沿用旧值。

        更新方（决定重放、后台结论并入、Run 外的占位结算）常常只知道新的状态，不知道也不该覆盖卡片的
        展示数据与后台记录；所以空值表示「不变」，而不是「清空」。目前没有需要清空这几项的场景。
        """
        return ToolMessageMetadata(
            status=self.status,
            error=self.error,
            display_data=self.display_data or previous.display_data,
            background=self.background or previous.background,
            facts=self.facts or previous.facts,
        )
