#!/usr/bin/env python3
"""log-triage 路径推导（triage_paths）与两个 CLI 的对抗性验证（独立测试轮）。

被测改动：新增 ``scripts/triage_paths.py``，数据根解析顺序 =
显式 ``CODING_AGENT_DATA_DIR`` → 已存在 ``.cosir`` 的桌面数据根 → 仓库根；
``query_logs.py`` 默认日志目录改走推导并排除 ``backend-console-*.log``、打印 stderr 提示；
``query_app_db.py`` 默认库路径改走推导、打印 stderr 提示、删除 ``attachments`` 子命令。

本文件只**暴露**行为，不修改任何生产代码。所有用例走临时目录 / 内存结构，
不读取也不写入仓库或真实 ``.cosir``（真实库仅以只读方式做一次连通性检查，失败时跳过）。

注：``# ruff: noqa: E501`` —— 日志夹具的一行 JSON 字面量天然超长，拆行无收益。
"""

# ruff: noqa: E501

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import query_app_db as qad  # noqa: E402
import query_logs as ql  # noqa: E402
import triage_paths as tp  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_data_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有用例默认从不带显式数据根开始，避免宿主机环境污染断言。"""

    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)


def _make_root_with_cosir(base: Path) -> Path:
    """在 ``base`` 下创建 ``.cosir`` 子树，代表一份“已存在数据”的数据根。"""

    (base / ".cosir" / "logs").mkdir(parents=True, exist_ok=True)
    (base / ".cosir" / "storage").mkdir(parents=True, exist_ok=True)
    return base


def _run(func, *args, **kwargs):
    """运行 CLI 入口，捕获 stdout/stderr 与退出码（argparse 错误转成异常码）。"""

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(*args, **kwargs)
    except SystemExit as exc:  # argparse 的错误/帮助路径
        code = exc.code
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# 1. 数据根回退顺序与边界
# --------------------------------------------------------------------------- #


class TestDataRootFallbackOrder:
    """data_root 的解析顺序：显式 → 桌面（有 .cosir）→ 仓库根。"""

    # 目的：显式变量为纯空白时不得被当作显式值，应继续回退。潜在缺陷：只判空不 strip 导致空白当路径。
    @pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t  "])
    def test_blank_explicit_is_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        blank: str,
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        repo = tmp_path / "repo"
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", blank)
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        # 空白不应被当作路径，必须回退到“有 .cosir 的桌面根”
        assert tp.data_root() == desktop

    # 目的：显式值指向不存在目录时应原样使用，不得静默改写为桌面/仓库根。潜在缺陷：对显式值做 exists() 校验后悄然改写。
    def test_explicit_nonexistent_used_as_is(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist" / "nested"
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(missing))
        monkeypatch.setattr(tp, "desktop_data_root", lambda: _make_root_with_cosir(tmp_path / "d"))
        monkeypatch.setattr(tp, "repository_root", lambda: _make_root_with_cosir(tmp_path / "r"))
        assert tp.data_root() == missing

    # 目的：显式变量带前后空格应被裁剪后使用（含实际路径）。潜在缺陷：保留空格导致路径带脏字符。
    def test_explicit_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", "  C:/data-root  ")
        assert str(tp.data_root()) == "C:\\data-root"

    # 目的：桌面根与仓库根都有 .cosir 时必须选桌面根。潜在缺陷：顺序反了导致默认查到仓库那份（最危险的静默错）。
    def test_desktop_wins_when_both_have_cosir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        repo = _make_root_with_cosir(tmp_path / "repo")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        assert tp.data_root() == desktop

    # 目的：桌面根无 .cosir 而仓库根有时必须选仓库根。潜在缺陷：无条件偏好桌面根导致查空目录。
    def test_repo_wins_when_only_repo_has_cosir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = tmp_path / "desktop"  # 无 .cosir
        repo = _make_root_with_cosir(tmp_path / "repo")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        assert tp.data_root() == repo

    # 目的：两者都无 .cosir 时回退仓库根且不得抛错。潜在缺陷：无匹配候选时抛异常。
    def test_falls_back_to_repo_when_neither_has_cosir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = tmp_path / "desktop"
        repo = tmp_path / "repo"
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        assert tp.data_root() == repo

    # 目的：Windows 上 APPDATA 缺失不得崩溃，应继续尝试仓库根。潜在缺陷：未捕获 ValueError 导致直接抛错。
    def test_apdata_missing_falls_back_to_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        # desktop_data_root 应抛 ValueError，被吞掉；data_root 回退仓库根
        with pytest.raises(ValueError):
            tp.desktop_data_root()
        assert tp.data_root() == repo

    # 目的：仓库根定位失败但桌面根确实有 .cosir 时，应能返回桌面根而非抛错。
    # 潜在缺陷：repository_root() 被无保护地提前调用，导致“插件式安装（仓库不在附近）+ 桌面有数据”场景直接报错。
    def test_repo_unlocatable_but_desktop_has_cosir_should_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)

        def _boom() -> Path:
            raise ValueError("cannot locate repository root")

        monkeypatch.setattr(tp, "repository_root", _boom)
        # 期望：桌面根可用，应返回它而不是因为仓库根异常而失败
        assert tp.data_root() == desktop


class TestPlatformDesktopRoot:
    """desktop_data_root 的平台推导。"""

    # 目的：Windows 用 %APPDATA%\\com.cosir.desktop（Roaming），不得误用 LOCALAPPDATA。潜在缺陷：平台分支写错。
    def test_windows_uses_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\u\AppData\Roaming")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
        assert str(tp.desktop_data_root()) == "C:\\Users\\u\\AppData\\Roaming\\com.cosir.desktop"

    # 目的：Windows 上 APPDATA 存在但为纯空白时同样视为缺失。潜在缺陷：只判 truthy 不 strip。
    def test_windows_blank_appdata_treated_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", "   ")
        with pytest.raises(ValueError):
            tp.desktop_data_root()

    # 目的：Windows 仅 LOCALAPPDATA 存在（无 APPDATA）时应抛 ValueError 交由上层回退，不得误用 LOCALAPPDATA。潜在缺陷：错误回退到 LocalData 布局。
    def test_windows_localappdata_only_does_not_change_choice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
        monkeypatch.setattr(tp, "repository_root", lambda: _make_root_with_cosir(tmp_path / "r"))
        assert tp.data_root() == tmp_path / "r"

    # 目的：Linux 下 XDG_DATA_HOME 生效。潜在缺陷：忽略 XDG 变量。
    def test_linux_xdg_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
        # 用 Path 比较：本机为 Windows，字符串会被规范化为反斜杠
        assert tp.desktop_data_root() == Path("/xdg/data") / "com.cosir.desktop"

    # 目的：Linux 无 XDG_DATA_HOME 时回退 ~/.local/share。潜在缺陷：缺省路径写错。
    def test_linux_xdg_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
        assert tp.desktop_data_root() == Path("/home/u/.local/share") / "com.cosir.desktop"

    # 目的：macOS 使用 ~/Library/Application Support。潜在缺陷：平台分支缺失。
    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/u")))
        assert tp.desktop_data_root() == (
            Path("/Users/u/Library/Application Support") / "com.cosir.desktop"
        )


# --------------------------------------------------------------------------- #
# 2. 派生路径正确性（完整字符串，防“数据根选错”）
# --------------------------------------------------------------------------- #


class TestDerivedPaths:
    """log_dir / app_db_path 的完整字符串必须等于期望根派生值。"""

    # 目的：数据根取桌面时，派生路径完整字符串正确。潜在缺陷：派生时用了另一个根（静默查错目录）。
    def test_log_dir_and_db_full_string_desktop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: tmp_path / "repo")
        assert tp.log_dir() == desktop / ".cosir" / "logs"
        assert tp.app_db_path() == desktop / ".cosir" / "storage" / "app.sqlite3"
        # 同时校验字符串形态，避免只比 Path 对象掩盖分隔符差异
        assert str(tp.log_dir()) == os.path.join(str(desktop), ".cosir", "logs")
        assert str(tp.app_db_path()) == os.path.join(
            str(desktop), ".cosir", "storage", "app.sqlite3"
        )

    # 目的：数据根取仓库时，派生路径完整字符串正确（不得残留桌面根片段）。潜在缺陷：桌面/仓库混拼。
    def test_log_dir_and_db_full_string_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _make_root_with_cosir(tmp_path / "repo")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: tmp_path / "desktop")
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        assert tp.log_dir() == repo / ".cosir" / "logs"
        assert tp.app_db_path() == repo / ".cosir" / "storage" / "app.sqlite3"

    # 目的：显式数据根时派生路径以其为准。潜在缺陷：显式值被忽略。
    def test_explicit_root_derivation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit-root"
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(explicit))
        assert tp.log_dir() == explicit / ".cosir" / "logs"
        assert tp.app_db_path() == explicit / ".cosir" / "storage" / "app.sqlite3"

    # 目的：system_cosir_dir 传入显式 root 时不再读环境。潜在缺陷：忽略入参仍去探测文件系统。
    def test_system_cosir_dir_explicit_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", "C:/ignored")
        assert tp.system_cosir_dir(Path("/given")) == Path("/given/.cosir")


# --------------------------------------------------------------------------- #
# 3. describe_path_choice：来源标注与服务端提示
# --------------------------------------------------------------------------- #


class TestDescribePathChoice:
    """describe_path_choice 的来源标注必须与实际选中的根一致。"""

    # 目的：显式变量时来源标注为 CODING_AGENT_DATA_DIR 且含实际路径。潜在缺陷：标注与实际不符。
    def test_explicit_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", "C:/explicit")
        assert tp.describe_path_choice() == "data root: C:\\explicit (from CODING_AGENT_DATA_DIR)"

    # 目的：桌面根胜出时来源标注为 desktop app_data_dir。潜在缺陷：来源标签写反。
    def test_desktop_label(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: tmp_path / "repo")
        assert "desktop app_data_dir" in tp.describe_path_choice()

    # 目的：回退到仓库根时来源标注为 repository root。潜在缺陷：误标为 desktop。
    def test_repo_label(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        repo = _make_root_with_cosir(tmp_path / "repo")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: tmp_path / "desktop")
        monkeypatch.setattr(tp, "repository_root", lambda: repo)
        assert "(repository root)" in tp.describe_path_choice()

    # 目的：桌面根可用且被选中、仓库根不可定位时，describe 不得抛错。潜在缺陷：_safe_repository_root 异常外泄。
    def test_describe_survives_missing_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)

        def _boom() -> Path:
            raise ValueError("no repo")

        monkeypatch.setattr(tp, "repository_root", _boom)
        # data_root 现已因仓库根异常而抛错，describe 应吞掉该异常返回空串（至少不能崩）
        text = tp.describe_path_choice()
        assert isinstance(text, str)

    # 目的：data_root 抛错时 describe_path_choice 必须返回空串而非外泄异常。潜在缺陷：提示属可选信息却让整个脚本崩。
    def test_describe_returns_empty_on_unresolvable_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom_root() -> Path:
            raise ValueError("no data root")

        monkeypatch.setattr(tp, "data_root_with_reason", _boom_root)
        # 推导失败也要留下线索，不能静默返回空串让人以为「没提示=没问题」
        assert "<unresolved>" in tp.describe_path_choice()

    # 目的：来源说明与数据根判定同源——仓库根命中（含 .cosir）标注 repository root，
    # 两处都无 .cosir 时必须标注 fallback，避免提示掩盖「只是兜底」的事实。
    # 潜在缺陷：用「结果是否等于仓库根」反推来源，回退场景给出误导性文案。
    def test_reason_distinguishes_hit_from_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _make_root_with_cosir(tmp_path / "repo")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: tmp_path / "desktop")
        monkeypatch.setattr(tp, "repository_root", lambda: repo)

        root, reason = tp.data_root_with_reason()

        assert root == repo
        assert reason == "repository root"

        bare_repo = tmp_path / "bare-repo"
        bare_repo.mkdir()
        monkeypatch.setattr(tp, "repository_root", lambda: bare_repo)

        root, reason = tp.data_root_with_reason()

        assert root == bare_repo
        assert "fallback" in reason

    # 目的：来源说明必须区分「显式变量」这一路。潜在缺陷：显式变量被误标为桌面/仓库来源。
    def test_reason_marks_explicit_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        explicit = tmp_path / "explicit-root"
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(explicit))

        root, reason = tp.data_root_with_reason()

        assert root == explicit
        assert "CODING_AGENT_DATA_DIR" in reason


# --------------------------------------------------------------------------- #
# 4. query_logs：console 排除、目录边界、stderr 纯度
# --------------------------------------------------------------------------- #


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )


class TestConsoleExclusion:
    """目录模式下 backend-console-*.log 必须被排除。"""

    # 目的：结构化分片 + console 分片共存时只读结构化分片（含 .N.log 大小分片）。潜在缺陷：console 污染 trace/级别查询。
    def test_console_and_shards_excluded(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend-2026-09-20.log", [{"event": "a"}])
        _write(tmp_path / "backend-2026-09-20.1.log", [{"event": "shard"}])
        _write(tmp_path / "backend-console-2026-09-20.log", [{"event": "console"}])
        _write(tmp_path / "backend-console-2026-09-20.1.log", [{"event": "console1"}])
        names = sorted(p.name for p in ql.resolve_log_files(str(tmp_path)))
        assert names == ["backend-2026-09-20.1.log", "backend-2026-09-20.log"]

    # 目的：目录里只有 console 文件时的行为必须明确。记录实际行为（报未找到），并断言不会静默返回空集。
    # 潜在缺陷：静默返回空集合让排查者误以为“没有日志”。
    def test_console_only_directory_reports_missing(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend-console-2026-09-20.log", [{"event": "console"}])
        with pytest.raises(FileNotFoundError):
            ql.resolve_log_files(str(tmp_path))

    # 目的：目录里没有任何 backend-*.log 时同样报错而非返回空。潜在缺陷：空目录被当作“读取成功 0 条”。
    def test_empty_directory_reports_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ql.resolve_log_files(str(tmp_path))

    # 目的：显式指定 console 文件时应当被读取（唯一查看通道）。潜在缺陷：显式路径也被 console 过滤规则拦掉。
    def test_explicit_console_file_is_readable(self, tmp_path: Path) -> None:
        console = tmp_path / "backend-console-2026-09-20.log"
        _write(console, [{"event": "console"}])
        assert ql.resolve_log_files(str(console)) == [console]

    # 目的：分片按 mtime 升序返回（时间线可拼接）。潜在缺陷：排序缺失导致跨分片顺序错乱。
    def test_shards_sorted_by_mtime(self, tmp_path: Path) -> None:
        older = tmp_path / "backend-2026-09-19.log"
        newer = tmp_path / "backend-2026-09-20.log"
        _write(newer, [{"event": "n"}])
        _write(older, [{"event": "o"}])
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        assert [p.name for p in ql.resolve_log_files(str(tmp_path))] == [
            "backend-2026-09-19.log",
            "backend-2026-09-20.log",
        ]

    # 目的：确证项目真实分片命名 `<stem>.<index><suffix>`（即 backend-<日期>.N.log）被目录模式收录。
    # 事实源：apps/backend/app/config/logging/handler/date_size_rotating.py::_shard_path。
    # 潜在缺陷：glob 模式与实际分片命名不一致 → 历史分片永远查不到。
    @pytest.mark.parametrize("index", [1, 2, 7])
    def test_project_shard_naming_is_collected(self, tmp_path: Path, index: int) -> None:
        shard = tmp_path / f"backend-2026-09-20.{index}.log"
        _write(shard, [{"event": f"s{index}"}])
        assert ql.resolve_log_files(str(tmp_path)) == [shard]

    # 目的：记录契约行为——标准库 RotatingFileHandler 风格的 `backend-<日期>.log.1`（后缀在末尾）
    # 不被当前 glob（`backend-*.log`）收录。本项目自研 handler 用 `.1.log`，故该形态不会真实出现；
    # 此用例仅固化“glob 依赖后缀结尾”这一前提，判定依据见上一条用例。
    def test_legacy_dotlog_shard_not_collected(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend-2026-09-20.log.1", [{"event": "legacy"}])
        with pytest.raises(FileNotFoundError):
            ql.resolve_log_files(str(tmp_path))


class TestLogsStderrPurity:
    """stderr 提示与 stdout 机器可读输出的分离。"""

    # 目的：--format json 时 stdout 必须是可 json.loads 的纯 JSON，提示只在 stderr。潜在缺陷：提示污染 stdout。
    def test_stdout_pure_json_explicit(self, tmp_path: Path) -> None:
        f = tmp_path / "backend-2026-09-20.log"
        _write(f, [{"event": "e", "ts": "2026-09-20T10:00:00.000Z", "level": "INFO"}])
        code, out, err = _run(ql.main, ["recent", "--log-file", str(f), "--format", "json"])
        assert code == 0
        assert isinstance(json.loads(out), list)
        assert "log files (explicit)" in err
        assert str(f) in err

    # 目的：自动探测时 stderr 必须包含 data root 与 auto 分片列表两行，且含实际路径。潜在缺陷：提示缺失或路径错。
    def test_auto_announce_contains_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        log_dir = tmp_path / "root" / ".cosir" / "logs"
        log_dir.mkdir(parents=True)
        f = log_dir / "backend-2026-09-20.log"
        _write(f, [{"event": "e", "ts": "2026-09-20T10:00:00.000Z", "level": "INFO"}])
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "root"))
        code, out, err = _run(ql.main, ["recent", "--format", "json"])
        assert code == 0
        assert isinstance(json.loads(out), list)
        assert "data root:" in err
        assert "log files (auto)" in err
        assert str(f) in err

    # 目的：显式与自动两种提示文案必须不同（显式不打印 data root 行）。潜在缺陷：两种路径共用同一文案导致无法区分来源。
    def test_explicit_and_auto_messages_differ(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "root" / ".cosir" / "logs"
        log_dir.mkdir(parents=True)
        f = log_dir / "backend-2026-09-20.log"
        _write(f, [{"event": "e", "ts": "2026-09-20T10:00:00.000Z", "level": "INFO"}])
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "root"))
        _, _, auto_err = _run(ql.main, ["recent", "--format", "json"])
        _, _, explicit_err = _run(ql.main, ["recent", "--log-file", str(f), "--format", "json"])
        assert "(auto)" in auto_err and "data root:" in auto_err
        assert "(explicit)" in explicit_err and "data root:" not in explicit_err

    # 目的：--log-file 传入空白串时应视为自动探测。潜在缺陷：空白被当显式路径导致读错位置。
    def test_blank_log_file_is_auto(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        log_dir = tmp_path / "root" / ".cosir" / "logs"
        log_dir.mkdir(parents=True)
        _write(log_dir / "backend-2026-09-20.log", [{"event": "e", "level": "INFO"}])
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "root"))
        code, _, err = _run(ql.main, ["recent", "--log-file", "   ", "--format", "json"])
        assert code == 0
        assert "(auto)" in err

    # 目的：query_logs.default_log_dir 必须等于按数据根推导的 <root>/.cosir/logs（端到端串起推导链）。
    # 潜在缺陷：default_log_dir 仍指向旧位置（如静态仓库内路径）。
    def test_default_log_dir_follows_data_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        desktop = _make_root_with_cosir(tmp_path / "desktop")
        monkeypatch.setattr(tp, "desktop_data_root", lambda: desktop)
        monkeypatch.setattr(tp, "repository_root", lambda: tmp_path / "repo")
        assert ql.default_log_dir() == desktop / ".cosir" / "logs"


# --------------------------------------------------------------------------- #
# 5. query_app_db：子命令面与 stderr 纯度
# --------------------------------------------------------------------------- #


class TestAppDbSubcommands:
    """attachments 必须已删除；库路径提示与 stdout 纯度。"""

    # 目的：attachments 子命令必须不存在——调用应报错退出（非零码），而不是静默成功。潜在缺陷：子命令仍挂在参数表。
    def test_attachments_subcommand_gone(self, tmp_path: Path) -> None:
        fake_db = tmp_path / "app.sqlite3"
        fake_db.write_bytes(b"")
        code, out, err = _run(qad.main, ["attachments", "--db", str(fake_db)])
        assert code != 0
        assert "invalid choice" in err or "attachments" in err

    # 目的：attachments 已从 parser 的 choices 中消失（直接检查参数面）。潜在缺陷：仅 dispatch 删除但解析仍接受。
    def test_attachments_not_in_parser_choices(self) -> None:
        parser = qad.build_parser()
        sub = next(a for a in parser._actions if isinstance(a, __import__("argparse")._SubParsersAction))
        assert "attachments" not in sub.choices
        assert "sessions" in sub.choices  # 相邻子命令仍在，排除“整块误删”

    # 目的：--db 显式时 stderr 文案为 explicit 且含实际路径。潜在缺陷：显式路径提示缺失或标注错误。
    def test_db_explicit_message(self, tmp_path: Path) -> None:
        db = tmp_path / "app.sqlite3"
        db.write_bytes(b"")
        _, _, err = _run(qad.main, ["schema", "--db", str(db), "--format", "json"])
        assert "db (explicit)" in err
        assert str(db) in err

    # 目的：自动探测时 stderr 含 data root + db 两行且 stdout 为纯 JSON。潜在缺陷：提示污染 stdout。
    def test_db_auto_message_and_purity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        storage = root / ".cosir" / "storage"
        storage.mkdir(parents=True)
        _build_min_db(storage / "app.sqlite3")
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(root))
        code, out, err = _run(qad.main, ["schema", "--no-columns", "--format", "json"])
        assert code == 0
        assert "data root:" in err
        assert "db:" in err
        assert json.loads(out) is not None

    # 目的：默认库路径推导失败（无显式变量且根不可推导）时应报错退出 1 而非崩溃 traceback。潜在缺陷：ValueError 外泄。
    def test_default_db_unresolvable_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Path:
            raise ValueError("cannot locate repository root")

        # resolve_db_path -> default_db_path -> app_db_path -> data_root -> repository_root
        monkeypatch.setattr(tp, "repository_root", _boom)
        monkeypatch.setattr(tp, "desktop_data_root", lambda: Path("/nonexistent-desktop-xyz"))
        code, _, err = _run(qad.main, ["schema", "--format", "json"])
        assert code == 1
        assert "Traceback" not in err


def _build_min_db(path: Path) -> None:
    """构造一个只含 workspaces/tasks 的最小业务库，供 schema 子命令连通。"""

    import sqlite3

    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, workspace_id INTEGER, title TEXT);"
    )
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# 6. 对抗尝试：symlink / 相对路径 / 指目录而非文件 / 尾随空格
# --------------------------------------------------------------------------- #


class TestAdversarialPathInputs:
    """显式路径的边界输入。"""

    # 目的：CODING_AGENT_DATA_DIR 指向一个“文件”而非目录时，应原样使用（不抛、不静默改写）。记录契约行为。
    def test_explicit_points_to_file_used_as_is(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "a-file"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(f))
        assert tp.data_root() == f
        # 派生路径仍按绝对字符串拼接（行为记录，非缺陷判定）
        assert tp.log_dir() == f / ".cosir" / "logs"

    # 目的：相对路径的显式数据根应被原样保留（相对），不因 cwd 变化而改写。记录契约行为。
    def test_explicit_relative_root_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", "relative/root")
        root = tp.data_root()
        assert not root.is_absolute()
        assert str(root) == os.path.join("relative", "root")

    # 目的：显式变量为 "." 时返回当前目录，不抛错。潜在缺陷：把 "." 当 falsy 丢弃。
    def test_explicit_dot_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", ".")
        assert str(tp.data_root()) == "."

    # 目的：Windows 上 APPDATA 存在但带前后空格时应裁剪，避免拼出带空格脏目录。
    def test_appdata_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", "  C:\\Roam  ")
        assert str(tp.desktop_data_root()) == os.path.join("C:\\Roam", "com.cosir.desktop")

    # 目的：仓库根查找同时覆盖“脚本路径向上”和“cwd 向上”两条路径：当脚本路径找不到时，cwd 命中应可用。
    # 潜在缺陷：只查一条路径导致用户级安装目录下永远找不到仓库。
    # 说明：pytest 的 tmp_path 落在仓库内，会干扰“脚本路径向上”分支，故改用系统临时目录。
    def test_repository_root_matches_cwd_when_script_path_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = Path(tempfile.mkdtemp(prefix="lt-adversarial-"))
        try:
            # 伪造一个“无 apps/backend”的脚本路径与一个含 apps/backend 的 cwd
            fake_repo = base / "repo"
            (fake_repo / "apps" / "backend").mkdir(parents=True)
            skill_dir = base / "installed" / "skill" / "scripts"
            skill_dir.mkdir(parents=True)
            fake_module = skill_dir / "triage_paths.py"
            fake_module.write_text("", encoding="utf-8")
            monkeypatch.setattr(tp, "__file__", str(fake_module))
            monkeypatch.chdir(fake_repo)
            assert tp.repository_root() == fake_repo
        finally:
            shutil.rmtree(base, ignore_errors=True)

    # 目的：当脚本路径与 cwd 都找不到仓库根时抛 ValueError（明确失败，不静默查空目录）。
    def test_repository_root_raises_when_unfindable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        base = Path(tempfile.mkdtemp(prefix="lt-adversarial-"))
        try:
            isolated = base / "isolated" / "deep"
            isolated.mkdir(parents=True)
            fake_module = isolated / "triage_paths.py"
            fake_module.write_text("", encoding="utf-8")
            monkeypatch.setattr(tp, "__file__", str(fake_module))
            monkeypatch.chdir(isolated)
            with pytest.raises(ValueError):
                tp.repository_root()
        finally:
            shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. 真实库只读连通性（存在才跑，缺失则跳过）
# --------------------------------------------------------------------------- #


def _real_db() -> Path | None:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    candidate = Path(appdata) / "com.cosir.desktop" / ".cosir" / "storage" / "app.sqlite3"
    return candidate if candidate.exists() else None


REAL_DB = _real_db()

APPDB_SUBCOMMANDS: list[list[str]] = [
    ["schema", "--no-columns"],
    ["workspaces"],
    ["tasks", "--limit", "5"],
    ["runs", "--limit", "5"],
    ["commands"],
    ["providers"],
    ["models"],
    ["sessions"],
    ["delegations"],
    ["stuck"],
]


@pytest.mark.skipif(REAL_DB is None, reason="no real desktop app.sqlite3 present")
class TestRealDbReadonly:
    """对真实库只读执行各子命令：退出码 0 且无 traceback。"""

    # 目的：真实库上所有无参子命令都能跑通（只读）。潜在缺陷：schema 漂移导致某子命令崩。
    @pytest.mark.parametrize("cmd", APPDB_SUBCOMMANDS)
    def test_subcommand_ok(self, cmd: list[str]) -> None:
        assert REAL_DB is not None
        code, _, err = _run(qad.main, [*cmd, "--db", str(REAL_DB), "--format", "json"])
        assert code == 0, f"{cmd} failed: {err}"
        assert "Traceback" not in err

    # 目的：真实库不再含 attachment_assets 表（与删除 attachments 子命令一致）。潜在缺陷：schema 与子命令面不同步。
    def test_no_attachment_assets_table(self) -> None:
        import sqlite3

        assert REAL_DB is not None
        con = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
        try:
            names = {
                row[0]
                for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            con.close()
        assert "attachment_assets" not in names
