"""Agent profile 值对象，以及子 Agent JSON 配置的唯一解析与校验入口。

本模块持有两类紧邻的事实：``AgentProfile`` 运行时值对象，以及
``.cosir/agents/*.json`` 的严格输入契约。``AgentProfile.vaild_agent_profile`` 是
「JSON 文件 → CHILD profile」的唯一入口，文档字段契约只在本模块维护；文件读取与 JSON
顶层解析复用 ``app.utils.json_utils.read_json_object``，``model_settings`` 覆盖项的类型
契约由 ``ModelSettings.from_json`` 持有。其他模块不得再实现第二套校验。

失败语义：单个配置文件无效只影响该文件——``vaild_agent_profile`` 捕获校验异常、以
``agent_profile_config_invalid`` 事件写 error 日志后返回 ``None``，由调用方决定跳过或降级；
目录级问题（目录不存在、符号链接越界、目录不可读）才由 ``AgentProfileRegistry`` 抛出
``AgentProfileConfigError``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config.logging.logger import log
from app.core.agents.model_settings import ModelSettings
from app.models import ConversationRunRecord
from app.service.depends import get_provider_service
from app.utils.json_utils import read_json_object

if TYPE_CHECKING:
    from app.core.tools.schemas.tool_definition import ToolDefinition
    from app.core.workflows.agent_workflow import AgentWorkflow


class AgentProfileType(str, Enum):
    """Agent 分类，决定它在运行时如何被暴露与调度。

    取值：
        MAIN: 主 Agent，全局唯一（系统内置，不对用户开放配置）。
        CHILD: 供主 Agent 委派的子 Agent（出现在委派摘要、可被 delegation 调用）。
        HIDDEN: 隐藏在系统内部的 Agent（如上下文压缩），不向委派摘要暴露、不可被委派。
    """

    MAIN = "main"
    CHILD = "child"
    HIDDEN = "hidden"


class AgentProfileConfigError(ValueError):
    """表示 Agent 配置加载失败（目录级问题）。

    由 ``AgentProfileRegistry`` 在配置目录不存在、符号链接越界或目录不可读时抛出，消息中
    始终带有路径。``AgentProfile.vaild_agent_profile`` 内部也用它标记单个文件的校验失败，
    但不会向外抛出：该方法捕获全部异常、写 error 日志后返回 ``None``（见模块 docstring 的
    失败语义）。调用方按作用域决定处置方式：系统目录出错阻止启动，workspace 目录出错由
    启动编排记录后继续装配其他 workspace。
    """


def _default_workflow() -> AgentWorkflow:
    """返回默认 ReAct-like 工作流实例（延迟导入，打破循环依赖）。

    参数:
        无。

    返回:
        新建的 ``ReactLikeWorkflow`` 实例；每个 profile 独享一份，不在 profile 间共享。

    异常:
        ImportError: 具体 workflow 实现模块不可用（属装配错误，直接向上暴露）。
        因实现类缺失导致的 ``TypeError`` 等构造期错误同样不在此转换。

    副作用:
        首次调用会导入 ``app.core.workflows.react``。导入刻意放在函数内：模块级导入会形成
        ``profile → react → runtime_operations → context → profile`` 的循环。
    """

    from app.core.workflows.react import ReactLikeWorkflow

    return ReactLikeWorkflow()


def _allowed_tool_names() -> frozenset[str]:
    """返回配置 `allowed_tools` 可引用的工具规范名集合。

    参数:
        无。

    返回:
        `app/core/tools/schemas/tool_names.py` 中声明的全部工具名集合。

    异常:
        无（导入失败会直接向上抛出 ImportError，表示工具名清单不可用）。

    副作用:
        首次调用会导入 ``app.core.tools.schemas`` 包。导入刻意放在函数内：该包的
        ``__init__`` 会拉起 ``tool_runtime_dependencies`` → ``agent_profile_registry``
        的装配链，而 registry 反向依赖本模块，模块级导入会在其中任一入口触发模块循环
        导入；函数级导入发生在运行期，此时相关模块均已初始化完成。
    """

    from app.core.tools.schemas.tool_names import ALL_TOOL_NAMES

    return frozenset(ALL_TOOL_NAMES)


# 子 Agent JSON 文档契约（原 `_AgentProfileDocument` 的 schema 以显式规则表承载）：
# 字段名 → 允许的 JSON 值类型。类型不做隐式转换，未声明的键一律拒绝；必填字段单独声明。
_DOCUMENT_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "agent_id": (str,),
    "role": (str,),
    "description": (str,),
    "system_prompt": (str,),
    "allowed_tools": (list,),
    "max_steps": (int,),
    "provider_id": (int, type(None)),
    "model_name": (str, type(None)),
    "model_settings": (dict,),
}
_BLANK_REJECTED_TEXT_FIELDS = ("agent_id", "role", "description", "system_prompt")
_REQUIRED_DOCUMENT_FIELDS = ("agent_id", "role", "description", "system_prompt", "allowed_tools")


def _validate_document(document: Mapping[str, Any], source: Path) -> None:
    """按字段契约校验文档的字段名与 JSON 值类型（不解释字段业务语义）。

    参数:
        document: 已解析的顶层 JSON 对象。
        source: 配置文件路径，仅用于拼装可定位的错误消息。

    返回:
        无；校验通过即静默返回。

    异常:
        AgentProfileConfigError: 含未声明字段、缺少必填字段，或字段值类型不符；消息中包含
            文件路径与字段名。

    副作用:
        无；失败日志由调用方 ``AgentProfile.vaild_agent_profile`` 统一记录，本函数只负责
        构造异常对象。
    """

    unknown = sorted(set(document) - set(_DOCUMENT_FIELD_TYPES))
    if unknown:
        raise AgentProfileConfigError(
            f"Agent 配置无效，文件={source}，含未知字段: {', '.join(unknown)}"
        )
    missing = [name for name in _REQUIRED_DOCUMENT_FIELDS if name not in document]
    if missing:
        raise AgentProfileConfigError(
            f"Agent 配置无效，文件={source}，缺少必填字段: {', '.join(missing)}"
        )
    for name, value in document.items():
        accepted = _DOCUMENT_FIELD_TYPES[name]
        # bool 是 int 的子类：JSON 的 true/false 不作为整数接受，保持严格类型语义。
        if isinstance(value, accepted) and (not isinstance(value, bool) or bool in accepted):
            continue
        expected = "/".join(item.__name__ for item in accepted)
        raise AgentProfileConfigError(f"Agent 配置无效，文件={source}，{name} 必须是 {expected}")


def _reject_blank_text_fields(document: Mapping[str, Any], source: Path) -> None:
    """拒绝 ``agent_id`` / ``role`` / ``description`` / ``system_prompt`` 为空或纯空白。

    参数:
        document: 已通过 :func:`_validate_document` 的文档。
        source: 配置文件路径，仅用于拼装可定位的错误消息。

    返回:
        无；全部文本字段非空白时静默返回。

    异常:
        AgentProfileConfigError: 任一文本字段去空白后为空（含全空白字符串）。

    副作用:
        无。
    """

    for name in _BLANK_REJECTED_TEXT_FIELDS:
        if not document[name].strip():
            raise AgentProfileConfigError(f"Agent 配置无效，文件={source}，{name} 不能为空或纯空白")


def _require_tool_names(document: Mapping[str, Any], source: Path) -> list[str]:
    """取出并校验 ``allowed_tools``：全为字符串、非空、来自规范清单且不重复。

    参数:
        document: 已通过 :func:`_validate_document` 的文档。
        source: 配置文件路径，仅用于拼装可定位的错误消息。

    返回:
        校验通过的 ``allowed_tools`` 内容副本（新列表，不引用文档原对象）。

    异常:
        AgentProfileConfigError: 含非字符串元素、列表为空、含未知工具名或存在重复。

    副作用:
        首次调用会经 :func:`_allowed_tool_names` 导入 ``app.core.tools.schemas`` 包。
    """
    try:
        tool_names = document["allowed_tools"]
        if not all(isinstance(name, str) for name in tool_names):
            raise AgentProfileConfigError(
                f"Agent 配置无效，文件={source}，allowed_tools 必须全部是字符串"
            )
        if not tool_names:
            raise AgentProfileConfigError(
                f"Agent 配置无效，文件={source}，allowed_tools 至少需要一个工具名"
            )
        unknown = sorted(set(tool_names) - _allowed_tool_names())
        if unknown:
            raise AgentProfileConfigError(
                f"Agent 配置无效，文件={source}，allowed_tools 含未知工具名: {', '.join(unknown)}"
            )
        if len(set(tool_names)) != len(tool_names):
            raise AgentProfileConfigError(f"Agent 配置无效，文件={source}，allowed_tools 不能重复")
        return list(tool_names)
    except Exception as exc:
        log.exception(
            "_require_tool_names_invalid",
            extra={
                "msg": "_require_tool_names 校验失败,返回空列表",
                "data": {
                    "file": str(source),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            },
        )
        return []


@dataclass
class AgentProfile:
    """描述某个任务的 Agent 执行主体（能力事实源）。
    description 应该是"选择指南"，system_prompt 应该是"执行协议"，而本次
    delegate_task.message 才是"具体工作单"。

    字段：
        agent_id: 持久化在任务和事件上的稳定 Agent 标识。
        role: 人类可读的 Agent 角色。
        description: 子 Agent 的职责/能力/适用场景与约束描述（delegate_task 中暴露给父 Agent）；
            主 Agent 不设置此字段。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        workflow: 执行策略（默认 ReAct-like，延迟导入打破循环依赖）。
        provider_id: 模型厂商 id（None 时由 model_name 推导）。
        model_name: 模型名称（可带 provider 前缀）。2026-08-18 决议：内置 profile 不内置
            默认模型，默认 None；None 表示未配置，由前端优先校验、后端兜底报错。
        model_settings: 模型覆盖配置（``ModelSettings``）。
        agent_type: Agent 分类（``AgentProfileType``），决定其在运行时的暴露与调度方式。
        max_steps: 单 run 最大步骤数。
        run: 当前所属 Conversation Run 记录（经 ``derive_for_run`` 注入 per-run 副本；
            单例上不原地写）。
        system_prompt: 已装配到内存中的完整 Agent 系统提示词正文；来源读取和文件路径不进入 profile。
    """

    agent_id: str
    role: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    agent_type: AgentProfileType = field(default=AgentProfileType.CHILD)
    description: str | None = field(default=None, kw_only=True)
    workflow: AgentWorkflow = field(default_factory=_default_workflow)
    provider_id: int | None = None
    model_name: str | None = None
    model_settings: ModelSettings = field(default_factory=ModelSettings.default_settings)
    max_steps: int = 100
    run: ConversationRunRecord | None = None

    def derive_for_run(
        self,
        run: ConversationRunRecord,
        *,
        ban_tools: list[str] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> AgentProfile:
        """为一次独立的 Conversation Run 执行派生 per-run 副本。

        并发隔离收口：AgentProfile 是注册表共享单例，禁止调用方对其原地写运行时字段
        （并发 run 会互相覆盖）。每次 run 执行必须先经本方法派生独立副本，副本承载本次
        执行的 ``run`` 与按 ``ban_tools`` 收窄后的 ``allowed_tools``，不同 run 的副本
        互不串扰。

        参数:
            run: 本次执行的 Conversation Run 记录（必填，写入副本的 ``run`` 字段）；其
                ``provider_id`` / ``model_name`` 用于回填副本上尚未配置的模型路由。
            ban_tools: 本次执行禁用的工具名列表；``None`` 表示不禁用。传入时按工具名
                从 ``allowed_tools`` 中差集收窄（``select_tools`` 同样按工具名过滤，
                两处口径必须一致）。
            model_settings: 模型参数覆盖；``None`` 表示沿用副本当前值。

        返回:
            绑定当前 run 的独立 ``AgentProfile`` 副本；``self`` 原实例不被修改。

        异常:
            无。

        副作用:
            无（纯值替换，不写数据库、不触碰注册表）。
        """

        changes: dict = {"run": run}
        if ban_tools is not None:
            banned = set(ban_tools)
            changes["allowed_tools"] = [t for t in self.allowed_tools if t not in banned]
        if self.provider_id is None:
            changes["provider_id"] = run.provider_id
        if self.model_name is None:
            changes["model_name"] = run.model_name
        if model_settings is not None:
            changes["model_settings"] = model_settings
        return replace(self, **changes)

    def select_tools(self, tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
        """从候选工具中筛选本 Agent 可运行的工具集合。

        工具「能否运行」由 Agent profile 全权决定，调用方（如运行底座）只按 workspace
        可见性给出候选，不再自行做权限门禁，避免职责分散。

        参数:
            tools: 候选工具定义集合（通常按 workspace 可见性预筛后）。

        返回:
            名称出现在 ``allowed_tools`` 中的工具定义列表；顺序沿用入参顺序，入参不被修改。

        异常:
            无。

        副作用:
            无（纯过滤，不修改入参、不读配置、不写状态）。
        """

        return [tool for tool in tools if tool.name in self.allowed_tools]

    def to_dict(self) -> dict:
        """将 Agent profile 转换为可 JSON 序列化的字典。

        字段集与 ``AgentProfileResponse`` 保持一致；``workflow`` 取 Protocol 声明的
        ``workflow_id``（漏定义在实现侧即类型错误）。不导出 ``system_prompt``、
        ``model_settings`` 与 ``run``：前者体积大且属内部装配结果，后两者不是展示字段。

        参数:
            无。

        返回:
            可直接 JSON 序列化的字典；``agent_type`` 与 ``workflow`` 已转为其字符串取值。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "agent_type": self.agent_type.value,
            "workflow": self.workflow.workflow_id,
            "model_name": self.model_name,
            "max_steps": self.max_steps,
        }

    @staticmethod
    def vaild_agent_profile(path: Path) -> AgentProfile | None:
        """读取并校验一个 JSON 子 Agent 配置文件，尽力构造 CHILD profile。

        本方法是「JSON 文件 → profile」的唯一入口：文件读取、文档字段契约校验和文本/
        工具名语义校验都在本方法内完成，``model_settings`` 覆盖项的字段契约委托
        ``ModelSettings.from_json``，不依赖任何外部配置加载模块。

        失败即降级：单个配置文件无效不应让整个目录加载失败，因此本方法捕获全部异常，写
        error 日志留痕后返回 ``None``，由调用方跳过该文件。异常类型不限于配置错误——出现
        ``KeyError`` 之类非预期类型时同样只降级，但会原样落到日志的 ``error_type`` 字段，
        避免「文件被静默丢弃」。

        参数:
            path: 子 Agent JSON 配置文件路径。文件名与 ``agent_id`` 是否一致不在本方法校验，
                也暂未由调用方校验（见 ``AgentProfileRegistry`` 的已知缺口）。

        返回:
            校验通过的 CHILD profile：``agent_type`` 固定为 ``CHILD``，workflow 使用默认
            实现，``run`` 为空（由 ``derive_for_run`` 在运行时派生），JSON 未声明
            ``max_steps`` 时沿用 ``AgentProfile.max_steps`` 字段默认值。

            任一步骤失败时返回 ``None``：文件不可读/编码无效、JSON 语法错误、顶层非对象、
            缺少必填字段、出现未知字段、字段值类型不符、文本字段空白、
            ``allowed_tools`` 为空/含非字符串/含未知名/重复、``max_steps`` 非正、
            ``model_settings`` 非法。

        异常:
            无。本方法把内部异常（``AgentProfileConfigError``、``JsonFileError``、
            ``ModelSettingsError`` 及非预期异常）全部转换为 ``None``。

        副作用:
            读取指定配置文件；失败时以 ``agent_profile_config_invalid`` 事件写一条 error
            日志（字段：``file`` 配置路径、``error_type`` 异常类型、``error`` 异常消息截断
            至 500 字符）。不修改注册表、其他文件或任何运行时状态。
        """

        source = Path(path)
        try:
            document = read_json_object(source)
            _validate_document(document, source)
            _reject_blank_text_fields(document, source)
            allowed_tools = _require_tool_names(document, source)
            max_steps = document.get("max_steps")
            if max_steps is not None and max_steps <= 0:
                raise AgentProfileConfigError(
                    f"Agent 配置无效，文件={source}，max_steps 必须是正整数"
                )
            model_settings = ModelSettings.from_json(document.get("model_settings", {}))
            provider_id = document.get("provider_id")
            model_name = document.get("model_name")
            if not get_provider_service().vaild_provider(provider_id=provider_id, model_name=model_name):
                provider_id = None
                model_name = None
            return AgentProfile(
                agent_id=document["agent_id"],
                role=document["role"],
                description=document["description"],
                allowed_tools=allowed_tools,
                agent_type=AgentProfileType.CHILD,
                system_prompt=document["system_prompt"],
                provider_id=provider_id,
                model_name=model_name,
                model_settings=model_settings,
                # 未声明时沿用 dataclass 字段默认值，避免在此重复硬编码步骤上限。
                max_steps=AgentProfile.max_steps if max_steps is None else max_steps,
            )
        except Exception as exc:
            # 坏配置必须留痕：这里捕获后返回 None，调用方只会跳过文件，没有日志就查不到原因。
            log.exception(
                "agent_profile_config_invalid",
                extra={
                    "msg": "子 Agent 配置无效，该配置文件被跳过",
                    "data": {
                        "file": str(source),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )
            return None
