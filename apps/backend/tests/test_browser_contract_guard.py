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

from app.tools.browser_runtime.deadlines import HOST_DEADLINES
from app.tools.display.browser_display import build_browser_page_display_data
from app.tools.host.errors import CANCELLED, INVALID_PARAMS, UNAVAILABLE, HostCallError
from app.tools.tool_handler.browser.errors import BROWSER_ERRORS, browser_error_observation
from app.tools.tool_handler.browser.rendering import (
    act_observation,
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

    assert content.splitlines()[:6] == [
        "tab: t3",
        "url: http://127.0.0.1:5173/login (HTTP 404)",
        "title: Sign in",
        "viewport: 390×844 mobile, dark",
        "other tabs: t1",
        "frames not shown: 1 (their contents are not in this snapshot; browser_screenshot "
        "shows what they display)",
    ]
    assert (
        "new logs since this tab's last page result: 2 console errors, 1 console warning, "
        "1 failed request (see browser_logs)"
    ) in content
    assert f"- {page['notes'][0]}" in content
    assert f"<page_snapshot>\n{page['snapshot']}\n</page_snapshot>" in content


def test_act_observation_failure_sample_renders_the_header_and_the_reason(
    samples: dict[str, Any],
) -> None:
    outcome = samples["act_observation_failed"]
    observation = act_observation("browser_act", outcome, [{"type": "click"}])
    content = observation.content

    assert content.splitlines()[:2] == ["actions: 1 of 1 done", "tab: t3"]
    assert "page observation unavailable: DIALOG_OPEN:" in content
    assert "Answer it first with a browser_act dialog action" in content
    assert content.rstrip().endswith("Read a fresh snapshot before continuing.")


def test_act_observation_failure_for_a_closed_tab_explains_the_stale_header(
    samples: dict[str, Any],
) -> None:
    outcome = {**samples["act_observation_failed"]}
    outcome["observation_error"] = {
        "code": "TAB_NOT_FOUND",
        "message": "tab t3 is not open in this session",
    }
    content = act_observation("browser_act", outcome, [{"type": "click"}]).content
    assert (
        "the tab was closed; the header above is the page before the actions." in content
    )
    assert "Pass one of this session's tab ids" in content


def test_act_sample_renders_the_failure_and_the_dialog(samples: dict[str, Any]) -> None:
    act = samples["act"]
    content = page_text(act["observation"], lead=act_summary(act))

    lines = content.splitlines()
    # 停下的原因与失败那一步的错误相同：错误只写在那一步上，stopped 只给建议。
    assert lines[:5] == [
        "actions: 1 of 3 done",
        "- 1 done",
        "- 2 failed (DIALOG_OPEN: the page is paused: it is showing a confirm dialog (#7))",
        "- 3 skipped",
        "stopped: " + BROWSER_ERRORS["DIALOG_OPEN"][0],
    ]
    assert content.count("the page is paused") == 1
    # 页面结果总是写视口（默认值也写），没有新日志时不写计数行。
    assert "viewport: 1280×800 (follows the browser panel)" in lines
    assert "new logs" not in content
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
    assert "older: more matching entries of this page not shown" in content
    assert content.splitlines()[-1] == "next: since=16"
    # 日志结果用不上视口。
    assert "viewport" not in content
    assert "dropped" not in content


def test_network_logs_sample_renders_outcomes_drops_and_more_after(
    samples: dict[str, Any],
) -> None:
    content = logs_text(samples["network_logs"])

    fenced = content.split("<network_requests>\n")[1].split("\n</network_requests>")[0]
    assert fenced.splitlines() == [
        "#20 GET http://127.0.0.1:5173/api/me → 401 (Fetch, 31 ms) · body: application/json, 27 B",
        "#21 POST http://127.0.0.1:5173/api/login → failed: net::ERR_CONNECTION_REFUSED (XHR)",
        "#22 GET http://127.0.0.1:5173/api/stream → no response yet (Fetch) · body: capturing",
    ]
    assert "dropped: entries before #18 (over capacity, cannot be read)" in content
    assert "next: since=23 (more matching entries follow)" in content
    assert "read without since later for their outcome." in content
    assert "older:" not in content


def test_response_body_samples_render_the_body_or_why_there_is_none(
    samples: dict[str, Any],
) -> None:
    content = logs_text(samples["response_body"])
    assert "request: #20 GET http://127.0.0.1:5173/api/me → 401 (Fetch, 31 ms)" in content
    assert "response body (application/json, 27 B):" in content
    assert '<response_body>\n{"error":"not signed in"}\n</response_body>' in content
    assert "next" not in content.split("response body")[0].split("request:")[1]

    truncated = logs_text(samples["response_body_truncated"])
    assert "response body (application/json, 293.0 KB; only the first 9 B were kept):" in truncated

    unavailable = logs_text(samples["response_body_unavailable"])
    assert (
        "no body: the page was paused by a dialog; the body is captured after the dialog is "
        "answered" in unavailable
    )
    assert "<response_body>" not in unavailable


def test_evaluate_samples_render_each_outcome(samples: dict[str, Any]) -> None:
    value = evaluate_text(samples["evaluate"])
    assert '<script_result>\n{"user":null,"items":3}\n</script_result>' in value

    error = evaluate_text(samples["evaluate_error"])
    assert "<script_error>\nReferenceError: foo is not defined\n</script_error>" in error
    assert "<script_result>" not in error

    assert "value: undefined" in evaluate_text(samples["evaluate_undefined"])
    assert "cut off at the size limit" in evaluate_text(samples["evaluate_truncated"])

    dialog = evaluate_text(samples["evaluate_dialog"])
    assert "the script opened a page dialog" in dialog
    assert "do not run it again" in dialog
    assert "browser_act dialog action with id 4" in dialog


def test_screenshot_and_tabs_samples_render(samples: dict[str, Any]) -> None:
    shot = screenshot_text(samples["screenshot"])
    assert shot.splitlines() == [
        "tab: t3",
        "url: http://127.0.0.1:5173/login (HTTP 200)",
        "title: Sign in",
        "viewport: 1280×800 (follows the browser panel)",
        "screenshot: the viewport (the image follows)",
    ]
    # 整页截图从页面顶端算起，像素不是视口坐标；没截断时不提截断，免得模型以为下面还有内容。
    whole = screenshot_text(samples["screenshot"], full_page=True)
    assert "pixels are not viewport coordinates" in whole
    assert "covers only the top" not in whole
    # 真截断时说明页面多高、图里有多少、怎么看剩下的。
    cut = screenshot_text(samples["screenshot_full_page_cut"], full_page=True)
    assert "the page is 9812px tall and this image covers only the top 4000px" in cut
    assert "scroll down and take viewport screenshots" in cut
    assert build_browser_page_display_data(samples["screenshot"])["tab_id"] == "t3"
    assert screenshot_bytes(samples["screenshot"]).startswith(b"\xff\xd8")

    assert tabs_text(samples["tabs"]).splitlines() == [
        "t1 (opened by the user) · Home · http://127.0.0.1:5173/",
        "t3 (current) · Sign in · http://127.0.0.1:5173/login",
    ]
    # select / close 先写结论，再列操作之后的标签；关掉最后一个时说明已经没有标签。
    assert tabs_text(samples["tabs"], "select", "t3").splitlines()[0] == "selected: t3"
    assert tabs_text([], "close", "t3") == "closed: t3\nthis session has no open tabs"
    # 未收敛的修改操作标在标签上，模型先看列表就能避开 TAB_BUSY。
    busy = [{**samples["tabs"][1], "operation": "running"}]
    assert tabs_text(busy) == (
        "t3 (current, an operation is still running) · Sign in · http://127.0.0.1:5173/login"
    )
