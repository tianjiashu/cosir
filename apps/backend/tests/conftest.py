"""跨测试文件的共享夹具：隔离后端全局状态（日志、主库、进程级单例）。

两类夹具：

- ``restore_backend_logging_state``（autouse）：保证同进程内日志的全局开关在用例之间不互相污染。
- ``isolated_storage`` / ``initialized_process_singletons``：按需装配「真实主库」与「进程级单例
  （agent profile 目录、工具系统）」，供需要触碰服务层 / 任务运行时的用例显式请求。

后两者刻意不做 autouse：不少用例断言「未初始化时的行为」（如 ``init_storage`` 的路径切换语义），
自动初始化会把那类契约测掉。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.agent_team import configuration_scope_source
from app.config import configuration
from app.config.configuration import build_agent_registry
from app.core.agents import agent_profile_config, agent_profile_source
from app.core.runtime.runner import AgentRuntime
from app.core.tools.tool_system import ToolSystem
from app.service import depends as depends_module
from app.service.depends import close_service_dependencies
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.path import system_cosir as paths

# 会被 ``configure_logging`` 改写 ``propagate`` / ``level`` 的后端 logger。
_BACKEND_LOGGERS = ("coding_agent.backend", "uvicorn.error")

# 单个 logger 需要复原的全局开关：``level`` / ``propagate`` / ``disabled``。
_LoggerState = tuple[int, bool, bool]


def _snapshot() -> tuple[dict[str, _LoggerState], int]:
    """快照后端 logger 的全局开关与全局禁用阈值。

    参数:
        无。

    返回:
        ``({logger_name: (level, propagate, disabled)}, manager.disable)``。``manager.disable``
        是 ``logging.disable()`` 写入的全局阈值，非 0 时会让低于该级别的记录被整体丢弃。

    异常:
        无。

    副作用:
        无（只读取全局配置）。
    """

    states: dict[str, _LoggerState] = {}
    for name in _BACKEND_LOGGERS:
        logger = logging.getLogger(name)
        states[name] = (logger.level, logger.propagate, logger.disabled)
    return states, logging.root.manager.disable


def _restore(snapshot: tuple[dict[str, _LoggerState], int]) -> None:
    """把后端 logger 的全局开关与全局禁用阈值复原到快照值。

    参数:
        snapshot: :func:`_snapshot` 产出的快照。

    返回:
        无。

    异常:
        无。

    副作用:
        改写 :data:`_BACKEND_LOGGERS` 中每个 logger 的 ``level`` / ``propagate`` / ``disabled``，
        并复原 ``logging.disable()`` 的全局阈值；不增删 handler，也不改动 ``Logger.manager``。
    """

    states, root_disable = snapshot
    for name, (level, propagate, disabled) in states.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
    logging.disable(root_disable)


@pytest.fixture(autouse=True)
def restore_backend_logging_state() -> Iterator[None]:
    """在每个用例前后复原日志的全局配置，消除跨文件的日志捕获污染。

    背景：``configure_logging`` 会把 ``coding_agent.backend.propagate`` 置为 ``False``，而
    ``shutdown_logging`` 只移除自己安装的 handler、不复原该开关；用例若再碰上
    ``logger.disabled`` 或 ``logging.disable()``，同进程内后续所有依赖日志记录的 ``caplog``
    断言都会静默抓不到记录，表现为「事件没写」的假失败（与业务行为无关）。

    参数:
        无（pytest 自动注入）。

    返回:
        供 pytest 使用的 ``None`` 迭代器（夹具上下文）。

    异常:
        无。

    副作用:
        用例结束后把 :data:`_BACKEND_LOGGERS` 的 ``level`` / ``propagate`` / ``disabled`` 与
        ``logging.disable()`` 阈值复原为用例开始前的值；不影响用例内部的日志行为（用例内自己
        改的开关在**同一用例内**仍然生效），也不触碰业务代码。
    """

    snapshot = _snapshot()
    try:
        yield
    finally:
        _restore(snapshot)


@pytest.fixture(autouse=True)
def isolate_scope_config_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把系统作用域的配置文件目录隔离到用例临时目录。

    为什么需要：Agent profile 与 Agent Team 注册表现在按作用域**懒装载**，任何一次读取都会去
    访问该作用域的配置目录。若系统作用域仍指向开发者本机的 ``~/.cosir/agents`` 与
    ``~/.cosir/agent-teams``，用例结果会随本机已放置的自定义配置漂移（典型失败形态是「索引里
    多出别人的 profile」）。workspace 作用域天然落在用例自己的临时 workspace 下，无需隔离。

    参数:
        tmp_path: pytest 提供的用例级临时目录。
        monkeypatch: pytest 提供的补丁器，用例结束后自动复原。

    返回:
        无（夹具上下文）。

    异常:
        无。

    副作用:
        改写两个配置来源模块内 ``system_agent_config_dir`` / ``system_agent_team_config_dir``
        的引用；不触碰真实的路径模块，也不创建目录。需要验证真实目录推导的用例可再自行覆盖。
    """

    system_agents = tmp_path / "system-agents"
    system_teams = tmp_path / "system-agent-teams"
    # 需要覆盖三处已导入符号：Agent 侧的装载、配置中心写入与目录 provisioning 都经
    # ``agent_profile_source`` 取值；Team 侧的装载与写入经 ``configuration_scope_source``；
    # Team 目录 provisioning 直接调用路径函数（``agent_profile_config``）。三处都覆盖，
    # 用例内的读、写与 provisioning 才会指向同一目录，且不触碰开发者本机的 ``~/.cosir``。
    monkeypatch.setattr(agent_profile_source, "system_agent_config_dir", lambda: system_agents)
    monkeypatch.setattr(
        configuration_scope_source, "system_agent_team_config_dir", lambda: system_teams
    )
    monkeypatch.setattr(
        agent_profile_config, "system_agent_team_config_dir", lambda: system_teams
    )


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为用例装配隔离的主库与 checkpoint 路径。

    为什么需要它：任务运行时空间、service 与 CRUD 现在都在构造时取真实会话工厂
    （``TaskRuntimeSpace.__post_init__`` 会读当前 task），因此触碰服务层的用例必须先初始化主库；
    若指向真实用户目录，用例之间会互相污染，也会写脏开发者的本地数据。

    参数:
        tmp_path: pytest 提供的用例级临时目录。

    返回:
        无（夹具上下文）。

    异常:
        无。

    副作用:
        关闭既有依赖与任务空间 → 把 ``paths`` 指向用例临时目录 → ``init_storage()`` 建库；
        退出时关闭任务空间与依赖并复原 ``paths``（不删除临时目录，交给 pytest 清理）。
    """

    close_service_dependencies()
    task_runtime_spaces.close()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        CHECKPOINT_FILE=db_dir / "checkpoints.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    try:
        yield
    finally:
        task_runtime_spaces.close()
        close_service_dependencies()
        paths.reset()


@pytest.fixture
def install_process_singletons(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """返回「装配进程级单例（agent 目录 + 工具系统 + 运行时）」的安装函数。

    为什么做成安装函数而不是夹具：``ToolSystem`` 的装配期会构造 ``delegate_task``，它依赖已初始化的
    存储单例，因此安装必须**晚于** ``init_storage()``；夹具的参数化依赖无法表达这种顺序，只能由
    调用方在初始化主库之后显式调用本函数。

    用法：``def storage(tmp_path, install_process_singletons):`` 内先 ``init_storage()``，
    再调用 ``install_process_singletons()``。

    参数:
        monkeypatch: pytest 提供的补丁器；用它改写模块级单例，用例结束后自动复原。

    返回:
        无参安装函数；重复调用会重新构建并覆盖前一次的单例。

    异常:
        RuntimeError: 工具系统装配期依赖的主库尚未初始化（调用顺序错误）。
        AgentProfileConfigError: 内置 agent 定义或 prompt 配置非法（装配层契约错误）。

    副作用:
        把 ``configuration._AGENT_REGISTRY`` / ``configuration._TOOL_SYSTEM`` /
        ``depends._RUNTIME`` 替换为新建实例。
    """

    def install() -> None:
        monkeypatch.setattr(configuration, "_AGENT_REGISTRY", build_agent_registry())
        monkeypatch.setattr(configuration, "_TOOL_SYSTEM", ToolSystem.build_tool_system())
        # 运行时构造期会取工具系统 / agent 目录 / 存储单例，必须在上面三者就绪之后构建。
        monkeypatch.setattr(depends_module, "_RUNTIME", AgentRuntime())

    return install


@pytest.fixture
def backend_env(
    isolated_storage: None, install_process_singletons: Callable[[], None]
) -> Iterator[None]:
    """标准后端测试环境：隔离主库 + 进程级单例（agent 目录 / 工具系统 / 运行时）。

    供整份测试文件需要「真实服务层」的用例用模块级
    ``pytestmark = pytest.mark.usefixtures("backend_env")`` 统一请求；单个用例需要时也可以直接
    作为参数请求。装配顺序由 ``isolated_storage`` 依赖保证：主库先就绪，再构建依赖它的工具系统与
    运行时。

    参数:
        isolated_storage: 隔离主库夹具（先执行）。
        install_process_singletons: 单例安装函数（见其 docstring 的顺序约束）。

    返回:
        无（夹具上下文）。

    异常:
        RuntimeError: 工具系统或运行时装配期依赖未就绪。

    副作用:
        初始化隔离主库并安装进程级单例；退出时由两个依赖夹具各自复原。
    """

    install_process_singletons()
    yield
