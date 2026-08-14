"""运行时事件 TypeScript 协议生成契约测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.enums.event_type import EventType


def _load_generator_module():
    """加载仓库脚本目录中的 runtime event TS 生成模块。

    参数:
        无。

    返回:
        已加载的 ``generate_runtime_event_ts`` 模块对象。

    异常:
        AssertionError: 生成脚本无法定位或加载时抛出。

    副作用:
        通过 importlib 执行生成脚本模块顶层导入，但不写入文件。
    """

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "generate_runtime_event_ts.py"
    spec = importlib.util.spec_from_file_location("generate_runtime_event_ts", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_failed_payload_data_is_generated_for_frontend_contract() -> None:
    """RunFailedPayload.data 应进入共享 TS 类型，支撑回放按 error_kind 分析。"""
    generator = _load_generator_module()

    rendered = generator.render_typescript(list(EventType))

    assert "export interface RunFailedPayload" in rendered
    assert "data?: Record<string, unknown> | null;" in rendered
