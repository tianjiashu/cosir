"""固定路径从 ``Settings`` 迁移到 ``app.config.paths`` 的独立对抗性测试。

本文件只测不改：不修改 ``apps/backend/app/`` 下任何生产代码。

对抗维度：
  A. 未设环境变量时四个固定常量的推导契约（数据根 / 日志目录 / 主库 / checkpoint）。
  B. ``paths.reset()`` 按环境变量重算：只设 DATA_DIR、同时设 DATA_DIR 与 LOG_DIR、
     LOG_DIR 优先级、空串回落、monkeypatch 清理后还原。
  C. ``paths.override(**kwargs)``：部分覆盖、未知键、覆盖后 ``system_cosir_dir`` 联动。
  D. ``Settings`` 已无四个路径属性、也不再提供覆盖入口；``Settings.load()`` 触发
     ``paths.reset()`` 与环境对齐。
  E. ``app.utils.cosir_paths`` 可独立导入且 ``system_cosir_dir`` 读 ``paths.DATA_DIR``。
  F. ``app.storage.store_engines`` 的 ``init_storage`` / ``checkpoint_path`` 使用 ``paths`` 常量、
     路径变化后切换引擎、并在结束时复位避免污染。
  G. 边界：``CODING_AGENT_DATA_DIR`` 为空串 / 纯空白（均按未设置回落仓库根）/ 相对路径的处理。
  H. 测试灵敏度自证（运行期内存突变，不落盘、不改生产文件）。
  I. ``paths`` 内部契约：推导表 ``_DERIVERS`` 与 ``__all__`` 同源、``_env_path`` 归一化、
     ``override`` 原子性与类型校验、脏 globals 复位。

每个用例结束都复位 ``paths.reset()`` 与 ``close_storage()``，避免污染其它测试。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.utils import paths


def _reset_paths() -> None:
    """还原固定路径到当前进程环境的推导结果（用例收尾统一调用）。"""

    paths.reset()


# ---------------------------------------------------------------------------
# A. 默认推导契约
# ---------------------------------------------------------------------------
def test_default_constants_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目的：未设环境变量时四个常量按「仓库根 + 固定子路径」推导。
    # 潜在缺陷：硬编码/相对路径/错拼文件名。
    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        repo_root = paths.repository_root()
        assert repo_root == paths.DATA_DIR
        assert paths.DATA_DIR.is_absolute()
        assert repo_root / ".cosir" / "logs" == paths.LOG_DIR
        assert repo_root / ".cosir" / "storage" / "app.sqlite3" == paths.DATABASE_FILE
        assert (
            repo_root / ".cosir" / "storage" / "langgraph_checkpoints.sqlite"
            == paths.CHECKPOINT_FILE
        )
        assert repo_root / ".cosir" / "runtime" == paths.RUNTIME_DIR
    finally:
        _reset_paths()


def test_repository_root_points_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目的：repository_root() 上溯四级应命中仓库根（含 apps 与 .git 之类锚点）。
    # 潜在缺陷：上溯层级错位。
    root = paths.repository_root()
    assert (root / "apps").is_dir() or (root / ".git").exists(), root


# ---------------------------------------------------------------------------
# B. reset() 按环境重算
# ---------------------------------------------------------------------------
def test_reset_only_data_dir_makes_log_dir_follow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：只设 CODING_AGENT_DATA_DIR 时 LOG_DIR 随之为 <DATA_DIR>/logs，主库/checkpoint 随之。
    # 潜在缺陷：LOG_DIR 未联动仍指向旧数据根。
    data_dir = tmp_path / "only_data"
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(data_dir))
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        assert data_dir == paths.DATA_DIR
        assert data_dir / ".cosir" / "logs" == paths.LOG_DIR
        assert data_dir / ".cosir" / "storage" / "app.sqlite3" == paths.DATABASE_FILE
        assert (
            data_dir / ".cosir" / "storage" / "langgraph_checkpoints.sqlite"
            == paths.CHECKPOINT_FILE
        )
        assert data_dir / ".cosir" / "runtime" == paths.RUNTIME_DIR
    finally:
        _reset_paths()


def test_reset_log_dir_is_always_nested_under_system_cosir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：日志目录不能脱离系统 .cosir 单独配置，避免产生第二个数据根。
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(data_dir))
    try:
        paths.reset()
        assert data_dir == paths.DATA_DIR
        assert data_dir / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()


def test_reset_restores_after_monkeypatch_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：monkeypatch 设置后 reset 生效，清理环境变量后再 reset 应回到仓库根。
    # 潜在缺陷：reset 缓存旧值或不重算。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "d"))
    paths.reset()
    assert tmp_path / "d" == paths.DATA_DIR

    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    paths.reset()
    assert paths.repository_root() == paths.DATA_DIR
    assert paths.repository_root() / ".cosir" / "logs" == paths.LOG_DIR


def test_reset_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 目的：连续多次 reset 结果稳定。潜在缺陷：状态累积导致漂移。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "idem"))
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        first = (paths.DATA_DIR, paths.LOG_DIR, paths.DATABASE_FILE, paths.CHECKPOINT_FILE)
        paths.reset()
        second = (paths.DATA_DIR, paths.LOG_DIR, paths.DATABASE_FILE, paths.CHECKPOINT_FILE)
        assert first == second
    finally:
        _reset_paths()


# ---------------------------------------------------------------------------
# C. override(**kwargs)
# ---------------------------------------------------------------------------
def test_override_partial_keeps_others(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目的：override 只改传入项，其余常量保持原值。潜在缺陷：整体重算导致被覆盖项丢失或误改。
    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    paths.reset()
    before_data = paths.DATA_DIR
    before_db = paths.DATABASE_FILE
    before_ckpt = paths.CHECKPOINT_FILE
    new_log = Path("/custom/log/dir")
    try:
        paths.override(LOG_DIR=new_log)
        assert new_log == paths.LOG_DIR
        assert before_data == paths.DATA_DIR
        assert before_db == paths.DATABASE_FILE
        assert before_ckpt == paths.CHECKPOINT_FILE
    finally:
        _reset_paths()


def test_override_all_four_keys(tmp_path: Path) -> None:
    # 目的：一次覆盖全部四项均生效。潜在缺陷：别名映射错误。
    paths.override(
        DATA_DIR=tmp_path / "a",
        LOG_DIR=tmp_path / "b",
        DATABASE_FILE=tmp_path / "c.sqlite3",
        CHECKPOINT_FILE=tmp_path / "d.sqlite3",
    )
    try:
        assert tmp_path / "a" == paths.DATA_DIR
        assert tmp_path / "b" == paths.LOG_DIR
        assert tmp_path / "c.sqlite3" == paths.DATABASE_FILE
        assert tmp_path / "d.sqlite3" == paths.CHECKPOINT_FILE
    finally:
        _reset_paths()


def test_override_unknown_key_raises_value_error(tmp_path: Path) -> None:
    # 目的：未知键抛 ValueError 且不产生副作用。潜在缺陷：静默接受未知键或写坏模块属性。
    before = (paths.DATA_DIR, paths.LOG_DIR, paths.DATABASE_FILE, paths.CHECKPOINT_FILE)
    with pytest.raises(ValueError):
        paths.override(NOT_A_PATH=tmp_path / "x")
    after = (paths.DATA_DIR, paths.LOG_DIR, paths.DATABASE_FILE, paths.CHECKPOINT_FILE)
    assert before == after


def test_override_unknown_key_after_valid_does_not_partially_apply(tmp_path: Path) -> None:
    # 目的：混入未知键时整体拒绝，不得先应用合法键再抛错（原子性）。潜在缺陷：先写后校验的部分应用。
    original = paths.DATA_DIR
    with pytest.raises(ValueError):
        paths.override(DATA_DIR=tmp_path / "changed", BOGUS=1)
    try:
        assert original == paths.DATA_DIR
    finally:
        _reset_paths()


def test_override_data_dir_updates_system_cosir_dir(tmp_path: Path) -> None:
    # 目的：override(DATA_DIR=...) 后 system_cosir_dir() == DATA_DIR/".cosir"。
    # 潜在缺陷：cosir_paths 读取静态快照或相对路径。
    from app.utils import cosir_paths

    paths.override(DATA_DIR=tmp_path / "sys")
    try:
        assert cosir_paths.system_cosir_dir() == tmp_path / "sys" / ".cosir"
    finally:
        _reset_paths()


# ---------------------------------------------------------------------------
# D. Settings 迁移
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attribute",
    ["LOG_DIR", "DATABASE_FILE", "CHECKPOINT_FILE", "DATA_DIR"],
)
def test_settings_has_no_path_attribute(attribute: str) -> None:
    # 目的：四个固定路径不再是 Settings 属性（迁移完成）。潜在缺陷：残留类属性导致双事实源。
    assert not hasattr(Settings, attribute), f"Settings 仍残留 {attribute}"


def test_settings_has_no_override_entry_point() -> None:
    # 目的：覆盖入口已退场，测试注入统一走 monkeypatch.setattr。
    # 潜在缺陷：无声地重新引入第二套进程级覆盖事实源。
    assert not hasattr(Settings, "override")


def test_settings_load_aligns_paths_with_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：Settings.load() 会调用 paths.reset() 使 DATA_DIR 与 CODING_AGENT_DATA_DIR 对齐。
    # 潜在缺陷：load 不再触发 reset。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "aligned"))
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    # 先人为打乱 paths，再 load 验证其被纠正。
    paths.override(DATA_DIR=tmp_path / "stale")
    try:
        Settings.load()
        assert tmp_path / "aligned" == paths.DATA_DIR
        assert tmp_path / "aligned" / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()
        Settings.load()


def test_settings_loads_environment_from_system_cosir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """运行配置只从系统 ``.cosir`` 读取，不依赖仓库或 backend 目录。"""

    data_dir = tmp_path / "data"
    env_file = data_dir / ".cosir" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("DEFAULT_LANGUAGE=en\n", encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(data_dir))
    monkeypatch.delenv("DEFAULT_LANGUAGE", raising=False)
    paths.reset()
    try:
        Settings._load_local_env()
        assert os.environ["DEFAULT_LANGUAGE"] == "en"
    finally:
        monkeypatch.delenv("DEFAULT_LANGUAGE", raising=False)
        _reset_paths()


# ---------------------------------------------------------------------------
# E. cosir_paths 导入与数据根
# ---------------------------------------------------------------------------
def test_cosir_paths_importable_without_cycle() -> None:
    # 目的：cosir_paths 可独立导入（无循环导入）。潜在缺陷：模块级相互导入导致 ImportError。
    module = importlib.import_module("app.utils.cosir_paths")
    assert module.COSIR_DIR_NAME == ".cosir"


def test_system_cosir_dir_reads_paths_data_dir(tmp_path: Path) -> None:
    # 目的：system_cosir_dir() 动态读取 paths.DATA_DIR（非硬编码/相对）。潜在缺陷：读静态快照。
    from app.utils import cosir_paths

    assert cosir_paths.system_cosir_dir() == paths.SYSTEM_COSIR_DIR
    paths.override(DATA_DIR=tmp_path / "moved")
    try:
        assert cosir_paths.system_cosir_dir() == tmp_path / "moved" / ".cosir"
    finally:
        _reset_paths()


# ---------------------------------------------------------------------------
# F. store_engines 使用 paths 常量
# ---------------------------------------------------------------------------
def test_init_storage_creates_db_and_checkpoint_parent(tmp_path: Path) -> None:
    # 目的：override 指向 tmp 后 init_storage() 在该目录创建主库文件与 checkpoint 父目录。
    # 潜在缺陷：仍写死旧路径。
    from app.storage import store_engines

    store_engines.close_storage()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    db_file = db_dir / "app.sqlite3"
    ckpt_file = db_dir / "nested" / "checkpoints.sqlite3"
    paths.override(DATABASE_FILE=db_file, CHECKPOINT_FILE=ckpt_file)
    try:
        store_engines.init_storage()
        assert db_file.exists(), "init_storage 未在 paths.DATABASE_FILE 创建库文件"
        assert ckpt_file.parent.is_dir(), "checkpoint 父目录未创建"
        assert store_engines.checkpoint_path() == str(ckpt_file)
    finally:
        store_engines.close_storage()
        _reset_paths()


def test_init_storage_switches_engine_on_path_change(tmp_path: Path) -> None:
    # 目的：路径变化后重新 init_storage 应切换到新库（旧引擎释放、新库文件出现）。
    # 潜在缺陷：路径变化后仍复用旧引擎。
    from app.storage import store_engines

    store_engines.close_storage()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    db_a = dir_a / "app.sqlite3"
    db_b = dir_b / "app.sqlite3"
    try:
        paths.override(DATABASE_FILE=db_a, CHECKPOINT_FILE=dir_a / "ck.sqlite3")
        store_engines.init_storage()
        engine_a = store_engines.main_engine()
        assert db_a.exists()

        paths.override(DATABASE_FILE=db_b, CHECKPOINT_FILE=dir_b / "ck.sqlite3")
        store_engines.init_storage()
        engine_b = store_engines.main_engine()
        assert engine_b is not engine_a, "路径变化后仍复用旧引擎"
        assert db_b.exists()
        assert store_engines.checkpoint_path() == str(dir_b / "ck.sqlite3")
    finally:
        store_engines.close_storage()
        _reset_paths()


def test_init_storage_same_path_is_noop_reuse(tmp_path: Path) -> None:
    # 目的：同路径二次 init_storage 复用同一引擎（幂等）。潜在缺陷：每次重建引擎破坏连接池单例语义。
    from app.storage import store_engines

    store_engines.close_storage()
    db_dir = tmp_path / "same"
    db_dir.mkdir()
    paths.override(DATABASE_FILE=db_dir / "app.sqlite3", CHECKPOINT_FILE=db_dir / "ck.sqlite3")
    try:
        store_engines.init_storage()
        engine_first = store_engines.main_engine()
        store_engines.init_storage()
        engine_second = store_engines.main_engine()
        assert engine_first is engine_second
    finally:
        store_engines.close_storage()
        _reset_paths()


# ---------------------------------------------------------------------------
# G. 边界：空串 / 纯空白 DATA_DIR
# ---------------------------------------------------------------------------
def test_empty_data_dir_env_falls_back_to_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目的：CODING_AGENT_DATA_DIR 为空串时应回落仓库根（falsy 判定）。
    # 潜在缺陷：空串被当作 Path("") 落到当前目录。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", "")
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        assert paths.repository_root() == paths.DATA_DIR
        assert paths.DATA_DIR.is_absolute()
        assert paths.repository_root() / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()


def test_whitespace_data_dir_env_falls_back_to_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目的：纯空白（"   "）按「未设置」处理并回落仓库根（env 取值先 strip）。
    # 潜在缺陷：空白被当作相对路径落到 cwd。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", "   ")
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        assert paths.repository_root() == paths.DATA_DIR
        assert paths.DATA_DIR.is_absolute()
        assert paths.repository_root() / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()


def test_reset_discards_prior_override(tmp_path: Path) -> None:
    # 目的：override 后 reset 应丢弃覆盖值，回到环境推导（避免覆盖跨用例泄漏）。
    # 潜在缺陷：override 值被固化。
    paths.override(LOG_DIR=tmp_path / "temp_logs", DATA_DIR=tmp_path / "temp_data")
    assert tmp_path / "temp_logs" == paths.LOG_DIR
    try:
        paths.reset()
        assert paths.repository_root() == paths.DATA_DIR
        assert paths.repository_root() / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()


def test_override_database_file_does_not_touch_data_dir(tmp_path: Path) -> None:
    # 目的：只覆盖 DATABASE_FILE 时 DATA_DIR/LOG_DIR/CHECKPOINT_FILE 不变（无隐式联动）。
    # 潜在缺陷：误触发全量重算。
    paths.override(DATA_DIR=tmp_path / "root", LOG_DIR=tmp_path / "logs")
    try:
        ckpt_before = paths.CHECKPOINT_FILE
        paths.override(DATABASE_FILE=tmp_path / "custom.sqlite3")
        assert tmp_path / "root" == paths.DATA_DIR
        assert tmp_path / "logs" == paths.LOG_DIR
        assert ckpt_before == paths.CHECKPOINT_FILE
        assert tmp_path / "custom.sqlite3" == paths.DATABASE_FILE
    finally:
        _reset_paths()


def test_override_log_dir_does_not_touch_data_dir_derived_files(tmp_path: Path) -> None:
    # 目的：只覆盖 LOG_DIR 时不动主库/checkpoint（它们锚定 DATA_DIR 而非 LOG_DIR）。
    # 潜在缺陷：错误地以 LOG_DIR 推导 storage。
    paths.override(DATA_DIR=tmp_path / "root", LOG_DIR=tmp_path / "root" / "logs")
    db_before = paths.DATABASE_FILE
    ckpt_before = paths.CHECKPOINT_FILE
    try:
        paths.override(LOG_DIR=tmp_path / "elsewhere")
        assert db_before == paths.DATABASE_FILE
        assert ckpt_before == paths.CHECKPOINT_FILE
    finally:
        _reset_paths()


def test_relative_data_dir_env_is_not_made_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目的：文档声称 DATA_DIR 是「绝对锚点」，但相对值不被 resolve。记录当前实现的真实行为。
    # 当前实现：Path("relative/data") 原样保留（非绝对）。
    # 若下游依赖 is_absolute 会出错——潜在缺陷点。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", "relative/data")
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        assert Path("relative/data") == paths.DATA_DIR
        assert not paths.DATA_DIR.is_absolute(), "本用例记录相对值未被绝对化的事实"
        assert Path("relative/data") / ".cosir" / "logs" == paths.LOG_DIR
    finally:
        _reset_paths()


# ---------------------------------------------------------------------------
# H. 测试灵敏度自证（运行时内存突变，不落盘、不改生产文件）
# ---------------------------------------------------------------------------
def test_sensitivity_to_data_dir_mutation_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：模拟 DATA_DIR 被错误实现为常量时的可检出性——证明断言非弱断言。潜在缺陷：弱断言漏检。
    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    try:
        paths.reset()
        expected = paths.repository_root() / ".cosir" / "logs"
        assert expected == paths.LOG_DIR
        # 注入错误的 LOG_DIR：正确断言必须失败（此处反向验证断言确实在比较）。
        paths.override(LOG_DIR=tmp_path / "wrong")
        assert expected != paths.LOG_DIR
    finally:
        _reset_paths()


def test_sensitivity_to_checkpoint_name_mutation_runtime(tmp_path: Path) -> None:
    # 目的：模拟 checkpoint 文件名被写错时可检出。潜在缺陷：文件名硬编码错拼而测试未捕获。
    paths.override(CHECKPOINT_FILE=tmp_path / "langgraph_checkpoint.sqlite")  # 少一个 s
    try:
        assert paths.CHECKPOINT_FILE.name != "langgraph_checkpoints.sqlite"
    finally:
        _reset_paths()
        # 复位后应恢复正确文件名。
        assert paths.CHECKPOINT_FILE.name == "langgraph_checkpoints.sqlite"


# ---------------------------------------------------------------------------
# I. paths 模块内部契约（推导表 / env 归一化 / override 语义）
# ---------------------------------------------------------------------------
def _snapshot() -> tuple[Path, Path, Path, Path]:
    """抓取四个固定路径常量的当前值，用于原子性与污染断言。"""

    return (paths.DATA_DIR, paths.LOG_DIR, paths.DATABASE_FILE, paths.CHECKPOINT_FILE)


@pytest.fixture(autouse=True)
def _restore_paths_after_each() -> None:
    """每个用例收尾无条件复位固定路径，作为跨用例污染的第二道防线。"""

    yield
    paths.reset()


def test_all_exports_cover_derivers_plus_helpers() -> None:
    # 目的：__all__ 应恰好等于推导表键 + {override, repository_root, reset}。
    # 潜在缺陷：__all__ 手抄漂移致缺失/多余导出。
    assert set(paths.__all__) == set(paths._DERIVERS) | {
        "override",
        "repository_root",
        "reset",
    }


def test_derivers_keys_match_module_constant_names() -> None:
    # 目的：推导表键必须与实际模块常量一一对应（键即常量名）。
    # 潜在缺陷：键名与常量名不一致致 override/reset 写错属性。
    for name in paths._DERIVERS:
        assert hasattr(paths, name), f"推导表键 {name} 无对应模块常量"


def test_env_path_unset_returns_none() -> None:
    # 目的：_env_path 对未设置变量返回 None。潜在缺陷：回落成 Path("")。
    assert paths._env_path("CODING_AGENT_DEFINITELY_UNSET_XYZ") is None


def test_env_path_blank_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目的：空串/纯空白（含 tab/newline）一律返回 None。
    # 潜在缺陷：空白被当作有效相对路径。
    for blank in ("", "   ", "\t", "\n", " \t \n "):
        monkeypatch.setenv("CODING_AGENT_DATA_DIR", blank)
        assert paths._env_path("CODING_AGENT_DATA_DIR") is None, repr(blank)


def test_env_path_strips_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：有内容时 strip 首尾空白。潜在缺陷：尾随空格进入路径。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", f"  {tmp_path / 'padded'}  ")
    assert paths._env_path("CODING_AGENT_DATA_DIR") == tmp_path / "padded"


def test_log_dir_is_derived_even_when_legacy_env_is_blank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 目的：LOG_DIR 不再接受独立环境变量，始终位于系统 .cosir 下。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CODING_AGENT_LOG_DIR", "   ")
    paths.reset()
    assert tmp_path / "d" / ".cosir" / "logs" == paths.LOG_DIR


def test_override_type_error_is_atomic_no_partial_write(tmp_path: Path) -> None:
    # 目的：合法 Path 与非法值混用时整体拒绝，无部分写入。潜在缺陷：先写后校验。
    before = _snapshot()
    with pytest.raises(TypeError):
        paths.override(LOG_DIR=tmp_path / "valid", DATABASE_FILE="not-a-path-object")
    assert _snapshot() == before


@pytest.mark.parametrize("bad", ["not-a-path", 123, None, 1.5, b"/bytes", ["x"], {"a": 1}])
def test_override_non_path_value_raises_type_error(bad: object, tmp_path: Path) -> None:
    # 目的：非 pathlib.Path 值抛 TypeError 且零写入。
    # 潜在缺陷：静默接受 str 致下游类型不一致崩溃。
    before = _snapshot()
    with pytest.raises(TypeError):
        paths.override(LOG_DIR=bad)
    assert _snapshot() == before


# 具体路径类平台相关（Windows 为 WindowsPath，POSIX 为 PosixPath）；动态派生其子类，
# 验证 isinstance 语义放行 Path 子类（而非误用 type(x) is Path）。
_ConcretePathType = type(Path())
_PathSubclass = type("_PathSubclass", (_ConcretePathType,), {})


def test_override_accepts_path_subclass(tmp_path: Path) -> None:
    # 目的：pathlib.Path 子类实例应被接受。潜在缺陷：用 type(x) is Path 误拒子类。
    sub = _PathSubclass(tmp_path / "subclass")
    assert isinstance(sub, Path)
    paths.override(LOG_DIR=sub)
    assert sub == paths.LOG_DIR


def test_override_empty_kwargs_is_noop() -> None:
    # 目的：无参数 override() 不改动任何常量。潜在缺陷：空调用误触发重算。
    before = _snapshot()
    paths.override()
    assert _snapshot() == before


def test_reset_after_corrupted_globals_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目的：模块常量被外部误写后 reset 仍能恢复。潜在缺陷：reset 依赖缓存而盖不掉脏值。
    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    paths.reset()
    clean = _snapshot()

    paths.DATA_DIR = Path("corrupted")  # type: ignore[misc]
    paths.LOG_DIR = Path("corrupted_logs")  # type: ignore[misc]
    assert _snapshot() != clean

    paths.reset()
    assert _snapshot() == clean


def test_single_dot_data_dir_env_is_not_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目的："." 非空故按有效路径处理且不 resolve 成 cwd——记录真实行为。
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", ".")
    monkeypatch.delenv("CODING_AGENT_LOG_DIR", raising=False)
    paths.reset()
    assert Path(".") == paths.DATA_DIR
    assert not paths.DATA_DIR.is_absolute()
