"""``app.utils.cosir_paths`` 与系统级 ``.cosir`` 锚点的单元测试。

覆盖：workspace 级子路径拼接、``.cosir`` 保留子树判定（含同前缀兄弟目录误匹配与指向保留区的
符号链接）、系统级路径的「固定目录名 + 数据根」组合，以及 ``paths.reset`` 按
``CODING_AGENT_DATA_DIR`` 推导数据根。测试彼此独立，不改动真实仓库目录。
"""

from pathlib import Path

import pytest

from app.utils import cosir_paths, paths


def test_workspace_paths_are_nested_under_cosir(tmp_path: Path) -> None:
    """workspace 级各子路径必须全部嵌在 ``<root>/.cosir`` 之下。"""

    root = tmp_path / "ws"
    assert cosir_paths.workspace_cosir_dir(root) == root / ".cosir"
    assert cosir_paths.workspace_attachment_dir(root) == root / ".cosir" / "Attachment"
    assert (
        cosir_paths.workspace_attachment_staging_dir(root)
        == root / ".cosir" / "Attachment" / ".uploading"
    )
    assert cosir_paths.workspace_tool_artifact_dir(root) == root / ".cosir" / "tool-artifacts"


def test_is_within_cosir_matches_reserved_subtree_only(tmp_path: Path) -> None:
    """``.cosir`` 本身与其子路径命中；同前缀兄弟目录与普通路径不命中。"""

    root = tmp_path / "ws"
    assert cosir_paths.is_within_cosir(root / ".cosir", root) is True
    assert cosir_paths.is_within_cosir(root / ".cosir" / "Attachment" / "a.png", root) is True
    assert cosir_paths.is_within_cosir(root / ".cosir2" / "a.txt", root) is False
    assert cosir_paths.is_within_cosir(root / "src" / "a.txt", root) is False
    assert cosir_paths.is_within_cosir(root / "a.txt", root) is False


def test_is_within_cosir_without_workspace_root_is_false(tmp_path: Path) -> None:
    """缺少 workspace 根时按「无 workspace」处理，恒返回 ``False``。"""

    assert cosir_paths.is_within_cosir(tmp_path / "x.txt", None) is False
    assert cosir_paths.is_within_cosir(tmp_path / "x.txt", "") is False


def test_is_within_cosir_treats_blank_path_as_invalid(tmp_path: Path) -> None:
    """空串/纯空白路径视为无效，不得因 ``realpath("")`` 落到当前目录而误判。"""

    root = tmp_path / "ws"
    assert cosir_paths.is_within_cosir("", root) is False
    assert cosir_paths.is_within_cosir("   ", root) is False


def test_is_within_cosir_follows_symlink_into_reserved_area(tmp_path: Path) -> None:
    """指向 ``.cosir`` 的符号链接必须命中（anti-symlink）。"""

    root = tmp_path / "ws"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    link = root / "link.txt"
    try:
        link.symlink_to(cosir / "target.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    assert cosir_paths.is_within_cosir(link, root) is True


def test_system_cosir_dir_combines_data_dir_with_fixed_name(tmp_path: Path) -> None:
    """``system_cosir_dir()`` = ``paths.DATA_DIR`` + 固定目录名 ``.cosir``。"""

    paths.override(DATA_DIR=tmp_path / "sys")
    try:
        assert cosir_paths.system_cosir_dir() == tmp_path / "sys" / cosir_paths.COSIR_DIR_NAME
    finally:
        paths.reset()


def test_paths_reset_derives_data_dir_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``paths.reset`` 按 ``CODING_AGENT_DATA_DIR`` 推导 ``DATA_DIR``；目录名不入配置。"""

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(data_dir))
    try:
        paths.reset()
        assert data_dir == paths.DATA_DIR
        assert cosir_paths.system_cosir_dir() == data_dir / cosir_paths.COSIR_DIR_NAME
    finally:
        monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
        paths.reset()
