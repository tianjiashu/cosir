"""`utils.code_file_utils` 扩展名判定工具的单元测试。

覆盖：隐藏配置文件名匹配、锁文件前缀排除、复合扩展名排除、大小写、目录、
无扩展名文件、常见语言源码等边界。与独立审查 Agent 复验的 14 个边界用例对齐。
"""

import pytest

from app.utils.code_file_utils import (
    extract_source_code_paths,
    get_source_code_extensions,
    is_source_code_file,
)


@pytest.mark.parametrize(
    "path, expected",
    [
        # 复合扩展名排除（压缩产物）
        ("a.min.js", False),
        ("app.min.css", False),
        ("foo.js", True),
        # 排除清单中的压缩/打包产物
        ("foo.tar.gz", False),  # gz 在排除清单
        ("app.js.map", False),  # map 在排除清单
        # 点号隐藏配置文件名显式匹配
        (".env", True),
        (".ENV", True),
        (".env.local", True),
        (".gitignore", True),
        (".editorconfig", True),
        (".npmrc", True),
        (".DS_Store", False),  # 非已知点号配置且 suffix 空
        # 无扩展名文件（非点号配置）
        ("Makefile", False),
        ("Dockerfile", False),
        ("README", False),
        # 大小写（注意：纯点号文件 .PY 的 suffix 为空，不命中白名单）
        ("src/Main.PY", True),
        # 常见语言源码
        ("src/main.py", True),
        ("src/main.pyi", True),
        ("src/index.ts", True),
        ("src/index.tsx", True),
        ("src/App.tsx", True),
        ("src/lib.rs", True),
        ("src/main.go", True),
        ("src/main.java", True),
        ("src/program.cs", True),
        ("src/component.vue", True),
        ("src/component.svelte", True),
        ("src/page.astro", True),
        ("contracts/token.sol", True),
        # 数据与标记语言
        ("data/config.json", True),
        ("data/config.jsonc", True),
        ("ci/pipeline.yaml", True),
        ("ci/pipeline.yml", True),
        ("pyproject.toml", True),
        ("README.md", True),
        ("docs/intro.mdx", True),
        # 构建与配置
        ("scripts/build.sh", True),
        ("scripts/build.ps1", True),
        ("Dockerfile.build", False),  # 带 .build 后缀，suffix=build 不在白名单
        # 二进制/噪声排除
        ("img/logo.png", False),
        ("img/photo.jpeg", False),
        ("doc/report.pdf", False),
        ("lib/foo.so", False),
        ("app/main.exe", False),
        ("__pycache__/mod.pyc", False),
        ("data/export.csv", False),
        ("logs/app.log", False),
        # 锁文件前缀排除（suffix 是 json/yaml 但应被拦截）
        ("package-lock.json", False),
        ("pnpm-lock.yaml", False),
        ("uv.lock", False),
        ("poetry.lock", False),
        ("yarn.lock", False),
        ("Gemfile.lock", False),
        ("composer.lock", False),
    ],
)
def test_is_source_code_file(path: str, expected: bool) -> None:
    """核对各类路径的源码判定结果符合预期。"""
    assert is_source_code_file(path) is expected


def test_is_source_code_file_rejects_directory(tmp_path) -> None:
    """目录必须被判定为非源码文件。"""
    assert is_source_code_file(tmp_path) is False
    assert is_source_code_file(str(tmp_path)) is False


def test_extra_extensions_expands_whitelist(tmp_path) -> None:
    """extra_extensions 应在白名单基础上追加可索引类型。"""
    (tmp_path / "comp.vue").write_text("")
    assert is_source_code_file(tmp_path / "comp.vue") is True  # vue 已在白名单
    assert is_source_code_file(tmp_path / "comp.tpl") is False
    assert is_source_code_file(tmp_path / "comp.tpl", extra_extensions={"tpl"}) is True


def test_exclude_extensions_narrows_whitelist(tmp_path) -> None:
    """exclude_extensions 应在排除基础上再剔除指定类型。"""
    assert is_source_code_file(tmp_path / "main.py") is True
    assert is_source_code_file(tmp_path / "main.py", exclude_extensions={"py"}) is False


def test_get_source_code_extensions_returns_immutable() -> None:
    """返回的集合应为不可变 frozenset，调用方无法修改。"""
    exts = get_source_code_extensions()
    assert isinstance(exts, frozenset)
    assert "py" in exts
    with pytest.raises(AttributeError):
        exts.add("xyz")  # frozenset 无 add 方法


def test_extract_source_code_paths_filters_and_dedupes(tmp_path) -> None:
    """extract_source_code_paths 应过滤非源码、去重并保持顺序。"""
    inputs = [
        "a.py",
        "b.md",
        "c.png",  # 非源码
        "a.py",  # 重复
        "d.min.js",  # 复合排除
        "package-lock.json",  # 锁文件排除
        "Makefile",  # 无扩展名
    ]
    result = extract_source_code_paths(inputs)
    assert [p.name for p in result] == ["a.py", "b.md"]
    # Path 形式输入同样工作
    result2 = extract_source_code_paths([tmp_path / "x.ts", tmp_path / "y.go"])
    assert {p.name for p in result2} == {"x.ts", "y.go"}
