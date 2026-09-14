"""Layer 3 workspace 项目指令层：只择优加载唯一一个指令文件的行为契约。"""

from pathlib import Path

from app.core.context.system_prompt_builder import SystemPromptBuilder

# 与 Settings.WORKSPACE_INSTRUCTION_FILE_NAMES 默认值一致，显式传入以固定优先级语义。
FILE_NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_DEPTH = 2


def _find(root: Path, file_names: tuple[str, ...] = FILE_NAMES) -> tuple[Path, Path] | None:
    """以默认深度调用被测方法，便于各用例只关注文件布局。"""
    return SystemPromptBuilder._find_instruction_file(root, file_names, MAX_DEPTH)


def test_file_name_priority_outranks_directory_depth(tmp_path: Path) -> None:
    """文件名优先级高于目录层级：深层 AGENTS.md 应击败根目录 CLAUDE.md。"""

    (tmp_path / "CLAUDE.md").write_text("ROOT-CLAUDE", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("DEEP-AGENTS", encoding="utf-8")

    found = _find(tmp_path)

    assert found is not None
    assert found[0] == Path("pkg") / "AGENTS.md"


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


def test_falls_back_to_second_configured_name(tmp_path: Path) -> None:
    """扫描范围内没有任何 AGENTS.md 时才回退到 CLAUDE.md。"""

    (tmp_path / "CLAUDE.md").write_text("ROOT-CLAUDE", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "deep" / "AGENTS.md").write_text(
        "OUT-OF-RANGE", encoding="utf-8"
    )

    found = _find(tmp_path)

    assert found is not None
    assert found[0] == Path("CLAUDE.md")


def test_ignores_dirs_in_skip_list_and_depth_limit(tmp_path: Path) -> None:
    """跳过忽略目录，且超过 max_depth 的文件不参与择优。"""

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "AGENTS.md").write_text("IGNORED", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "deep" / "AGENTS.md").write_text("TOO-DEEP", encoding="utf-8")

    assert _find(tmp_path) is None


def test_returns_empty_layer_without_any_candidate(tmp_path: Path) -> None:
    """无命中时定位返回 None，且项目指令层为空字符串。"""

    assert _find(tmp_path) is None
    assert SystemPromptBuilder._build_workspace_layer(str(tmp_path)) == ""


def test_workspace_layer_contains_only_selected_file(tmp_path: Path) -> None:
    """项目指令层只包含选中的那一个文件，并带相对路径标题。"""

    (tmp_path / "AGENTS.md").write_text("ROOT-AGENTS", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "CLAUDE.md").write_text("DEEP-CLAUDE", encoding="utf-8")

    layer = SystemPromptBuilder._build_workspace_layer(str(tmp_path))

    assert layer.startswith("<workspace_layer>\n# ./AGENTS.md\n")
    assert layer.endswith("\n</workspace_layer>")
    assert "ROOT-AGENTS" in layer
    assert "DEEP-CLAUDE" not in layer
