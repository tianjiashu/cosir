"""浏览器工具与宿主之间的跨语言契约守卫。

四份契约各有唯一来源，这里核对另一侧没有漂移：
- 请求参数：后端参数模型 → 导出的 ``tool-args.schema.json``（宿主按它校验）；
- 错误码：宿主 ``errors.ts`` 的 ``BrowserErrorCode`` ↔ 后端建议表；
- 方法时限：宿主 ``method-deadlines.json`` → 后端镜像 ``HOST_DEADLINES``（据此定等待时限）；
- 结果形状：宿主维护的 ``wire-samples.json``（宿主单测断言 ``wire.ts`` 的输出与它逐字相同）
  → 后端读取结果的纯函数（渲染正文、截图解码）。主要渲染分支各有一份样例，这些函数读到的字段
  改名或缺失在这里以 ``KeyError`` 暴露。
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.tools.display.browser_display import build_browser_page_display_data
from app.tools.host.errors import CANCELLED, INVALID_PARAMS, UNAVAILABLE, HostCallError
from app.tools.tool_handler.browser.errors import BROWSER_ERRORS, browser_error_observation
from app.tools.tool_handler.browser.host_request import HOST_DEADLINES
from app.tools.tool_handler.browser.rendering import (
    act_summary,
    evaluate_text,
    logs_text,
    page_text,
    screenshot_text,
    tabs_text,
)
from app.tools.tool_handler.browser.screenshot import screenshot_bytes

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_HOST_BROWSER = _BACKEND_ROOT.parent / "desktop" / "electron" / "main" / "browser"
_EXPORT_SCRIPT = _BACKEND_ROOT / "scripts" / "export_browser_tool_schema.py"
_LINK_CODES = {INVALID_PARAMS, CANCELLED, UNAVAILABLE}


def _export_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_browser_tool_schema", _EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_codes() -> list[str]:
    source = (_HOST_BROWSER / "errors.ts").read_text(encoding="utf-8")
    block = re.search(r"export type BrowserErrorCode =(.*?);", source, flags=re.S)
    assert block is not None, "errors.ts 里找不到 BrowserErrorCode"
    return re.findall(r'"([^"]+)"', block.group(1))


@pytest.fixture(scope="module")
def samples() -> dict[str, Any]:
    path = _HOST_BROWSER / "contract" / "wire-samples.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_exported_schema_matches_the_arg_models() -> None:
    script = _export_script()
    committed = script.SCHEMA_PATH.read_text(encoding="utf-8")
    assert committed == script.render_schema(), (
        "tool-args.schema.json 与参数模型不一致：在 apps/backend 下运行 "
        "uv run python scripts/export_browser_tool_schema.py"
    )


def test_host_deadlines_mirror_the_host_contract() -> None:
    path = _HOST_BROWSER / "contract" / "method-deadlines.json"
    host = json.loads(path.read_text(encoding="utf-8"))
    assert host == HOST_DEADLINES, "HOST_DEADLINES 与宿主 method-deadlines.json 不一致：照宿主的改"


def test_every_registered_error_code_has_backend_wording() -> None:
    registered = _registered_codes()
    assert registered and len(registered) == len(set(registered))
    assert set(registered) >= _LINK_CODES
    missing = [code for code in registered if code not in _LINK_CODES | set(BROWSER_ERRORS)]
    assert not missing, f"这些错误码在后端没有建议文案：{missing}"
    stale = set(BROWSER_ERRORS) - set(registered)
    assert not stale, f"后端建议表里有宿主登记表没有的码：{sorted(stale)}"
    assert not set(BROWSER_ERRORS) & _LINK_CODES, "链路层同名码交给 host_error_observation 解释"


@pytest.mark.parametrize(
    ("code", "status"),
    [(INVALID_PARAMS, "error"), (UNAVAILABLE, "error"), (CANCELLED, "cancelled")],
)
def test_link_level_codes_fall_through_to_the_link_layer(code: str, status: str) -> None:
    observation = browser_error_observation("browser_navigate", HostCallError(code, "detail"))
    assert observation.status == status


def test_page_sample_renders_header_logs_notes_and_fenced_snapshot(samples: dict[str, Any]) -> None:
    page = samples["page"]
    content = page_text(page)

    first, emulation = content.splitlines()[:2]
    assert first == "tab t3 · Sign in · http://127.0.0.1:5173/login · HTTP 404"
    assert emulation == "viewport 390×844 mobile, dark"
    assert "other tabs in this task: t1" in content
    assert (
        "new on this page since this tab's last page result: 2 console errors, 1 console warning, "
        "1 failed request (browser_logs shows them; narrow down with level or failed_only)"
    ) in content
    assert f"- {page['notes'][0]}" in content
    assert f"<page_snapshot>\n{page['snapshot']}\n</page_snapshot>" in content


def test_act_sample_renders_the_failure_and_the_dialog(samples: dict[str, Any]) -> None:
    act = samples["act"]
    content = page_text(act, lead=act_summary(act, 3))

    lines = content.splitlines()
    assert lines[0].startswith("completed 1 of 3 action(s); action 2 failed (DIALOG_OPEN)")
    assert lines[1] == "tab t3 · Sign in · http://127.0.0.1:5173/login · HTTP 200"
    # 视口总是写（默认值也写），没有新日志时不写计数行。
    assert lines[2] == "viewport 1280×800 (follows the browser panel)"
    assert "new on this page" not in content
    assert "showing dialog 7 (confirm)" in content
    assert "<dialog_message>\nmessage: Delete it?\n</dialog_message>" in content
    assert "<page_snapshot>" not in content


def test_prompt_dialog_sample_renders_the_default_answer(samples: dict[str, Any]) -> None:
    content = page_text(samples["page_prompt"])
    assert "showing dialog 9 (prompt)" in content
    assert 'default answer (used when accepting without text): "Ada"' in content


def test_console_logs_sample_renders_fenced_entries_and_older_entries(
    samples: dict[str, Any],
) -> None:
    content = logs_text(samples["console_logs"])

    fenced = content.split("<console_messages>\n")[1].split("\n</console_messages>")[0]
    assert fenced.splitlines() == [
        "#12 [error] Uncaught TypeError: x is undefined (at http://127.0.0.1:5173/src/main.ts:42)",
        "#15 [warning] deprecated API (in frame http://localhost:6006/embed)",
    ]
    assert "older matching entries of this page were not returned" in content
    assert "to read only newer entries later, pass since=16." in content
    assert "dropped" not in content


def test_network_logs_sample_renders_outcomes_drops_and_more_after(
    samples: dict[str, Any],
) -> None:
    content = logs_text(samples["network_logs"])

    fenced = content.split("<network_requests>\n")[1].split("\n</network_requests>")[0]
    assert fenced.splitlines() == [
        "#20 GET http://127.0.0.1:5173/api/me → 401 (Fetch, 31 ms)",
        "#21 POST http://127.0.0.1:5173/api/login → failed: net::ERR_CONNECTION_REFUSED (XHR)",
        "#22 GET http://127.0.0.1:5173/api/stream → no response yet (Fetch)",
    ]
    assert "entries before #18 were dropped" in content
    assert "more matching entries follow; pass since=23 to read on." in content
    assert "older matching entries" not in content


def test_evaluate_samples_render_each_outcome(samples: dict[str, Any]) -> None:
    value = evaluate_text(samples["evaluate"])
    assert '<script_result>\n{"user":null,"items":3}\n</script_result>' in value

    error = evaluate_text(samples["evaluate_error"])
    assert "<script_error>\nReferenceError: foo is not defined\n</script_error>" in error
    assert "<script_result>" not in error

    assert "the expression returned undefined." in evaluate_text(samples["evaluate_undefined"])
    assert "cut off at the size limit" in evaluate_text(samples["evaluate_truncated"])

    dialog = evaluate_text(samples["evaluate_dialog"])
    assert "the script opened a page dialog" in dialog
    assert "do not run it again" in dialog
    assert "browser_act dialog action with id 4" in dialog


def test_screenshot_and_tabs_samples_render(samples: dict[str, Any]) -> None:
    shot = screenshot_text(samples["screenshot"])
    assert shot.splitlines()[:2] == [
        "tab t3 · Sign in · http://127.0.0.1:5173/login · HTTP 200",
        "viewport 1280×800 (follows the browser panel)",
    ]
    assert build_browser_page_display_data(samples["screenshot"])["tab_id"] == "t3"
    assert screenshot_bytes(samples["screenshot"]).startswith(b"\xff\xd8")

    assert tabs_text(samples["tabs"]).splitlines() == [
        "t1 (opened by the user) · Home · http://127.0.0.1:5173/",
        "t3 (current) · Sign in · http://127.0.0.1:5173/login",
    ]
