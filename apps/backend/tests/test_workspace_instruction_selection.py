"""Layer 3 workspace 项目指令层：只择优加载唯一一个指令文件的行为契约。"""

from pathlib import Path

from app.core.context.system_prompt_builder import SystemPromptBuilder

# 候选文件名硬编码在被测模块内（不再经 Settings 配置注入），故用例不再传文件名。
INSTRUCTION_FILE_NAME = "AGENTS.md"
IGNORED_FILE_NAME = "CLAUDE.md"


def _find(root: Path) -> tuple[Path, Path] | None:
    """调用被测方法定位唯一指令文件，便于各用例只关注文件布局。

    扫描深度上限为被测模块内硬编码的固定值（4 层），不接收深度参数。
    """
    return SystemPromptBuilder._find_instruction_file(root)


def test_other_instruction_files_are_not_candidates(tmp_path: Path) -> None:
    """只认 AGENTS.md：CLAUDE.md 不是候选，深层 AGENTS.md 仍然胜出。"""

    (tmp_path / IGNORED_FILE_NAME).write_text("ROOT-CLAUDE", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / INSTRUCTION_FILE_NAME).write_text("DEEP-AGENTS", encoding="utf-8")

    found = _find(tmp_path)

    assert found is not None
    assert found[0] == Path("pkg") / INSTRUCTION_FILE_NAME


def test_same_name_prefers_shallowest_directory(tmp_path: Path) -> None:
    """同名文件共存时取目录层级更浅者，且文件名匹配不区分大小写。"""

    (tmp_path / "agents.md").write_text("ROOT-LOWER", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("DEEP-UPPER", encoding="utf-8")

    found = _find(tmp_path)

    assert found is not None
    assert found[0] == Path("agents.md")


def test_same_depth_breaks_tie_by_path_order(tmp_path: Path) -> None:
    """同层级多命中时以相对路径字典序作为确定性兜底。"""

    for name in ("b", "a"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "AGENTS.md").write_text(name, encoding="utf-8")

    found = _find(tmp_path)

    assert found is not None
    assert found[0] == Path("a") / "AGENTS.md"


def test_no_agents_md_returns_none_even_with_other_instruction_file(tmp_path: Path) -> None:
    """扫描范围内没有 AGENTS.md 时不回退到其它指令文件：定位返回 None、层为空字符串。"""

    (tmp_path / IGNORED_FILE_NAME).write_text("ROOT-CLAUDE", encoding="utf-8")
    # 唯一 AGENTS.md 放在固定最大深度（4 层）之外，属于扫描范围之外的越界文件。
    out_of_range = tmp_path
    for name in ("pkg", "sub", "deep", "deeper", "deepest"):
        out_of_range = out_of_range / name
    out_of_range.mkdir(parents=True)
    (out_of_range / INSTRUCTION_FILE_NAME).write_text("OUT-OF-RANGE", encoding="utf-8")

    assert _find(tmp_path) is None
    assert SystemPromptBuilder._build_workspace_layer(str(tmp_path)) == ""


def test_ignores_dirs_in_skip_list_and_depth_limit(tmp_path: Path) -> None:
    """跳过忽略目录，且超过固定最大深度（4 层）的文件不参与择优。"""

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "AGENTS.md").write_text("IGNORED", encoding="utf-8")
    # 越界 AGENTS.md：放到固定最大深度（4 层）之外，确认不参与择优。
    too_deep = tmp_path
    for name in ("pkg", "sub", "deep", "deeper", "deepest"):
        too_deep = too_deep / name
    too_deep.mkdir(parents=True)
    (too_deep / "AGENTS.md").write_text("TOO-DEEP", encoding="utf-8")

    assert _find(tmp_path) is None


def test_returns_empty_layer_without_any_candidate(tmp_path: Path) -> None:
    """无命中时定位返回 None，且项目指令层为空字符串。"""

    assert _find(tmp_path) is None
    assert SystemPromptBuilder._build_workspace_layer(str(tmp_path)) == ""


def test_workspace_layer_contains_only_selected_file(tmp_path: Path) -> None:
    """项目指令层只包含选中的那一个文件，并带相对路径标题。"""

    (tmp_path / INSTRUCTION_FILE_NAME).write_text("ROOT-AGENTS", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / IGNORED_FILE_NAME).write_text("DEEP-CLAUDE", encoding="utf-8")

    layer = SystemPromptBuilder._build_workspace_layer(str(tmp_path))

    assert layer.startswith("<workspace_layer>\n# ./AGENTS.md\n")
    assert layer.endswith("\n</workspace_layer>")
    assert "ROOT-AGENTS" in layer
    assert "DEEP-CLAUDE" not in layer
