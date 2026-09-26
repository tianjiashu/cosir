"""``SystemPromptBuilder`` 系统级全局指令层（Layer G）单元测试。

验证 ``<system_cosir_dir>/AGENTS.md`` 作为跨 workspace 生效的全局提示词：
存在时注入 ``<global_layer>``、缺失/读取失败时降级为空、预算截断生效、且 ``build``
将其按「runtime → agent → global → workspace」顺序拼入。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.constant import Constant
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.context import system_prompt_builder as spb
from app.utils import paths


@pytest.fixture
def redirect_system_cosir(tmp_path: Path):
    """把系统级 ``.cosir`` 重定向到临时目录，避免触碰真实用户数据。"""
    paths.override(DATA_DIR=tmp_path)
    yield tmp_path / ".cosir"
    paths.reset()


def _write_global(md_dir: Path, text: str) -> None:
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "AGENTS.md").write_text(text, encoding="utf-8")


def _profile() -> AgentProfile:
    # workflow=None 跳过默认 ReactLikeWorkflow 实例化，build 不消费该字段。
    return AgentProfile(
        agent_id="main",
        role="main",
        allowed_tools=[],
        agent_type=AgentProfileType.MAIN,
        system_prompt="Test main system prompt.",
        workflow=None,  # type: ignore[arg-type]
    )


def test_global_layer_absent_creates_blank_file(redirect_system_cosir: Path):
    # 缺失时创建空白 AGENTS.md 供用户编辑，且本次返回空（无有效内容）。
    assert spb.SystemPromptBuilder._build_global_layer() == ""
    created = redirect_system_cosir / "AGENTS.md"
    assert created.is_file()
    assert created.read_text(encoding="utf-8") == ""


def test_global_layer_loads_content(redirect_system_cosir: Path):
    _write_global(redirect_system_cosir, "# Global\nAlways be concise.\n")
    out = spb.SystemPromptBuilder._build_global_layer()
    assert out.startswith("<global_layer>")
    assert "Always be concise." in out
    assert out.strip().endswith("</global_layer>")


def test_global_layer_read_error_degrades_to_empty(redirect_system_cosir: Path, monkeypatch):
    _write_global(redirect_system_cosir, "x")

    def _raise(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(spb, "read_text_file", _raise)
    assert spb.SystemPromptBuilder._build_global_layer() == ""


def test_global_layer_truncated_by_budget(redirect_system_cosir: Path):
    # 远超字节/ token 上限的大文件，应被截断到模块内固定字节兜底以内。
    _write_global(redirect_system_cosir, "a" * 500_000)
    out = spb.SystemPromptBuilder._build_global_layer()
    assert out.startswith("<global_layer>")
    body = out[len("<global_layer>\n"): -len("\n</global_layer>")]
    assert len(body.encode("utf-8")) <= Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_BYTES


def test_build_includes_global_layer_in_order(redirect_system_cosir: Path, tmp_path: Path):
    _write_global(redirect_system_cosir, "GLOBAL INSTRUCTION BODY")
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    (workspace_root / "AGENTS.md").write_text("WORKSPACE INSTRUCTION BODY", encoding="utf-8")
    prompt = spb.SystemPromptBuilder.build(_profile(), str(workspace_root))
    assert "<global_layer>" in prompt
    assert "GLOBAL INSTRUCTION BODY" in prompt
    # 顺序：agent_layer → global_layer → workspace_layer
    assert prompt.index("<agent_layer>") < prompt.index("<global_layer>")
    assert prompt.index("<global_layer>") < prompt.index("<workspace_layer>")
