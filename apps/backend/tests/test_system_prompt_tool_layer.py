"""``SystemPromptBuilder`` 工具能力目录层（Layer T）单元测试。

覆盖该层的四条契约：只在 ``AgentProfile.allowed_tools`` 含委派工具时生成、目录取自进程级 Agent
目录且按 workspace 作用域隔离、无 CHILD 候选时不生成、字节兜底截断后标签仍闭合；另覆盖
`Agent 目录未初始化必须硬失败` 与 `build` 中的层序。不覆盖 delegate_task 工具定义（见
``test_delegate_task_model_contract.py``）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import configuration
from app.config.constant import Constant
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import main_agent
from app.core.context import system_prompt_builder as spb
from app.utils import paths

_DELEGATION_TOOLS = ("read_file", "delegate_task")
_TOOL_LAYER_OVERHEAD = len(b"<tool_layer>\n\n</tool_layer>")
# 该层只把 workspace 当作 Registry 作用域键，故用例用任意稳定的路径字符串。
_ANY_WORKSPACE = "ws"


def _child_profile(
    agent_id: str = "reviewer",
    description: str = "Reviews changes.",
) -> AgentProfile:
    """构造一个真实契约的 CHILD profile（workflow=None 跳过工作流装配）。"""

    return AgentProfile(
        agent_id=agent_id,
        role=agent_id,
        description=description,
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="Child prompt.",
        workflow=None,  # type: ignore[arg-type]
    )


def _registry_with(*profiles: AgentProfile) -> AgentProfileRegistry:
    """把给定 profile 注册进 system 作用域并返回 Registry。"""

    registry = AgentProfileRegistry()
    for profile in profiles:
        registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, profile)
    return registry


@pytest.fixture
def redirect_system_cosir(tmp_path: Path):
    """把系统级 ``.cosir`` 重定向到临时目录，避免触碰真实用户数据。"""

    paths.override(DATA_DIR=tmp_path)
    yield tmp_path / ".cosir"
    paths.reset()


def test_tool_layer_skipped_without_delegation_tool(monkeypatch) -> None:
    """工具集不含委派工具时不生成该层，且完全不访问 Agent 目录。"""

    def _fail() -> AgentProfileRegistry:
        raise AssertionError("工具集不含委派工具时不应读取 Agent 目录")

    monkeypatch.setattr(configuration, "get_agent_registry", _fail)

    assert spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, ("read_file",)) == ""
    assert spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, ()) == ""


def test_tool_layer_projects_child_catalog_without_main_profile(monkeypatch) -> None:
    """委派工具生效时输出子 Agent 目录，MAIN profile 不进入目录。"""

    registry = _registry_with(_child_profile(), main_agent())
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: registry)

    layer = spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, _DELEGATION_TOOLS)

    assert layer.startswith("<tool_layer>\n")
    assert layer.endswith("\n</tool_layer>")
    assert "agent_id: reviewer" in layer
    assert "Reviews changes." in layer
    assert "main_agent" not in layer


def test_tool_layer_skipped_without_child_candidates(monkeypatch) -> None:
    """目录里只有 MAIN profile 时不生成该层（不留误导性空标题）。"""

    monkeypatch.setattr(
        configuration,
        "get_agent_registry",
        lambda: _registry_with(main_agent()),
    )

    assert spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, _DELEGATION_TOOLS) == ""


def test_tool_layer_scopes_catalog_to_workspace(monkeypatch, tmp_path: Path) -> None:
    """workspace profile 只在其自身作用域生效，不泄漏到其它 workspace 的目录。"""

    workspace = tmp_path / "ws-a"
    workspace.mkdir()
    registry = _registry_with(_child_profile())
    registry.register(workspace, _child_profile(agent_id="ws-a-agent"))
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: registry)

    scoped = spb.SystemPromptBuilder._build_tool_layer(str(workspace), _DELEGATION_TOOLS)
    other = spb.SystemPromptBuilder._build_tool_layer(str(tmp_path / "ws-b"), _DELEGATION_TOOLS)

    assert "ws-a-agent" in scoped
    assert "ws-a-agent" not in other


def test_tool_layer_requires_initialized_registry(monkeypatch) -> None:
    """委派工具生效但 Agent 目录未初始化时硬失败，不静默降级为空层。"""

    def _uninitialized() -> AgentProfileRegistry:
        raise RuntimeError("agent registry has not been initialized")

    monkeypatch.setattr(configuration, "get_agent_registry", _uninitialized)

    with pytest.raises(RuntimeError, match="agent registry has not been initialized"):
        spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, _DELEGATION_TOOLS)


def test_tool_layer_byte_cap_keeps_tags_balanced(monkeypatch) -> None:
    """目录超字节兜底时截断正文并保留完整标签对，整体不超过上限。"""

    monkeypatch.setattr(
        configuration,
        "get_agent_registry",
        lambda: _registry_with(_child_profile(description="a" * 20_000)),
    )

    layer = spb.SystemPromptBuilder._build_tool_layer(_ANY_WORKSPACE, _DELEGATION_TOOLS)

    assert layer.startswith("<tool_layer>\n")
    assert layer.endswith("\n</tool_layer>")
    assert len(layer.encode("utf-8")) <= Constant.SystemPrompt.TOOL_LAYER_MAX_BYTES
    assert "a" * 20_000 not in layer
    body = layer[len("<tool_layer>\n"): -len("\n</tool_layer>")]
    assert (
        len(body.encode("utf-8"))
        == Constant.SystemPrompt.TOOL_LAYER_MAX_BYTES - _TOOL_LAYER_OVERHEAD
    )


def test_build_orders_tool_layer_between_agent_and_global(
    monkeypatch, redirect_system_cosir
) -> None:
    """build 中层序为 agent_layer → tool_layer → global_layer → workspace_layer。"""

    redirect_system_cosir.mkdir(parents=True, exist_ok=True)
    (redirect_system_cosir / "AGENTS.md").write_text("GLOBAL BODY", encoding="utf-8")
    workspace = redirect_system_cosir.parent / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("WORKSPACE BODY", encoding="utf-8")
    monkeypatch.setattr(
        configuration,
        "get_agent_registry",
        lambda: _registry_with(_child_profile()),
    )

    prompt = spb.SystemPromptBuilder.build(main_agent(), str(workspace))

    assert prompt.index("<agent_layer>") < prompt.index("<tool_layer>")
    assert prompt.index("<tool_layer>") < prompt.index("<global_layer>")
    # workspace 层标签带属性（abs_path=...），故只比较前缀。
    assert prompt.index("<global_layer>") < prompt.index("<workspace_layer")
    assert "agent_id: reviewer" in prompt


def test_build_omits_tool_layer_without_delegation_in_allowed_tools(
    monkeypatch, redirect_system_cosir
) -> None:
    """``allowed_tools`` 不含委派工具时 build 不生成工具层，也不读取 Agent 目录。"""

    def _fail() -> AgentProfileRegistry:
        raise AssertionError("allowed_tools 不含委派工具时不应读取 Agent 目录")

    monkeypatch.setattr(configuration, "get_agent_registry", _fail)

    # 该 helper profile 只声明 read_file，不含 delegate_task。
    prompt = spb.SystemPromptBuilder.build(_child_profile(), str(redirect_system_cosir.parent))

    assert "<tool_layer>" not in prompt
