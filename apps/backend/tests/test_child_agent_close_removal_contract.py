"""独立验证：删除 ``child_agent_close`` 工具后系统无回归的契约测试。

本文件的测试对象是「删除 child_agent_close」这一变更的**外部可观测契约**，分四层取证：

1. 模块面：``child_agent_close``（handler）与 ``child_agent_close_args``（args 模型）
   两个模块已不可导入、源文件已从磁盘删除、无残留文本引用。
2. 名字面：``ALL_TOOL_NAMES`` 与 ``ToolName`` 不含该名字、无重复、长度与常量集自洽。
3. 装配面：``build_tool_registry()`` 的注册清单不含该名字，且「注册集合 =
   规范集合 - 条件跳过的 Web 工具」这一不变式仍然成立。
4. 兄弟面（对抗）：仍存在的三个 child 工具（send/status/wait）的 ``args_model`` /
   ``to_definition()`` 契约，以及 ``CHILD_DISALLOWED_TOOLS`` 不变式未被破坏。

注明：本文件只做测试与事实固化，不修改任何生产代码。
"""

from __future__ import annotations

import importlib
import importlib.util
import threading
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.service.agent_team import registry as agent_team_registry_module
from app.service.agent_team.configuration_scope_source import AgentTeamConfigurationScopeSource
from app.service.agent_team.registry import AgentTeamConfigurationRegistry
from app.config.constant import Constant
from app.core.tool.schemas import ALL_TOOL_NAMES, ToolName
from app.core.tool.handler.child_agent_send import (
    ChildAgentSendTool,
    build_child_agent_send_definition,
)
from app.core.tool.handler.child_agent_status import (
    ChildAgentStatusTool,
    build_child_agent_status_definition,
)
from app.core.tool.handler.wait_messages import (
    WaitMessagesTool,
    build_wait_messages_definition,
)
from app.core.tool.handler.child_agent.child_agent_send_args import ChildAgentSendArgs
from app.core.tool.handler.child_agent.child_agent_status_args import ChildAgentStatusArgs
from app.core.tool.handler.wait.wait_messages_args import WaitMessagesArgs
from app.tools.tool_registration import build_tool_registry
from app.service.depends import close_service_dependencies
from app.storage.store_engines import init_storage
from app.utils.path import system_cosir as paths

# 被删除的工具名（唯一事实源：本测试的待验证目标）。
_REMOVED_TOOL_NAME = "child_agent_close"

# 被删除的模块全限定名。
_REMOVED_HANDLER_MODULE = "app.core.tool.handler.child_agent_close"
_REMOVED_ARGS_MODULE = "app.core.tool.handler.child_agent.child_agent_close_args"

# 业务源码根目录（用于磁盘残留扫描）。
_APP_DIR = Path(__file__).resolve().parents[1] / "app"

# 三个仍存在的 child 工具的描述符：(工具名, 工具类, 参数模型, 工厂函数, 工具级超时)。
_CHILD_TOOL_SPECS = [
    (
        "child_agent_send",
        ChildAgentSendTool,
        ChildAgentSendArgs,
        build_child_agent_send_definition,
        30.0,
    ),
    (
        "child_agent_status",
        ChildAgentStatusTool,
        ChildAgentStatusArgs,
        build_child_agent_status_definition,
        10.0,
    ),
    (
        "wait_messages",
        WaitMessagesTool,
        WaitMessagesArgs,
        build_wait_messages_definition,
        10.0,
    ),
]

# 期望被禁用的工具全集（子 Agent 不得使用）：委派/Team 工具 + 配置提案 + terminal_session/ 全族。
# 说明：子 Agent 不允许继续委派，也不具备提案 Agent / Team 配置的权限；Team 节点（不同清单，
# 见 ``Constant.Tools.TEAM_NODE_DISALLOWED_TOOLS``）则保留委派与父子通信工具。
_EXPECTED_BANNED_TOOLS = frozenset(
    {
        "delegate_task",
        "agent_team",
        "propose_agent_team_configuration",
        "propose_agent_configuration",
        "child_agent_status",
        "child_agent_send",
        "wait_messages",
        "terminal_start",
        "terminal_write",
        "terminal_read",
        "terminal_signal",
        "terminal_close",
    }
)

# 装配期可能被条件跳过的工具（无可用 Provider 的 Web 工具）。
_CONDITIONALLY_SKIPPED_TOOLS = frozenset({"web_search", "web_extract"})


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """为需要真实注册表/服务单例的用例提供隔离的主库，并按 lifespan 顺序注入 Team 注册表。

    ``build_tool_registry()`` 装配期会构造 ``agent_team`` 工具，它读取进程级 Team
    注册表；未注入时按契约抛 ``RuntimeError``，因此须先于工具系统装配注入。
    """

    monkeypatch.setattr(
        agent_team_registry_module,
        "_REGISTRY",
        AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource()),
    )
    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    yield
    close_service_dependencies()
    paths.reset()


# --------------------------------------------------------------------------- #
# 1. 模块面：被删除模块不可导入、源文件已删除、无残留引用
# --------------------------------------------------------------------------- #


def test_removed_handler_module_is_not_importable() -> None:
    """目的：child_agent_close handler 模块已删除，导入应抛 ModuleNotFoundError。

    潜在缺陷类型：删除不彻底（模块仍可导入 / 被 __pycache__ 陈旧字节码顶替）。
    """

    assert importlib.util.find_spec(_REMOVED_HANDLER_MODULE) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_REMOVED_HANDLER_MODULE)


def test_removed_args_module_is_not_importable() -> None:
    """目的：child_agent_close_args 参数模型模块已删除，导入应抛 ModuleNotFoundError。

    潜在缺陷类型：删除不彻底（参数模型残留，导入面与工具面不一致）。
    """

    assert importlib.util.find_spec(_REMOVED_ARGS_MODULE) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_REMOVED_ARGS_MODULE)


def test_removed_source_files_are_absent_from_disk() -> None:
    """目的：两个被删模块的 .py 源文件确实已从磁盘移除。

    潜在缺陷类型：仅从装配清单摘除但源文件仍在，导致幽灵模块/误导入。
    """

    handler_py = (
        _APP_DIR / "core" / "tool" / "handler" / "child_agent_close.py"
    )
    args_py = (
        _APP_DIR / "core" / "tool" / "handler" / "child_agent" / "child_agent_close_args.py"
    )
    assert not handler_py.exists(), f"残留源文件: {handler_py}"
    assert not args_py.exists(), f"残留源文件: {args_py}"


def test_removed_symbols_absent_from_package_namespaces() -> None:
    """目的：被删符号未出现在任何包的导出面（__all__ / 属性）。

    潜在缺陷类型：包入口仍 re-export 已删符号（from ... import ChildAgentCloseArgs），
    造成导入即 ImportError 的隐蔽断点。
    """

    schemas = importlib.import_module("app.core.tool.schemas")
    tool_models = importlib.import_module("app.tools.tool_models")
    child_task_models = importlib.import_module("app.core.tool.handler.child_agent")
    tool_names = importlib.import_module("app.core.tool.schemas.tool_names")

    assert not hasattr(tool_names, "TOOL_CHILD_AGENT_CLOSE")
    assert not hasattr(schemas, "TOOL_CHILD_AGENT_CLOSE")
    assert "TOOL_CHILD_AGENT_CLOSE" not in getattr(schemas, "__all__", [])
    assert not hasattr(child_task_models, "ChildAgentCloseArgs")
    assert "ChildAgentCloseArgs" not in getattr(child_task_models, "__all__", [])
    assert not hasattr(tool_models, "ChildAgentCloseArgs")
    assert "ChildAgentCloseArgs" not in getattr(tool_models, "__all__", [])


@pytest.mark.parametrize("marker", ["child_agent_close", "ChildAgentClose", "CHILD_AGENT_CLOSE"])
def test_no_residual_reference_in_app_sources(marker: str) -> None:
    """目的：app/ 源码树中不存在被删工具的任何大小写形态文本引用。

    潜在缺陷类型：字符串/日志/schema 描述里残留旧工具名，导致模型或前端看到幽灵工具名。
    """

    hits: list[str] = []
    for path in list(_APP_DIR.rglob("*.py")) + list(_APP_DIR.rglob("*.md")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if marker in text:
            hits.append(str(path.relative_to(_APP_DIR)))
    assert hits == [], f"app/ 源码树仍引用 {marker}: {hits}"


# --------------------------------------------------------------------------- #
# 2. 名字面：ALL_TOOL_NAMES / ToolName 契约
# --------------------------------------------------------------------------- #


def test_all_tool_names_excludes_removed_tool_and_has_no_duplicates() -> None:
    """目的：规范工具名清单不含被删名字且无重复。

    潜在缺陷类型：删除时漏改一处导致重复/残留名字，破坏名字数组的集合语义。
    """

    assert _REMOVED_TOOL_NAME not in ALL_TOOL_NAMES
    assert len(ALL_TOOL_NAMES) == len(set(ALL_TOOL_NAMES))
    assert all(isinstance(name, str) and name for name in ALL_TOOL_NAMES)


def test_tool_name_literal_matches_all_tool_names_exactly() -> None:
    """目的：ToolName Literal 与 ALL_TOOL_NAMES 精确一致（双向），且不含被删名字。

    潜在缺陷类型：类型别名与运行期数组漂移（TypedDict/Literal 仍含已删名字）。
    """

    literal_args = set(typing.get_args(ToolName))
    assert literal_args == set(ALL_TOOL_NAMES)
    assert _REMOVED_TOOL_NAME not in literal_args


def test_every_tool_name_constant_is_registered_in_canonical_list() -> None:
    """目的：tool_names 模块里每个 ``TOOL_*`` 常量值都出现在 ALL_TOOL_NAMES 中。

    潜在缺陷类型：新增/删除常量时漏改 ALL_TOOL_NAMES，留下孤儿常量或未登记名字。
    """

    module = importlib.import_module("app.core.tool.schemas.tool_names")
    constants = {
        key: value
        for key, value in vars(module).items()
        if key.startswith("TOOL_") and isinstance(value, str)
    }
    assert constants, "未发现任何 TOOL_* 常量"
    canonical = set(ALL_TOOL_NAMES)
    orphans = {k: v for k, v in constants.items() if v not in canonical}
    assert orphans == {}, f"存在未登记进 ALL_TOOL_NAMES 的常量: {orphans}"
    assert "TOOL_CHILD_AGENT_CLOSE" not in constants


# --------------------------------------------------------------------------- #
# 3. 装配面：工具注册清单
# --------------------------------------------------------------------------- #


def test_tool_registry_excludes_removed_tool(storage: None) -> None:
    """目的：进程级装配后的注册表不含 child_agent_close，且相关查询全部落空。

    潜在缺陷类型：装配清单未同步删除，注册表仍暴露已删工具（模型可调幽灵工具）。
    """

    registry = build_tool_registry()
    names = registry.get_all_tool_names()

    assert _REMOVED_TOOL_NAME not in names
    assert registry.get_tool_definition(_REMOVED_TOOL_NAME) is None
    assert registry.get_schema(_REMOVED_TOOL_NAME) is None


def test_registry_is_subset_of_canonical_names_and_only_web_tools_may_be_missing(
    storage: None,
) -> None:
    """目的：注册集合 ⊆ 规范集合，且规范集合中缺失的只能是条件跳过的 Web 工具。

    潜在缺陷类型：注册了规范清单外的名字（幽灵工具），或删改导致内置工具被意外丢掉。
    """

    names = set(build_tool_registry().get_all_tool_names())
    canonical = set(ALL_TOOL_NAMES)

    assert names <= canonical, f"注册了非规范工具名: {names - canonical}"
    missing = canonical - names
    unexpected_missing = missing - _CONDITIONALLY_SKIPPED_TOOLS
    assert unexpected_missing == set(), f"非 Web 工具被意外跳过: {unexpected_missing}"
    assert _REMOVED_TOOL_NAME not in names


def test_registry_exposes_three_remaining_child_tools(storage: None) -> None:
    """目的：删除 close 后，三个兄弟 child 工具仍被正常注册。

    潜在缺陷类型：删除动作误伤同目录兄弟工具（整目录导入失败或漏注册）。
    """

    names = set(build_tool_registry().get_all_tool_names())
    assert {"child_agent_send", "child_agent_status", "wait_messages"} <= names
    assert "delegate_task" in names


def test_build_tool_registry_is_deterministic_across_calls(storage: None) -> None:
    """目的：多次装配得到相同的工具名集合（装配无隐藏状态）。

    潜在缺陷类型：装配依赖进程级可变状态，导致二次装配清单漂移。
    """

    first = build_tool_registry().get_all_tool_names()
    second = build_tool_registry().get_all_tool_names()
    assert first == second


def test_registry_reads_are_thread_safe(storage: None) -> None:
    """目的：并发读取注册表清单结果一致（线程安全 smoke）。

    潜在缺陷类型：并发查询下注册表内部结构被破坏或返回不一致快照。
    """

    registry = build_tool_registry()
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def _read() -> None:
        try:
            for _ in range(200):
                results.append(registry.get_all_tool_names())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results
    assert all(names == results[0] for names in results)
    assert _REMOVED_TOOL_NAME not in results[0]


# --------------------------------------------------------------------------- #
# 4. 兄弟面（对抗）：三个 child 工具的契约与 CHILD_DISALLOWED_TOOLS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool_name", "tool_cls", "args_model", "factory", "timeout_seconds"),
    _CHILD_TOOL_SPECS,
    ids=[spec[0] for spec in _CHILD_TOOL_SPECS],
)
def test_child_tool_args_model_permission_and_name_contract(
    storage: None,
    tool_name: str,
    tool_cls: type,
    args_model: type,
    factory: object,
    timeout_seconds: float,
) -> None:
    """目的：每个存留 child 工具的 name / args_model / timeout 契约未被破坏。

    潜在缺陷类型：删除动作连带改坏兄弟工具的权限标签或参数模型绑定。
    """

    tool = tool_cls()
    assert tool.name == tool_name
    assert tool.args_model is args_model
    assert tool.timeout_seconds == timeout_seconds


@pytest.mark.parametrize(
    ("tool_name", "tool_cls", "args_model", "factory", "timeout_seconds"),
    _CHILD_TOOL_SPECS,
    ids=[spec[0] for spec in _CHILD_TOOL_SPECS],
)
def test_child_tool_to_definition_is_consistent(
    storage: None,
    tool_name: str,
    tool_cls: type,
    args_model: type,
    factory: object,
    timeout_seconds: float,
) -> None:
    """目的：to_definition() 返回的定义字段自洽，且工厂函数等价于实例方法。

    潜在缺陷类型：定义构造遗漏 args_model，或工厂与实例方法分叉。
    """

    tool = tool_cls()
    definition = tool.to_definition()

    assert definition.name == tool_name
    assert definition.args_model is args_model
    assert definition.execution_mode == "thread"
    assert definition.timeout_seconds > 0
    assert definition.display is not None
    assert definition.handler is not None

    built = factory()
    assert built.name == definition.name
    assert built.args_model is definition.args_model
    assert built.execution_mode == definition.execution_mode


def test_child_tool_definitions_match_registry_definitions(storage: None) -> None:
    """目的：注册表内的 child 工具定义与各自工厂产出一致（装配未改写契约）。

    潜在缺陷类型：注册期对定义做了归一化改写，使注册表与工厂产出分叉。
    """

    registry = build_tool_registry()
    for tool_name, _tool_cls, args_model, _factory, _timeout in _CHILD_TOOL_SPECS:
        definition = registry.get_tool_definition(tool_name)
        assert definition is not None, f"{tool_name} 未注册"
        assert definition.args_model is args_model


def test_child_send_args_model_boundaries() -> None:
    """目的：child_agent_send 参数模型边界（id>0、message 1..8000）未被破坏。

    潜在缺陷类型：删除改动误改参数约束，放宽/收紧合法输入区间。
    """

    ok = ChildAgentSendArgs(child_task_id=1, message="x")
    assert ok.child_task_id == 1
    assert ok.message == "x"

    for bad_id in (0, -1):
        with pytest.raises(ValidationError):
            ChildAgentSendArgs(child_task_id=bad_id, message="x")

    with pytest.raises(ValidationError):
        ChildAgentSendArgs(child_task_id=1, message="")
    with pytest.raises(ValidationError):
        ChildAgentSendArgs(child_task_id=1, message="x" * 8001)

    assert ChildAgentSendArgs(child_task_id=1, message="x" * 8000).message == "x" * 8000


def test_child_status_args_model_boundaries() -> None:
    """目的：child_agent_status 参数模型边界（id>0）未被破坏。

    潜在缺陷类型：边界约束丢失（允许 0/负数 id）。
    """

    assert ChildAgentStatusArgs(child_task_id=5).child_task_id == 5
    for bad_id in (0, -1):
        with pytest.raises(ValidationError):
            ChildAgentStatusArgs(child_task_id=bad_id)


def test_wait_messages_takes_no_arguments() -> None:
    """目的：等待工具没有参数——等什么由挂起时 Task 上未完成的工作决定，模型不能指定超时。"""

    WaitMessagesArgs()
    with pytest.raises(ValidationError):
        WaitMessagesArgs.model_validate({"timeout_seconds": 10})


def test_child_banned_tools_invariants() -> None:
    """目的：``Constant.Tools.CHILD_DISALLOWED_TOOLS`` 非空、无重复、全为 str、是规范名子集
    且不含已删工具名。

    潜在缺陷类型：删除 close 后禁用清单残留旧名字（禁用已不存在的工具，或命名失效），
    或误删其它禁用项削弱子 Agent 隔离。
    """

    banned = Constant.Tools.CHILD_DISALLOWED_TOOLS
    assert isinstance(banned, tuple)
    assert banned, "CHILD_DISALLOWED_TOOLS 不得为空"
    assert len(banned) == len(set(banned)), "存在重复项"
    assert all(isinstance(name, str) and name for name in banned)
    assert _REMOVED_TOOL_NAME not in banned
    assert set(banned) <= set(ALL_TOOL_NAMES), "禁用清单含非规范（幽灵/已删）工具名"
    assert set(banned) == _EXPECTED_BANNED_TOOLS


def test_banned_tools_all_exist_in_live_registry(storage: None) -> None:
    """目的：禁用清单中每个名字都指向一个真实存在的内置工具（除被条件跳过的 Web 工具）。

    潜在缺陷类型：禁用清单指向已删/不存在的工具，使禁用规则形同虚设。
    """

    registered = set(build_tool_registry().get_all_tool_names())
    unbacked = set(Constant.Tools.CHILD_DISALLOWED_TOOLS) - registered
    assert unbacked <= _CONDITIONALLY_SKIPPED_TOOLS, f"禁用清单指向不存在的工具: {unbacked}"


