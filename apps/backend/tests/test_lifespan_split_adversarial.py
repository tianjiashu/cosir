"""``app.lifespan`` 拆分后的对抗性回归测试（只测不改）。

背景：本次把 ``app.py`` 中「启动/关闭生命周期编排」整体摘出到 ``app.lifespan``，
``app.py`` 只保留装配。本文件独立验证拆分后的行为契约是否仍与拆分前一致、边界是否
仍然正确。**不修改任何生产代码**，只通过 monkeypatch 打桩隔离重量级依赖。

对抗维度：

  A. 导入与装配契约：``import app.app`` 可用、``app.app.app`` 是 FastAPI 单例、
     lifespan 绑定到 ``app.lifespan.lifespan``、各域路由注册在同一实例上。
  B. ``bootstate`` 三态守卫：``ready`` / ``failed``（三态/损坏/缺失/非 booting 不得改写）
     / ``stopped``，以及环境变量未设置时的安全 no-op。
  C. ``_ensure_system_cosir_dir()``：幂等、同名文件降级。
  D. ``lifespan`` 异常路径：yield 前抛错必须原样冒泡、标 ``failed``、仍调用
     ``shutdown_logging()``；正常关闭与关闭期异常路径。

依赖隔离原则：完整 lifespan 会连真实 SQLite 与 LangGraph，这里统一把 ``app.lifespan``
命名空间内已导入的符号替换为记录型 stub，只驱动编排逻辑本身，不启动任何真实服务。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import app.app as app_module
import app.lifespan as lifespan_module
from app.utils import paths

# ---------------------------------------------------------------------------
# 通用夹具 / 辅助
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_paths() -> Any:
    """每个用例结束后还原 ``app.utils.paths`` 的模块级路径常量，避免跨用例污染。"""

    yield
    paths.reset()


def _boot_state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 ``CODING_AGENT_BOOT_STATE_FILE`` 指向 tmp 下的启动状态文件并返回该路径。"""

    boot_file = tmp_path / "backend.bootstate.json"
    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", str(boot_file))
    return boot_file


def _write_phase(path: Path, phase: str) -> None:
    """写入一个只含 ``phase`` 的最小启动状态文件（模拟 supervisor 初值）。"""

    path.write_text(json.dumps({"phase": phase}, ensure_ascii=False), encoding="utf-8")


def _read_state(path: Path) -> dict[str, Any]:
    """读回启动状态 JSON 并断言其可解析。"""

    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A. 导入与装配契约
# ---------------------------------------------------------------------------


def test_app_module_exposes_fastapi_singleton() -> None:
    """``app.app.app`` 必须是 FastAPI 实例（拆分后单例契约不变）。"""

    assert isinstance(app_module.app, FastAPI)


def test_app_lifespan_is_bound_to_lifespan_module() -> None:
    """``app.app.app`` 的 lifespan 必须绑定 ``app.lifespan.lifespan``（拆分后装配点唯一）。

    FastAPI 把 ``lifespan=`` 包装进 ``router.lifespan_context``；由于 ``@asynccontextmanager``
    包装后的对象不保真 ``__eq__``，这里用 ``__wrapped__`` 做同一性判定。
    """

    bound = app_module.app.router.lifespan_context
    assert getattr(bound, "__wrapped__", None) is lifespan_module.lifespan.__wrapped__


def test_import_module_returns_same_singleton() -> None:
    """``importlib.import_module("app.app")`` 必须复用 ``sys.modules`` 中的同一实例。"""

    again = importlib.import_module("app.app")
    assert again is app_module
    assert again.app is app_module.app


def test_lifespan_module_does_not_import_app_app() -> None:
    """``app.lifespan`` 顶层不得反向导入 ``app.app``（否则形成循环导入）。"""

    source = Path(lifespan_module.__file__).read_text(encoding="utf-8")
    assert "from app.app import" not in source
    assert "import app.app" not in source


def test_all_domain_routes_registered_on_same_app() -> None:
    """各域路由模块级装饰器注册到同一 ``app`` 实例：OpenAPI 路径必须齐全。"""

    oa_paths = set(app_module.app.openapi()["paths"])
    expected = {
        "/health",
        "/workspaces",
        "/workspaces/{workspace_id}",
        "/workspaces/{workspace_id}/tasks",
        "/tasks/{task_id}",
        "/tasks/{task_id}/fork",
        "/tasks/{task_id}/attachments",
        "/tasks/{task_id}/attachments/{asset_id}",
        "/tasks/{task_id}/attachments/{asset_id}/content",
        "/providers",
        "/providers/catalog",
        "/providers/{provider_id}",
        "/providers/{provider_id}/test",
        "/models",
        "/assistant",
        "/tasks/{task_id}/assistant/attach",
        "/tasks/{task_id}/assistant/state",
        "/runs/{run_id}/cancel",
        "/runs/{run_id}/tool-calls/{tool_call_id}/cancel",
    }
    missing = expected - oa_paths
    assert missing == set(), f"拆分后丢失的域路由：{sorted(missing)}"


def test_domain_route_modules_share_the_singleton() -> None:
    """域路由模块内 ``from app.app import app`` 取到的必须与装配入口同一对象。"""

    for module_name in (
        "app.api.tasks_api",
        "app.api.workspaces_api",
        "app.api.providers_api",
        "app.api.models_api",
        "app.assistant_transport.assistant_api",
    ):
        module = sys.modules.get(module_name) or importlib.import_module(module_name)
        assert module.app is app_module.app, module_name


def test_cors_middleware_installed_after_split() -> None:
    """CORS 中间件必须仍装配在 app 上（拆分后装配逻辑未丢失）。"""

    from fastapi.middleware.cors import CORSMiddleware

    assert any(m.cls is CORSMiddleware for m in app_module.app.user_middleware)


# ---------------------------------------------------------------------------
# B. bootstate 三态守卫
# ---------------------------------------------------------------------------


def test_mark_boot_ready_writes_ready_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_mark_boot_ready()`` 应写入 phase=ready 且带 ``step="app_ready"``。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    lifespan_module._mark_boot_ready()
    state = _read_state(boot_file)
    assert state["phase"] == "ready"
    assert state["step"] == "app_ready"


def test_mark_boot_stopped_writes_stopped_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_mark_boot_stopped()`` 应写入 phase=stopped。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    lifespan_module._mark_boot_stopped()
    assert _read_state(boot_file)["phase"] == "stopped"


def test_mark_boot_failed_rewrites_booting_to_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """当前 phase=booting 时，``_mark_boot_failed`` 必须改写为 failed 并带结构化字段。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")

    try:
        raise ValueError("boom-payload")
    except ValueError as exc:
        lifespan_module._mark_boot_failed(exc)

    state = _read_state(boot_file)
    assert state["phase"] == "failed"
    assert state["step"] == "lifespan"
    assert state["error_type"] == "ValueError"
    assert state["error_message"] == "boom-payload"
    assert "ValueError" in state["traceback"]


@pytest.mark.parametrize("phase", ["ready", "stopped", "failed"])
def test_mark_boot_failed_does_not_overwrite_terminal_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """非 booting 的终态（ready/stopped/failed）绝不能被 ``_mark_boot_failed`` 改写。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, phase)

    lifespan_module._mark_boot_failed(RuntimeError("late-failure"))

    assert _read_state(boot_file) == {"phase": phase}


def test_mark_boot_failed_when_file_missing_does_not_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动状态文件不存在时，``_mark_boot_failed`` 不得创建文件、不得抛异常。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    assert not boot_file.exists()

    lifespan_module._mark_boot_failed(RuntimeError("no-file"))

    assert not boot_file.exists()


def test_mark_boot_failed_when_file_corrupt_does_not_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """JSON 损坏时 ``_mark_boot_failed`` 必须安全跳过（不得抛异常、不得覆盖原文件）。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text("{not valid json", encoding="utf-8")

    lifespan_module._mark_boot_failed(RuntimeError("corrupt"))

    assert boot_file.read_text(encoding="utf-8") == "{not valid json"


def test_mark_boot_failed_when_payload_not_object_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """JSON 可解析但顶层不是对象（如 ``[1,2]``）时不得因 ``.get`` 缺失而崩溃。

    期望：安全跳过（既不抛异常，也不改写文件）。这是「损坏输入」的极端形态。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text("[1, 2, 3]", encoding="utf-8")

    lifespan_module._mark_boot_failed(RuntimeError("non-object"))

    assert boot_file.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_mark_boot_failed_when_phase_missing_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """文件存在但没有 ``phase`` 键时视为非 booting：不得改写。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text(json.dumps({"step": "start"}), encoding="utf-8")

    lifespan_module._mark_boot_failed(RuntimeError("no-phase"))

    assert _read_state(boot_file) == {"step": "start"}


# 修复复验补充的对抗用例。
# 缺陷类型：非对象 JSON 载荷导致 ``current.get`` 抛 ``AttributeError``（对应已修复的
# ``isinstance(current, dict)`` 守卫）；同时区分「跳过」与「误写 failed」两种行为，
# 避免守卫从「安全跳过」放宽成「任何输入都写 failed」。

# 非对象 JSON 载荷的各种形态与其原始文本：
#   - 容器型（list）、标量型（str/int/bool/null）都必须原样跳过。
#   - 断言的是**文件字节级不变**：任何改写（尤其误写 failed）都会立刻暴露。
_NOT_OBJECT_PAYLOADS: list[tuple[str, str]] = [
    ("json_array", "[1, 2, 3]"),
    ("json_string", '"booting"'),
    ("json_number", "42"),
    ("json_null", "null"),
    ("json_true", "true"),
]


@pytest.mark.parametrize(
    ("payload_name", "raw"),
    _NOT_OBJECT_PAYLOADS,
    ids=[name for name, _ in _NOT_OBJECT_PAYLOADS],
)
def test_mark_boot_failed_with_non_object_payload_skips_and_keeps_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload_name: str, raw: str
) -> None:
    """非对象顶层（数组/字符串/数字/null/true）必须「安全跳过且不覆盖原文件」。

    区分两种错误形态：
      1. 抛 ``AttributeError``（``.get`` 不存在）——核心回归点；
      2. 不抛但把非对象载荷改写成了 failed —— 属于「误判」缺陷（载荷里根本没有
         ``phase=booting`` 的语义），会让 supervisor 读到错误状态。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text(raw, encoding="utf-8")
    before_bytes = boot_file.read_bytes()

    lifespan_module._mark_boot_failed(RuntimeError(f"non-object-{payload_name}"))

    assert boot_file.read_bytes() == before_bytes, (
        f"{payload_name} 载荷被误改写：{boot_file.read_text(encoding='utf-8')}"
    )


@pytest.mark.parametrize(
    ("payload_name", "raw"),
    _NOT_OBJECT_PAYLOADS,
    ids=[name for name, _ in _NOT_OBJECT_PAYLOADS],
)
def test_mark_boot_failed_non_object_payload_no_temp_file_left_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload_name: str, raw: str
) -> None:
    """跳过分支不得在目录里遗留 ``*.tmp`` 残留（跳过应完全不触碰文件系统）。

    若实现改成「先临时文件后判断」之类的写法，会留下垃圾临时文件；本用例专门暴露这类
    副作用泄漏（原实现只读不写，跳过时应零副作用）。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text(raw, encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    lifespan_module._mark_boot_failed(RuntimeError(f"non-object-{payload_name}"))

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_mark_boot_failed_still_rewrites_booting_after_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """回归守卫：``isinstance`` 检查不得把合法 ``{"phase": "booting"}`` 场景一起挡掉。

    与既有 ``test_mark_boot_failed_rewrites_booting_to_failed`` 互补：这里在「同一用例内」
    先用非对象载荷触发一次跳过，再放入合法 booting 载荷，确认守卫状态无残留、正常改写仍生效。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text("[1, 2, 3]", encoding="utf-8")
    lifespan_module._mark_boot_failed(RuntimeError("first-skip"))
    assert boot_file.read_text(encoding="utf-8") == "[1, 2, 3]"

    _write_phase(boot_file, "booting")
    try:
        raise KeyError("second-real-failure")
    except KeyError as exc:
        lifespan_module._mark_boot_failed(exc)

    state = _read_state(boot_file)
    assert state["phase"] == "failed"
    assert state["step"] == "lifespan"
    assert state["error_type"] == "KeyError"
    assert state["error_message"] == "'second-real-failure'"


@pytest.mark.parametrize(
    ("payload_name", "raw"),
    [
        ("phase_int", '{"phase": 123}'),
        ("phase_null", '{"phase": null}'),
        ("phase_bool", '{"phase": true}'),
        ("phase_list", '{"phase": ["booting"]}'),
        ("phase_nested", '{"phase": {"name": "booting"}}'),
        ("phase_empty_str", '{"phase": ""}'),
        ("phase_similar", '{"phase": "Booting"}'),
        ("phase_with_space", '{"phase": " booting"}'),
        ("phase_from_list_like", '[{"phase": "booting"}]'),
    ],
)
def test_mark_boot_failed_with_non_string_or_non_booting_phase_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload_name: str, raw: str
) -> None:
    """非字符串 phase（数字/null/布尔/容器）或近似值必须安全跳过，不得抛异常、不得改写。

    ``{"phase": 123}`` 与 ``"booting"`` 不相等，属「非 booting」；实现若用
    ``in`` / 类型强转 / 大小写归一化做匹配，就会把它误判为 booting 并改写（暴露误判缺陷）。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text(raw, encoding="utf-8")
    before_bytes = boot_file.read_bytes()

    lifespan_module._mark_boot_failed(RuntimeError(f"odd-phase-{payload_name}"))

    assert boot_file.read_bytes() == before_bytes, (
        f"{payload_name} 被误改写：{boot_file.read_text(encoding='utf-8')}"
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"phase": "booting", "extra": [1, 2]}',
        '{"phase": "booting", "step": "start", "nested": {"a": 1}}',
        '{  "phase"  :  "booting"  }',
    ],
)
def test_mark_boot_failed_rewrites_booting_with_extra_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    """正路径扩展：带额外键 / 任意空白的合法 booting 对象仍必须被改写为 failed。

    对照上面「非 booting 一律跳过」的用例，锁定判据是**值相等**而非「键必须唯一」或
    格式严格：含额外键时若实现改成「只接受恰好一个键」就会漏报失败（暴露过严判据缺陷）。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    boot_file.write_text(raw, encoding="utf-8")

    lifespan_module._mark_boot_failed(RuntimeError("with-extra"))

    state = _read_state(boot_file)
    assert state["phase"] == "failed"
    assert state["step"] == "lifespan"
    assert state["error_message"] == "with-extra"


@pytest.mark.parametrize("phase", ["booting", "ready", "failed", "stopped"])
def test_write_bootstate_always_writes_json_object(tmp_path: Path, phase: str) -> None:
    """不变量：``write_bootstate`` 写出的永远是 JSON 对象，且 ``phase`` 保真为字符串。

    这条不变量是 ``_mark_boot_failed`` 里 ``isinstance(current, dict)`` 守卫的「合法输入
    形状有真实来源」依据：只要写入方永远写对象，非对象载荷就只可能来自外部破坏/截断，
    守卫跳过是正确语义。若写入方改为写数组/裸标量，本用例会失败，守卫的前提即被破坏。
    """

    from app.bootstate import write_bootstate

    target = tmp_path / "bootstate.json"
    write_bootstate(target, phase, step="start")

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"写入结果顶层不是对象：{type(parsed).__name__}"
    assert isinstance(parsed["phase"], str)
    assert parsed["phase"] == phase


def test_write_bootstate_omits_none_values_keeps_phase(tmp_path: Path) -> None:
    """不变量补充：``None`` 值被裁剪掉后 ``phase`` 仍在，形状仍是对象（供守卫消费）。"""

    from app.bootstate import write_bootstate

    target = tmp_path / "bootstate.json"
    write_bootstate(target, "booting")

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == {"phase": "booting"}
    assert isinstance(parsed, dict)


def test_bootstate_round_trip_feeds_mark_boot_failed_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端不变量：``write_bootstate`` 产物的形状必须能被 ``_mark_boot_failed`` 正常消费。

    覆盖真实来源：先用生产写入器写出 booting（模拟 supervisor 初值），再触发失败标记，
    必须成功改写为 failed —— 证明上面的 ``isinstance`` 守卫没有把合法来源一起挡掉。
    """

    from app.bootstate import write_bootstate

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    write_bootstate(boot_file, "booting", step="start")
    assert _read_state(boot_file)["phase"] == "booting"

    lifespan_module._mark_boot_failed(ValueError("round-trip-failure"))

    state = _read_state(boot_file)
    assert state["phase"] == "failed"
    assert state["error_type"] == "ValueError"
    assert state["error_message"] == "round-trip-failure"


def test_boot_markers_are_noop_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """环境变量未设置时，三个标记函数必须是安全 no-op：不得建文件、不得抛异常。"""

    monkeypatch.delenv("CODING_AGENT_BOOT_STATE_FILE", raising=False)
    before = sorted(p.name for p in tmp_path.iterdir())

    lifespan_module._mark_boot_ready()
    lifespan_module._mark_boot_stopped()
    lifespan_module._mark_boot_failed(RuntimeError("env-unset"))

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after == []


def test_boot_markers_noop_when_env_is_empty_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """环境变量为空串时按未设置处理：不得创建文件。"""

    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", "")
    before = sorted(p.name for p in tmp_path.iterdir())

    lifespan_module._mark_boot_ready()
    lifespan_module._mark_boot_stopped()

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_mark_boot_ready_creates_parent_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """启动状态文件父目录不存在时，写入必须自动建父目录（不能因缺目录静默失败）。"""

    boot_file = tmp_path / "nested" / "runtime" / "backend.bootstate.json"
    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", str(boot_file))

    lifespan_module._mark_boot_ready()

    assert boot_file.is_file()
    assert _read_state(boot_file)["phase"] == "ready"


def test_mark_boot_ready_write_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动状态文件不可写（父路径被同名文件占位）时不得抛异常，须降级记录错误日志。

    ``write_bootstate`` 的边界契约是「写入失败不阻断主流程」；标记函数作为其调用方
    必须把该 OSError 一直吞到调用点，不得让启动流程因写不了状态文件而崩溃。
    """

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    # 父路径是文件 → mkdir / mkstemp 必然失败。
    boot_file = blocker / "backend.bootstate.json"
    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", str(boot_file))

    lifespan_module._mark_boot_ready()  # 不得抛异常

    # 父路径仍是文件：写入被安全吞掉，不产生半成品状态文件。
    assert blocker.is_file()
    assert not (blocker / "backend.bootstate.json").exists()


def test_mark_boot_ready_write_failure_logs_stderr_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """写入失败时必须按 ``write_bootstate`` 契约落一条 ``bootstate.error.log`` 兜底记录。"""

    read_only_root = tmp_path / "ro"
    read_only_root.mkdir()
    boot_file = read_only_root / "backend.bootstate.json"
    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", str(boot_file))

    # 让 mkstemp 失败，模拟目录不可写（Windows 下用目录占位更稳）。
    monkeypatch.setattr(
        "app.bootstate.tempfile.mkstemp",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
    )

    lifespan_module._mark_boot_ready()  # 不得抛异常

    error_log = read_only_root / "bootstate.error.log"
    assert error_log.is_file()
    assert "bootstate" in error_log.read_text(encoding="utf-8")
    assert capsys.readouterr().err != ""


def test_mark_boot_failed_write_failure_does_not_mask_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bootstate 写入失败时，``_mark_boot_failed`` 必须保留原始异常语义（自身不抛出）。"""

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    boot_file = blocker / "backend.bootstate.json"
    monkeypatch.setenv("CODING_AGENT_BOOT_STATE_FILE", str(boot_file))

    # 即使写 failed 失败，调用点也不应看到新异常干扰原始错误上报。
    lifespan_module._mark_boot_failed(RuntimeError("original-cause"))

    assert blocker.is_file()


# ---------------------------------------------------------------------------
# C. _ensure_system_cosir_dir
# ---------------------------------------------------------------------------


def test_ensure_system_cosir_dir_is_idempotent(tmp_path: Path) -> None:
    """幂等：连续调用不抛异常且目录存在（第二次走 exist_ok 分支）。"""

    paths.override(DATA_DIR=tmp_path / "sys")

    lifespan_module._ensure_system_cosir_dir()
    lifespan_module._ensure_system_cosir_dir()

    assert (tmp_path / "sys" / ".cosir").is_dir()


def test_ensure_system_cosir_dir_creates_nested_parents(tmp_path: Path) -> None:
    """数据根尚不存在时必须按 ``parents=True`` 递归创建出 ``.cosir``。"""

    paths.override(DATA_DIR=tmp_path / "a" / "b" / "c")

    lifespan_module._ensure_system_cosir_dir()

    assert (tmp_path / "a" / "b" / "c" / ".cosir").is_dir()


def test_ensure_system_cosir_dir_degrades_when_name_is_file(tmp_path: Path) -> None:
    """系统 ``.cosir`` 位置被同名文件占位：必须降级（不抛异常 + 写 error 事件）。"""

    from app.config.logging.logger import log as backend_log

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    previous_level = backend_log.level
    backend_log.addHandler(handler)
    backend_log.setLevel(logging.DEBUG)

    blocker = tmp_path / ".cosir"
    blocker.write_text("i am a file", encoding="utf-8")
    paths.override(DATA_DIR=tmp_path)
    try:
        lifespan_module._ensure_system_cosir_dir()  # 不得抛异常
    finally:
        backend_log.removeHandler(handler)
        backend_log.setLevel(previous_level)

    events = [r.getMessage() for r in records]
    assert "system_cosir_init_failed" in events, events
    failed = next(r for r in records if r.getMessage() == "system_cosir_init_failed")
    assert failed.levelno == logging.ERROR
    assert blocker.is_file()


def test_ensure_system_cosir_dir_logs_init_event(tmp_path: Path) -> None:
    """成功创建时写 ``system_cosir_initialized`` info 事件（正路径可观测性）。"""

    from app.config.logging.logger import log as backend_log

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    previous_level = backend_log.level
    backend_log.addHandler(handler)
    backend_log.setLevel(logging.DEBUG)

    paths.override(DATA_DIR=tmp_path / "sys")
    try:
        lifespan_module._ensure_system_cosir_dir()
    finally:
        backend_log.removeHandler(handler)
        backend_log.setLevel(previous_level)

    initialized = next(
        (r for r in records if r.getMessage() == "system_cosir_initialized"), None
    )
    assert initialized is not None
    assert initialized.levelno == logging.INFO


# ---------------------------------------------------------------------------
# D. lifespan 异常 / 关闭路径（用 stub 隔离真实依赖）
# ---------------------------------------------------------------------------


class _Recorder:
    """记录型 stub：替换 ``app.lifespan`` 命名空间下的重量级依赖。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.shutdown_logging_calls = 0
        self.fail_step: str | None = None

    def _boom_if(self, step: str) -> None:
        if self.fail_step == step:
            raise RuntimeError(f"stub failure at {step}")


@pytest.fixture
def stubbed_lifespan(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """把 ``app.lifespan`` 的重依赖替换为可观测 stub，避免真实 DB/网络/服务启动。

    替换的是 ``app.lifespan`` 模块命名空间里已导入的符号（lifespan 内部直接按局部名解析），
    因此不会改动生产代码，也不触发真实存储/终端/Hook 副作用。
    """

    recorder = _Recorder()

    def record(step: str, result: Any = None) -> Any:
        def _stub(*args: Any, **kwargs: Any) -> Any:
            recorder.calls.append(step)
            recorder._boom_if(step)
            return result

        return _stub

    monkeypatch.setattr(
        lifespan_module, "install_logging_for_current_process", record("install_logging")
    )
    monkeypatch.setattr(lifespan_module, "shutdown_logging", _shutdown_logging_stub(recorder))
    monkeypatch.setattr(lifespan_module.Settings, "load", staticmethod(record("settings_load")))
    monkeypatch.setattr(lifespan_module, "initialize_service_dependencies", record("init_deps"))
    monkeypatch.setattr(lifespan_module, "_ensure_system_cosir_dir", record("ensure_cosir"))

    class _RunService:
        def recover_orphaned_runs(self) -> list[Any]:
            recorder.calls.append("recover_runs")
            return []

        def list_latest_runs(self) -> list[Any]:
            recorder.calls.append("list_latest_runs")
            return []

    class _DelegationService:
        def mark_interrupted_delegations_failed(self, reason: str) -> None:
            recorder.calls.append("mark_delegations")

    class _TerminalService:
        def initialize(self) -> None:
            recorder.calls.append("terminal_init")

        def shutdown(self) -> None:
            recorder.calls.append("terminal_shutdown")

    class _Executor:
        async def close(self) -> None:
            recorder.calls.append("executor_close")

    monkeypatch.setattr(lifespan_module, "get_conversation_run_service", record("get_run_service", _RunService()))
    monkeypatch.setattr(
        lifespan_module, "get_delegation_service", record("get_delegation_service", _DelegationService())
    )
    monkeypatch.setattr(
        lifespan_module,
        "get_terminal_session_service",
        record("get_terminal_service", _TerminalService()),
    )
    monkeypatch.setattr(lifespan_module, "get_conversation_run_executor", record("get_executor", _Executor()))
    monkeypatch.setattr(lifespan_module, "flush_langfuse", record("flush_langfuse"))
    monkeypatch.setattr(lifespan_module, "close_service_dependencies", record("close_deps"))
    monkeypatch.setattr(lifespan_module, "ToolSystem", _ToolSystemStub(recorder))
    monkeypatch.setattr(lifespan_module, "build_agent_registry", record("build_agent_registry", "registry"))
    monkeypatch.setattr(lifespan_module, "set_tool_system", record("set_tool_system"))
    monkeypatch.setattr(lifespan_module, "set_agent_registry", record("set_agent_registry"))
    monkeypatch.setattr(lifespan_module, "set_runtime", record("set_runtime"))
    monkeypatch.setattr(lifespan_module, "AgentRuntime", record("agent_runtime", "runtime"))
    monkeypatch.setattr(
        lifespan_module, "ToolRuntimeOutputChannelFactory", record("output_channel_factory")
    )
    monkeypatch.setattr(lifespan_module.HookInterceptor, "safe_fire", _safe_fire_stub(recorder))

    hook_registry_module = importlib.import_module("app.hook.hook_registry")
    monkeypatch.setattr(
        hook_registry_module, "initialize_hook_registry", record("init_hook_registry")
    )

    return recorder


def _shutdown_logging_stub(recorder: _Recorder) -> Any:
    """构造计数用的 ``shutdown_logging`` stub。"""

    def _stub(*args: Any, **kwargs: Any) -> None:
        recorder.calls.append("shutdown_logging")
        recorder.shutdown_logging_calls += 1

    return _stub


def _safe_fire_stub(recorder: _Recorder) -> Any:
    """构造记录 Hook 事件的 ``HookInterceptor.safe_fire`` stub。"""

    def _stub(context: Any, *args: Any, **kwargs: Any) -> None:
        event = getattr(getattr(context, "event", None), "value", None) or str(context)
        recorder.calls.append(f"safe_fire:{event}")

    return _stub


class _ToolSystemStub:
    """``ToolSystem.build_tool_system`` 的最小替身。"""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    @classmethod
    def build_tool_system(cls) -> str:
        return "tool-system"


def _run_lifespan_once() -> None:
    """驱动一次完整 ``lifespan``：进入上下文后立即退出，触发关闭分支。"""

    async def _drive() -> None:
        async with lifespan_module.lifespan(app_module.app):
            pass

    asyncio.run(_drive())


def test_lifespan_happy_path_marks_ready_then_stopped(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正常路径：启动步骤按序执行，就绪写 ready、关闭写 stopped。

    ``shutdown_logging`` 由 ``_lifespan_impl`` 的内层 finally 与 ``lifespan`` 包装层的
    finally 各调用一次，合计 2 次（拆分后的既有语义，非缺陷）。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)

    _run_lifespan_once()

    calls = stubbed_lifespan.calls
    assert calls.index("install_logging") < calls.index("settings_load")
    assert calls.index("settings_load") < calls.index("init_deps")
    assert "init_hook_registry" in calls
    assert calls.count("install_logging") == 2, "启动期应重建一次日志管线"

    assert _read_state(boot_file)["phase"] == "stopped"
    assert stubbed_lifespan.shutdown_logging_calls == 2


def test_lifespan_logs_recovered_runs_path(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """存在遗留 active run 时必须走「收敛并记 info」分支，不得因日志字段缺项崩溃。"""

    from types import SimpleNamespace

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)

    class _RunServiceWithRecovered:
        def recover_orphaned_runs(self) -> list[Any]:
            stubbed_lifespan.calls.append("recover_runs")
            return [SimpleNamespace(id=101), SimpleNamespace(id=202)]

        def list_latest_runs(self) -> list[Any]:
            stubbed_lifespan.calls.append("list_latest_runs")
            return []

    monkeypatch.setattr(
        lifespan_module, "get_conversation_run_service", lambda: _RunServiceWithRecovered()
    )

    _run_lifespan_once()

    assert "recover_runs" in stubbed_lifespan.calls
    assert _read_state(boot_file)["phase"] == "stopped"


def test_lifespan_fires_session_start_and_end(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SESSION_START 必须在启动步骤后、关闭编排前触发；SESSION_END 在关闭块最先触发。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)

    _run_lifespan_once()

    calls = stubbed_lifespan.calls
    start_events = [c for c in calls if c.startswith("safe_fire:")]
    assert len(start_events) == 2, start_events
    # SESSION_START 晚于依赖初始化与 Hook 注册表播种。
    assert calls.index("init_hook_registry") < calls.index(start_events[0])
    # SESSION_END 是关闭块的第一步，早于 executor / terminal / 依赖关闭。
    assert calls.index(start_events[1]) < calls.index("executor_close")
    assert calls.index(start_events[1]) < calls.index("close_deps")


def test_lifespan_startup_failure_reraises_and_marks_failed(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """yield 前抛错：异常必须原样冒泡、bootstate 标 failed、shutdown_logging 仍被调用。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)
    stubbed_lifespan.fail_step = "init_deps"

    with pytest.raises(RuntimeError, match="stub failure at init_deps"):
        _run_lifespan_once()

    state = _read_state(boot_file)
    assert state["phase"] == "failed"
    assert state["step"] == "lifespan"
    assert state["error_type"] == "RuntimeError"
    assert state["error_message"] == "stub failure at init_deps"
    assert stubbed_lifespan.shutdown_logging_calls == 1


def test_lifespan_startup_failure_before_yield_does_not_stop(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动失败的路径不得触发关闭编排（SESSION_END / executor close / close_deps）。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)
    stubbed_lifespan.fail_step = "ensure_cosir"

    with pytest.raises(RuntimeError, match="stub failure at ensure_cosir"):
        _run_lifespan_once()

    assert "executor_close" not in stubbed_lifespan.calls
    assert "close_deps" not in stubbed_lifespan.calls


def test_lifespan_startup_failure_does_not_overwrite_ready(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """若 bootstate 已是 ready（例如重复启动），yield 前失败也不得把终态改回 failed。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "ready")
    paths.override(DATA_DIR=tmp_path)
    stubbed_lifespan.fail_step = "init_deps"

    with pytest.raises(RuntimeError):
        _run_lifespan_once()

    assert _read_state(boot_file) == {"phase": "ready"}


def test_lifespan_body_exception_still_runs_shutdown_and_marks_stopped(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """yield 之后（应用运行期）异常：关闭编排仍执行、bootstate 收敛为 stopped 后异常冒泡。

    注意：``_mark_boot_failed`` 只在 ``lifespan`` 包装层捕获，应用运行期异常会先走
    内部 finally 的关闭块（写 stopped），随后被包装层标 failed——这里验证关闭块未被跳过。
    """

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)

    async def _drive() -> None:
        async with lifespan_module.lifespan(app_module.app):
            raise RuntimeError("application body failure")

    with pytest.raises(RuntimeError, match="application body failure"):
        asyncio.run(_drive())

    calls = stubbed_lifespan.calls
    assert "executor_close" in calls
    assert "close_deps" in calls
    assert stubbed_lifespan.shutdown_logging_calls == 2
    # 关闭块先写 stopped；包装层的 _mark_boot_failed 见到非 booting 阶段会跳过。
    assert _read_state(boot_file)["phase"] == "stopped"


def test_lifespan_shutdown_failure_still_calls_shutdown_logging(
    stubbed_lifespan: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """关闭期异常：内层与包装层 finally 都必须执行 ``shutdown_logging``（资源不泄漏）。"""

    boot_file = _boot_state_file(monkeypatch, tmp_path)
    _write_phase(boot_file, "booting")
    paths.override(DATA_DIR=tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("close deps failed")

    monkeypatch.setattr(lifespan_module, "close_service_dependencies", _boom)

    async def _drive() -> None:
        async with lifespan_module.lifespan(app_module.app):
            pass

    with pytest.raises(OSError, match="close deps failed"):
        asyncio.run(_drive())

    assert stubbed_lifespan.shutdown_logging_calls == 2
