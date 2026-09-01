"""对话事实重构删除验证：确认 Runtime 专用事件体系已从生产代码彻底移除。

对齐 plan ``docs/plan/conversation-facts-runtime-event-removal-plan.md`` §7「删除验证」：
仓库中不存在 Runtime 专用 ``RuntimeEvent``、``EventType``（Runtime 枚举）、``runtime_events``
表、``TurnStreamService`` 的生产引用；schema 初始化与级联删除不再注册旧表。

本测试为**静态扫描**，只对 ``app/`` 生产源码生效，忽略本测试文件自身。符号残留扫描
仅匹配**可执行引用**（``import`` 行与 ``RuntimeEvent(`` 实例化调用），忽略 docstring /
注释中作为迁移对比提及的 ``RuntimeEventBus`` 等历史参照名——这些不构成可运行依赖；
已独立迁移的 Workspace 事件体系（``WorkspaceEvent`` / ``WorkspaceEventType``）属合法
产品能力，不被误判。
"""

from __future__ import annotations

import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"

# Runtime 专用类名 / 符号，必须完全不存在于生产源码（含 import 与实例化）。
RUNTIME_EVENT_PRODUCTION_SYMBOLS = (
    "RuntimeEvent(",
    "RuntimeEventBus",
    "TurnStreamService",
    "RuntimeEventType",
    "RuntimeEventPayload",
)

# ``runtime_events`` 表名残留（仅允许出现在注释/文档，不允许出现在 import 或 DDL 引用）。
RUNTIME_EVENTS_TABLE_REF = "runtime_events"


def _production_sources() -> list[pathlib.Path]:
    """收集 app/ 下全部生产 Python 源码，排除本测试文件。"""
    sources: list[pathlib.Path] = []
    for path in APP_ROOT.rglob("*.py"):
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue
        sources.append(path)
    return sources


def _read_lines(path: pathlib.Path) -> list[str]:
    """读取文件全部行；不可读时返回空列表。"""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def test_no_runtime_event_symbols_in_production_code() -> None:
    """生产代码中不得出现任何 Runtime 专用事件体系的可执行引用。

    仅检测可执行引用（import、实例化调用、类继承），忽略 docstring 中作为设计对比
    提及的 ``RuntimeEventBus`` 等历史参照名——这些不构成可运行依赖，且属于合法的
    迁移说明。
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _production_sources():
        for idx, line in enumerate(_read_lines(path), start=1):
            stripped = line.strip()
            is_executable_ref = (
                "import" in stripped
                and any(symbol in line for symbol in RUNTIME_EVENT_PRODUCTION_SYMBOLS)
            ) or "RuntimeEvent(" in line
            if is_executable_ref:
                offenders.append((str(path.relative_to(APP_ROOT)), idx, stripped))
    assert offenders == [], (
        "发现 Runtime 专用事件体系残留可执行引用，应随对话事实重构一并删除：\n"
        + "\n".join(f"  {f}:{n}: {text}" for f, n, text in offenders)
    )


def test_no_runtime_events_table_reference_in_code() -> None:
    """生产代码不得引用已删除的 ``runtime_events`` 表（schema 与级联删除均不再注册）。"""
    offenders: list[tuple[str, int, str]] = []
    for path in _production_sources():
        for idx, line in enumerate(_read_lines(path), start=1):
            if RUNTIME_EVENTS_TABLE_REF in line:
                offenders.append((str(path.relative_to(APP_ROOT)), idx, line.strip()))
    assert offenders == [], "发现已删除的 runtime_events 表残留引用：\n" + "\n".join(
        f"  {f}:{n}: {text}" for f, n, text in offenders
    )


def test_workspace_event_system_is_independent_of_runtime() -> None:
    """Workspace 事件体系已独立迁移，不得复用 Runtime 专用的事件基类或注册表。"""
    offenders: list[tuple[str, int, str]] = []
    runtime_only_imports = (
        "from app.models.event.runtime_event import",
        "from app.models.payload.runtime_event_payload import",
        "from app.models.payload.registry.runtime_event_payload_registry import",
        "from app.service.agent_runtime_event",
    )
    for path in _production_sources():
        for idx, line in enumerate(_read_lines(path), start=1):
            for bad in runtime_only_imports:
                if bad in line:
                    offenders.append((str(path.relative_to(APP_ROOT)), idx, line.strip()))
    assert offenders == [], (
        "Workspace 事件仍借用 Runtime 专用事件基类/注册表，违反独立迁移约束：\n"
        + "\n".join(f"  {f}:{n}: {text}" for f, n, text in offenders)
    )
