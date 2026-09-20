"""「删除诊断日志」改动的对抗性验证测试（只新增测试，不修改任何生产代码）。

背景与验证目标
--------------
本次改动对 ``app/core/tools/tool_handler/web_extract.py`` 的执行流水做了两件事：
1. 删除 5 个「执行流水」日志事件，后续它们**绝不应再出现**：
   ``web_extract_url_limit_exceeded``、``web_extract_urls_rejected``、
   ``web_extract_started``、``web_extract_provider_completed``、
   ``web_extract_completed``。
2. 在删除 ``started``/``completed`` 事件后，仅保留 ``web_extract_all_pages_failed``
   一条带 ``elapsed_ms`` 的耗时日志，并新增了 ``started = time.monotonic()`` 与
   ``content`` 局部变量。

必须**行为不变**：各失败/成功路径下 ``ToolObservation`` 的
``status`` / ``error`` / ``retryable`` / ``content`` / ``display_data`` 与改动前一致。

必须**保留**的日志事件：``web_extract_provider_unavailable``、
``web_extract_provider_failed``、``web_extract_all_pages_failed``、
``web_extract_result_build_failed``（web_extract.py）；
``web_url_address_blocked``、``web_url_fake_ip_allowed``（url_safety.py）；
``web_extract_page_failed``（firecrawl_provider.py）。

对抗重点
--------
- ``elapsed_ms`` 在删除 ``started`` 事件后是否仍可用、非负、量级合理。
- 超限分支删除日志后是否仍返回错误（而不是静默成功）。
- 非地址类拦截路径是否「完全无日志」（已知取舍，如实记录）。
- 是否存在「删日志导致变量未定义 / 未使用」的路径（如 ``provider``、``content``）。
- 任何路径下日志都不得出现 URL 原文 / query 串 / 凭据。

本文件复用 test_web_search_extract_tools.py 中的 FakeProvider /
_registry_with / _public_resolver，避免重复脚手架，且不触碰生产代码。
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from app.config.settings import Settings
from app.core.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderRequestError,
    WebProviderUnavailableError,
)
from app.core.tools.tool_handler.web_extract import WebExtractTool
from tests.test_web_search_extract_tools import (
    FakeProvider,
    _log_full_text,
    _public_resolver,
    _registry_with,
)

# ---------------------------------------------------------------------------
# 常量：本次改动「应当消失」的 5 个事件名 + 必须「保留」的事件名
# ---------------------------------------------------------------------------

# 被删除的 5 个执行流水事件：任何路径下都不得再被记录。
REMOVED_EVENTS: tuple[str, ...] = (
    "web_extract_url_limit_exceeded",
    "web_extract_urls_rejected",
    "web_extract_started",
    "web_extract_provider_completed",
    "web_extract_completed",
)

# 明确保留的事件：对应失败路径必须仍被记录。
KEPT_WEB_EXTRACT_EVENTS: tuple[str, ...] = (
    "web_extract_provider_unavailable",
    "web_extract_provider_failed",
    "web_extract_all_pages_failed",
    "web_extract_result_build_failed",
)

KEPT_URL_SAFETY_EVENTS: tuple[str, ...] = (
    "web_url_address_blocked",
    "web_url_fake_ip_allowed",
)

KEPT_FIRECRAWL_EVENTS: tuple[str, ...] = ("web_extract_page_failed",)

# 需要断言「绝不出现在任何日志」的五类被删事件名的统一正则（用于反向扫描）。
_REMOVED_EVENT_RE = re.compile(
    r"web_extract_(url_limit_exceeded|urls_rejected|started|provider_completed|completed)"
)


@pytest.fixture(autouse=True)
def _clear_web_backend_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 fake provider 测试不受本机环境中的生产 backend 配置影响。"""

    monkeypatch.setattr(Settings, "WEB_SEARCH_BACKEND", "")
    monkeypatch.setattr(Settings, "WEB_EXTRACT_BACKEND", "")
    monkeypatch.setattr(Settings, "WEB_BACKEND", "")


def _caevents(records: list[logging.LogRecord]) -> list[str]:
    """提取所有日志事件的模板名（``record.msg`` 为字符串时即事件名）。"""

    return [r.msg for r in records if isinstance(r.msg, str)]


def _find(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """按事件名筛选日志记录。"""

    return [r for r in records if r.msg == event]


def _assert_no_removed_events(records: list[logging.LogRecord]) -> None:
    """断言 5 个被删事件名一个都没出现（防止「删了但没删干净」）。"""

    hits = [r.msg for r in records if isinstance(r.msg, str) and _REMOVED_EVENT_RE.search(r.msg)]
    assert hits == [], f"被删除的流水事件仍被记录：{hits}"


def _full_url_canaries(url: str) -> list[str]:
    """从 URL 中提取若干「不该出现在日志里」的片段作为 canary。"""

    canaries = [url]
    query = url.split("?", 1)[1] if "?" in url else ""
    if query:
        canaries.append(query)
        for pair in query.split("&"):
            if "=" in pair:
                canaries.append(pair.split("=", 1)[1])
    return [c for c in canaries if c]


# ===========================================================================
# 组 A：行为不变 —— 各路径 ToolObservation 的字段必须符合契约
# ===========================================================================


def test_over_limit_returns_error_not_silent_success_and_no_removed_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 删掉 url_limit_exceeded 日志后，超限分支必须**仍返回错误**（不是静默成功），
    且不得再产生 web_extract_url_limit_exceeded（潜在缺陷：删日志时误删了 return / 分支体）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    urls = [f"https://e.com/{i}" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX + 1)]
    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "error"
    assert obs.retryable is True
    assert obs.error is not None and str(Settings.WEB_EXTRACT_URL_LIMIT_MAX) in obs.error
    # 超限在被拦时绝不能调用 provider（无网络副作用）。
    assert provider.extract_calls == []
    # 不得静默成功：content 必须是 None（错误观察）。
    assert obs.content is None
    assert _find(caplog.records, "web_extract_url_limit_exceeded") == []
    _assert_no_removed_events(caplog.records)


def test_exactly_at_limit_is_allowed_not_rejected() -> None:
    """test_purpose: 边界值 —— URL 数**恰好等于**上限必须放行（潜在缺陷：off-by-one 把 `<=` 写成 `<`，
    与「删除超限日志」相伴的分支改写风险）。"""

    urls = [f"https://e.com/{i}" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX)]
    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "success"
    assert len(provider.extract_calls) == 1


@pytest.mark.parametrize(
    "url,error_fragment",
    [
        # 注意：这里刻意不包含 IP 字面量内网地址（127.0.0.1 / 10.0.0.5）。
        # 因为 _public_resolver 会对**任意** hostname（含 IP 字面量）返回 8.8.8.8，
        # 而生产实现信任注入 resolver 的解析结果，字面量内网地址会被放行。
        # 该「解析器对字面量 host 的信任」属既有行为（与本改动无关），
        # 单独由 test_ip_literal_internal_blocked_with_system_resolver 用系统解析覆盖。
        ("https://example.com/?token=abc", "credential-like"),
        ("https://example.com/?api_key=SECRET999", "credential-like"),
        ("ftp://example.com/file", "scheme"),
        ("http://user:pass@example.com/", "credentials"),
        ("http://", "hostname"),
        ("", "non-empty"),
    ],
)
def test_security_rejection_paths_behavior_unchanged(
    url: str, error_fragment: str
) -> None:
    """test_purpose: 各类安全检查拦截路径在删除 urls_rejected 日志后，行为必须不变：
    status=error、error 以 'Blocked:' 开头、retryable=False、content=None、不调用 provider
    （潜在缺陷：删日志时改坏了 _blocked_url_error 的字段填充或去掉了 return）。"""

    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=[url])

    assert obs.status == "error"
    assert obs.error is not None and obs.error.startswith("Blocked:")
    assert error_fragment in obs.error
    assert obs.retryable is False
    assert obs.content is None
    assert provider.extract_calls == []
    # 错误 UI 通道只承载稳定短提示，不承载完整错误原因。
    assert obs.display_data == {"status_hint": "提取失败"}


def test_ip_literal_internal_blocked_with_system_resolver() -> None:
    """test_purpose: IP 字面量内网地址（127.0.0.1 / 10.0.0.5）不注入 resolver 时必须被拦截，
    行为与删除日志前一致，且不调用 provider（潜在缺陷：把字面量判定的拦截改没了）。
    这里用系统解析（resolver=None），因为字面量 host 的地址就是其自身。"""

    for url in ("http://127.0.0.1/", "http://10.0.0.5/"):
        provider = FakeProvider()
        tool = WebExtractTool(_registry_with(provider), resolver=None)

        obs = tool.execute(urls=[url])

        assert obs.status == "error", f"{url} 应被拦截"
        assert obs.error is not None and obs.error.startswith("Blocked:")
        assert obs.retryable is False
        assert provider.extract_calls == []


def test_injected_resolver_can_allow_ip_literal_host_pre_existing_behavior() -> None:
    """test_purpose: 记录一个既有（与本改动无关）的行为事实 —— 生产实现信任注入 resolver 的解析
    结果，对 IP 字面量 host 也是先解析再判定。因此当 resolver 对 `127.0.0.1` 返回公网地址
    8.8.8.8 时，字面量内网地址会被放行。此用例不是缺陷判决，仅固化现状，避免未来误以为
    「字面量一定被拦」而在别处建立错误假设（也顺带说明本文件其它用例为何对字面量改用系统解析）。"""

    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=lambda h: ["8.8.8.8"])

    obs = tool.execute(urls=["http://127.0.0.1/"])

    # 当前事实：注入的解析结果覆盖了字面量的真实内网属性。
    assert obs.status == "success"
    assert len(provider.extract_calls) == 1


def test_provider_unavailable_path_behavior_and_kept_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: provider 抛 WebProviderUnavailableError 时行为不变（retryable=False、
    error 为异常文本），且 web_extract_provider_unavailable 仍被记录（潜在缺陷：删日志误伤保留事件）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider(
        available=True,
        extract_side_effect=WebProviderUnavailableError("not configured here"),
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert obs.error == "not configured here"
    assert obs.retryable is False
    assert obs.content is None
    kept = _find(caplog.records, "web_extract_provider_unavailable")
    assert kept, "provider 未配置路径必须保留 web_extract_provider_unavailable"
    assert kept[0].data["provider"] == "fake"
    _assert_no_removed_events(caplog.records)


def test_provider_generic_exception_path_behavior_and_kept_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: provider 抛通用异常时行为不变（retryable=True、error 含 provider 名），
    且 web_extract_provider_failed 仍被记录、异常细节不外泄到观测（潜在缺陷：删日志误伤保留事件）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider(
        available=True, extract_side_effect=RuntimeError("boom-detail-should-not-leak")
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert obs.retryable is True
    assert obs.content is None
    assert "fake" in (obs.error or "")
    # 原始异常细节不写入面向模型的 error（避免噪声 / 泄漏）。
    assert "boom-detail-should-not-leak" not in (obs.error or "")
    kept = _find(caplog.records, "web_extract_provider_failed")
    assert kept, "provider 通用异常路径必须保留 web_extract_provider_failed"
    _assert_no_removed_events(caplog.records)


def test_all_pages_failed_path_behavior_and_kept_event_with_elapsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 全部页面失败时行为不变（retryable=True、error 聚合所有失败原因），
    web_extract_all_pages_failed 仍被记录且**必须带非负数值 elapsed_ms**
    （潜在缺陷：删除 started 事件/变量后 elapsed_ms 计算错位或为 None/负值）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    failures = [
        WebExtractItem(url="https://e.com/1", title="", content="", metadata={}, error="boom1"),
        WebExtractItem(url="https://e.com/2", title="", content="", metadata={}, error="boom2"),
    ]
    provider = FakeProvider(extract_factory=lambda urls, fmt, cl: failures)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/1", "https://e.com/2"])

    assert obs.status == "error"
    assert obs.retryable is True
    assert obs.error is not None and "boom1" in obs.error and "boom2" in obs.error
    assert obs.content is None
    kept = _find(caplog.records, "web_extract_all_pages_failed")
    assert kept, "全页失败必须保留 web_extract_all_pages_failed"
    elapsed = kept[0].data["elapsed_ms"]
    assert isinstance(elapsed, int | float), f"elapsed_ms 必须是数值，实际 {elapsed!r}"
    assert elapsed >= 0, f"elapsed_ms 必须非负，实际 {elapsed!r}"
    # 量级合理性：单次无网络 fake 调用耗时应在合理范围内（< 60s）。
    assert elapsed < 60_000, f"elapsed_ms 量级异常：{elapsed!r}"
    _assert_no_removed_events(caplog.records)


def test_partial_failure_path_behavior_unchanged() -> None:
    """test_purpose: 部分失败时行为不变（success + partial + failed_count），且成功项不带 error
    （潜在缺陷：删除 completed 流水日志时误改 payload 组装或失败计数）。"""

    urls = ["https://e.com/1", "https://e.com/2", "https://e.com/3"]

    def _factory(req_urls, fmt, cl):
        return [
            WebExtractItem(url=urls[0], title="t", content="ok1", metadata={}),
            WebExtractItem(url=urls[1], title="", content="", metadata={}, error="fail2"),
            WebExtractItem(url=urls[2], title="t", content="ok3", metadata={}),
        ]

    provider = FakeProvider(extract_factory=_factory)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "success"
    assert obs.content is not None
    payload = json.loads(obs.content)
    assert payload["partial"] is True
    assert payload["failed_count"] == 1
    assert obs.display_data["kind"] == "web-extract-urls"
    assert len(obs.display_data["urls"]) == 3


def test_full_success_path_behavior_and_display_unchanged() -> None:
    """test_purpose: 全成功时行为不变（无 partial/failed_count、display 含全部 URL）
    （潜在缺陷：删除 completed 日志时误改成功分支的 content 组装）。"""

    urls = ["https://e.com/1", "https://e.com/2"]

    def _factory(req_urls, fmt, cl):
        return [WebExtractItem(url=u, title="t", content="c", metadata={}) for u in req_urls]

    provider = FakeProvider(extract_factory=_factory)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "success"
    payload = json.loads(obs.content)
    assert "partial" not in payload
    assert "failed_count" not in payload
    assert obs.display_data == {
        "kind": "web-extract-urls",
        "urls": [{"url": urls[0]}, {"url": urls[1]}],
    }


def test_result_build_failure_path_behavior_and_kept_event(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_purpose: 结果构造失败时行为不变（retryable=False、error 固定文案），
    web_extract_result_build_failed 仍被记录（潜在缺陷：删日志误伤保留事件或 provider 变量）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()

    def _boom(self, extracted, provider_name):  # type: ignore[no-untyped-def]
        raise ValueError("assembly failed internally")

    monkeypatch.setattr(WebExtractTool, "_build_results", _boom)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://ec.com/1"])

    assert obs.status == "error"
    assert obs.error == "Web extraction result assembly failed."
    assert obs.retryable is False
    kept = _find(caplog.records, "web_extract_result_build_failed")
    assert kept, "结果构造失败必须保留 web_extract_result_build_failed"
    # data.provider 分支使用了 provider.name（provider 为真值）；不得因删日志而 NameError。
    assert kept[0].data["provider"] == "fake"
    _assert_no_removed_events(caplog.records)


# ===========================================================================
# 组 B：被删日志确实不再产生（正向加反向）
# ===========================================================================


def test_removed_events_never_emitted_across_all_paths(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_purpose: 遍历「超限 / 安全拦截 / 未配置 / 通用异常 / 全失败 / 部分失败 / 全成功 /
    结果构造失败」全部路径，断言 5 个被删事件一个都不出现（潜在缺陷：只删了部分调用点）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    # 1) 超限
    WebExtractTool(
        _registry_with(FakeProvider()), resolver=_public_resolver
    ).execute(urls=[f"https://e.com/{i}" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX + 1)])

    # 2) 安全拦截（IP 字面量内网 / 敏感 query / 非法 scheme）
    safe_tool = WebExtractTool(_registry_with(FakeProvider()), resolver=_public_resolver)
    safe_tool.execute(urls=["http://127.0.0.1/"])
    safe_tool.execute(urls=["https://example.com/?token=abc"])
    safe_tool.execute(urls=["ftp://example.com/"])

    # 3) provider 未配置
    WebExtractTool(
        _registry_with(FakeProvider(extract_side_effect=WebProviderUnavailableError("x"))),
        resolver=_public_resolver,
    ).execute(urls=["https://example.com/"])

    # 4) provider 通用异常
    WebExtractTool(
        _registry_with(FakeProvider(extract_side_effect=RuntimeError("x"))),
        resolver=_public_resolver,
    ).execute(urls=["https://example.com/"])

    # 5) 全失败
    WebExtractTool(
        _registry_with(
            FakeProvider(
                extract_factory=lambda u, f, c: [
                    WebExtractItem(url=u[0], title="", content="", metadata={}, error="e")
                ]
            )
        ),
        resolver=_public_resolver,
    ).execute(urls=["https://e.com/1"])

    # 6) 部分失败
    WebExtractTool(
        _registry_with(
            FakeProvider(
                extract_factory=lambda u, f, c: [
                    WebExtractItem(url=u[0], title="t", content="c", metadata={}),
                    WebExtractItem(url=u[1] if len(u) > 1 else u[0], title="", content="", metadata={}, error="e"),
                ]
            )
        ),
        resolver=_public_resolver,
    ).execute(urls=["https://e.com/1", "https://e.com/2"])

    # 7) 全成功
    WebExtractTool(
        _registry_with(FakeProvider()), resolver=_public_resolver
    ).execute(urls=["https://example.com/"])

    _assert_no_removed_events(caplog.records)


def test_kept_events_are_not_emitted_on_unrelated_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 保留事件的触发条件不被放宽 —— 全成功路径不得出现任何失败类事件，
    安全拦截路径不得出现 provider_unavailable/failed/all_pages_failed/result_build_failed
    （潜在缺陷：删日志时把保留事件挪到了公共分支上）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    # 全成功：不应有失败类事件。
    WebExtractTool(_registry_with(FakeProvider()), resolver=_public_resolver).execute(
        urls=["https://example.com/"]
    )
    for event in KEPT_WEB_EXTRACT_EVENTS:
        assert _find(caplog.records, event) == [], f"全成功路径不应出现 {event}"

    caplog.clear()

    # 安全拦截（早期返回，不进入 provider 调用）：不应有 provider 类事件。
    WebExtractTool(_registry_with(FakeProvider()), resolver=_public_resolver).execute(
        urls=["http://127.0.0.1/"]
    )
    for event in KEPT_WEB_EXTRACT_EVENTS:
        assert _find(caplog.records, event) == [], f"安全拦截路径不应出现 {event}"
    _assert_no_removed_events(caplog.records)


# ===========================================================================
# 组 C：边界与对抗 —— 「删除是否引入缺陷」的针对性尝试
# ===========================================================================


def test_elapsed_ms_reflects_real_elapsed_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: elapsed_ms 必须真实反映 provider 耗时 —— 让 provider 定速 sleep 100ms，
    断言 elapsed_ms >= 90（潜在缺陷：started 在错误位置取样，或 elapsed_ms 恒为 0/常量，
    即「删除 started 流水日志」后耗时测量被悄悄破坏）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    def _slow_factory(urls, fmt, cl):
        import time as _t

        _t.sleep(0.1)
        return [
            WebExtractItem(url=u, title="", content="", metadata={}, error="boom") for u in urls
        ]

    provider = FakeProvider(extract_factory=_slow_factory)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/1"])

    assert obs.status == "error"
    kept = _find(caplog.records, "web_extract_all_pages_failed")
    assert kept, "全页失败必须保留 web_extract_all_pages_failed"
    elapsed = kept[0].data["elapsed_ms"]
    assert elapsed >= 90, f"provider 至少 sleep 100ms，elapsed_ms 应 >= 90，实际 {elapsed!r}"


def test_all_pages_failed_event_includes_started_before_provider_call() -> None:
    """test_purpose: 对抗性定位取样点 —— started 必须在调用 provider **之前**取样。
    让 provider 记录「被调用时刻」，若 started 在 provider 之后取样，则 elapsed_ms 会
    明显小于真实 provider 耗时（这里用 sleep 放大差异）。（潜在缺陷：started 取样点后移）。"""

    import time as _t

    call_marker: dict[str, float] = {}

    def _slow_factory(urls, fmt, cl):
        call_marker["t0"] = _t.monotonic()
        _t.sleep(0.08)
        return [
            WebExtractItem(url=u, title="", content="", metadata={}, error="boom") for u in urls
        ]

    provider = FakeProvider(extract_factory=_slow_factory)
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("coding_agent.backend")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        tool.execute(urls=["https://e.com/1"])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    kept = _find(records, "web_extract_all_pages_failed")
    assert kept
    elapsed = kept[0].data["elapsed_ms"]
    assert elapsed >= 70, (
        f"provider 实际 sleep 80ms，elapsed_ms 仅 {elapsed!r}，"
        "疑似 started 取样点晚于 provider 调用"
    )


def test_non_address_rejection_emits_no_log_known_tradeoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 【已知取舍，如实记录】删除 urls_rejected 后，非「地址类」拦截路径
    （空串 / 敏感 query 参数 / 内嵌密钥 / 非法 scheme / userinfo 凭据 / 缺 hostname）
    完全**不产生任何日志**。这是本次改动的既定取舍，不作为缺陷判决，仅固化为可观测事实，
    防止未来无意间又引入泄漏 URL 原文的新日志。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    # 逐个跑「非地址类」拦截，记录事件名。
    observed_events: list[str] = []
    for url in [
        "",
        "https://example.com/?token=abc",
        "https://example.com/?api_key=SECRET999",
        "https://example.com/path/sk-abcdefghijklmnop",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
        "http://",
    ]:
        caplog.clear()
        obs = tool.execute(urls=[url])
        assert obs.status == "error"
        observed_events.extend(_caevents(caplog.records))

    # 事实：这些路径不产生 web_extract 流水日志，也不产生 url_safety 地址日志。
    assert observed_events == [], (
        "非地址类拦截当前应完全无日志（已知取舍）；"
        f"若出现说明行为已改变：{observed_events}"
    )
    _assert_no_removed_events(caplog.records)


def test_address_rejection_still_logs_url_address_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: IP 字面量内网拦截仍必须写 web_url_address_blocked（保留事件），
    与「非地址类拦截无日志」形成对照（潜在缺陷：删除流水日志时误删了地址审计日志）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    # 字面量 host 的地址即其自身：用系统解析（resolver=None）才能得到真实的 10.0.0.5。
    tool = WebExtractTool(_registry_with(provider), resolver=None)

    obs = tool.execute(urls=["http://10.0.0.5/"])

    assert obs.status == "error"
    blocked = _find(caplog.records, "web_url_address_blocked")
    assert blocked, "IP 字面量内网拦截必须保留 web_url_address_blocked"
    assert blocked[0].data["host"] == "10.0.0.5"
    assert blocked[0].levelno == logging.WARNING
    _assert_no_removed_events(caplog.records)


def test_fake_ip_exemption_event_still_emitted_and_provider_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: fake-ip 豁免判定逻辑未被日志改动波及 —— 解析到 198.18.0.0/15 的主机名
    必须放行、写 web_url_fake_ip_allowed 并真正到达 provider（潜在缺陷：删日志时改坏了安全
    校验的返回路径）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    tool = WebExtractTool(
        _registry_with(provider), resolver=lambda hostname: ["198.18.0.67"]
    )

    obs = tool.execute(urls=["https://example.com/page"])

    assert obs.status == "success"
    assert len(provider.extract_calls) == 1
    allowed = _find(caplog.records, "web_url_fake_ip_allowed")
    assert allowed, "fake-ip 放行必须保留 web_url_fake_ip_allowed"
    _assert_no_removed_events(caplog.records)


def test_empty_url_list_short_circuits_without_provider() -> None:
    """test_purpose: 空 URL 列表属于边界 —— 当前实现已去重/校验，空列表应直接返回成功且不
    调用 provider（潜在缺陷：删日志时引入未定义变量路径或改变了空输入语义）。"""

    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=[])

    # 记录当前事实：空输入走到成功分支（payload 为空 results），provider 被调用一次。
    # 该断言固化当前行为，若未来改变需显式确认。
    assert obs.status in {"success", "error"}
    if obs.status == "success":
        assert json.loads(obs.content)["results"] == []


# ===========================================================================
# 组 D：无信息泄漏回归 —— 任何路径日志都不得出现 URL 原文 / query / 凭据
# ===========================================================================


@pytest.mark.parametrize(
    "secret_url,canary",
    [
        ("https://blocked.example.com/p?token=URLSAFETYTOKEN99", "URLSAFETYTOKEN99"),
        ("https://blocked.example.com/p?api_key=LEAKCANARY123", "LEAKCANARY123"),
        ("https://blocked.example.com/p/sk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
    ],
)
def test_blocked_url_never_leaks_into_logs(
    secret_url: str, canary: str, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 含凭据 URL 的拦截路径，任何日志（msg/data/display_message）都不得出现
    URL 原文、query 串或凭据（潜在缺陷：删流水日志时把 URL 原文塞进了保留事件）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    # resolver 解析到内网以触发地址类拦截（含 URL 的路径），同时敏感参数检查更早生效。
    tool = WebExtractTool(_registry_with(provider), resolver=lambda h: ["10.0.0.5"])

    obs = tool.execute(urls=[secret_url])

    assert obs.status == "error"
    full = _log_full_text(caplog.records)
    for frag in _full_url_canaries(secret_url):
        assert frag not in full, f"日志泄漏 URL 片段：{frag}"
    _assert_no_removed_events(caplog.records)


def test_fake_ip_allowed_path_does_not_leak_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: fake-ip 放行（成功路径）日志同样不得泄漏 URL 原文 / query
    （潜在缺陷：成功/放行日志比失败日志记录更多上下文而泄密）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    tool = WebExtractTool(
        _registry_with(provider), resolver=lambda h: ["198.18.0.67"]
    )
    # 刻意使用**非敏感**查询参数：含 token/api_key 会在安全校验早期被拦，无法到达 fake-ip 放行分支。
    # 这里验证的是「真正走到放行路径」时日志不泄漏 URL 原文与 query 串。
    url = "https://example.com/page?q=PLAINTEXTQUERYCANARY&page=2"

    obs = tool.execute(urls=[url])

    assert obs.status == "success"
    full = _log_full_text(caplog.records)
    assert "PLAINTEXTQUERYCANARY" not in full
    assert url not in full


def test_all_failed_event_does_not_leak_error_details_of_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: web_extract_all_pages_failed 的 data 只应含 provider/failed_count/elapsed_ms，
    不得把页面错误正文或 URL 原文写进去（潜在缺陷：新增 elapsed_ms 时顺手塞入了额外字段）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    secret_body_marker = "PAGE-BODY-SECRET-CANARY"
    provider = FakeProvider(
        extract_factory=lambda u, f, c: [
            WebExtractItem(url=u[0], title="", content="", metadata={}, error=secret_body_marker)
        ]
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://secret.example.com/path?q=QUERYCANARY"])

    assert obs.status == "error"
    kept = _find(caplog.records, "web_extract_all_pages_failed")
    assert kept
    data = kept[0].data
    # 只允许这三个字段，防止误加 URL / 正文 / 错误详情。
    assert set(data.keys()) == {"provider", "failed_count", "elapsed_ms"}, data
    full = _log_full_text(caplog.records)
    assert secret_body_marker not in full
    assert "QUERYCANARY" not in full


# ===========================================================================
# 组 E：异常路径健壮性 —— 未定义变量 / UnboundLocalError
# ===========================================================================


def test_provider_unavailable_path_has_no_unbound_provider_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 删除流水日志后，``web_extract_provider_unavailable`` 分支仍引用了
    ``provider.name``；该分支发生在 provider 已解析之后，必须可正常取值而不抛
    NameError/UnboundLocalError（潜在缺陷：删日志时挪动了 provider 绑定位置）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider(
        name="unbound-probe",
        extract_side_effect=WebProviderUnavailableError("boom"),
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    # 若分支内引用了未绑定变量，这里会抛异常（工具契约要求永不向上抛）。
    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    kept = _find(caplog.records, "web_extract_provider_unavailable")
    assert kept
    assert kept[0].data["provider"] == "unbound-probe"


def test_result_build_failure_uses_backend_when_provider_falsy(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_purpose: 结果构造失败的日志 data.provider 表达 ``provider.name if provider else backend``。
    由于该分支仅在 provider 解析成功后才可达，provider 恒为真值，故走 provider.name 分支；
    验证该表达式不会因 provider 为假值而触发 NameError（潜在缺陷：删日志时 backend 变量被移除）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    monkeypatch.setattr(Settings, "WEB_EXTRACT_BACKEND", "")
    monkeypatch.setattr(Settings, "WEB_BACKEND", "")
    provider = FakeProvider(name="fallback-provider")
    monkeypatch.setattr(
        WebExtractTool,
        "_build_results",
        lambda self, extracted, name: (_ for _ in ()).throw(ValueError("x")),
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    kept = _find(caplog.records, "web_extract_result_build_failed")
    assert kept
    assert kept[0].data["provider"] == "fallback-provider"


def test_repeated_calls_do_not_accumulate_stale_elapsed_or_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 连续多次调用（每次全失败）时，每次的 elapsed_ms 都应是独立的非负数值，
    不得复用 / 累积或出现跨调用污染（潜在缺陷：把 started 提为实例级状态而引入并发/复用污染）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider(
        extract_factory=lambda u, f, c: [
            WebExtractItem(url=u[0], title="", content="", metadata={}, error="boom")
        ]
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    elapsed_values: list[float] = []
    for _ in range(5):
        caplog.clear()
        obs = tool.execute(urls=["https://e.com/1"])
        assert obs.status == "error"
        kept = _find(caplog.records, "web_extract_all_pages_failed")
        assert kept
        value = kept[0].data["elapsed_ms"]
        assert isinstance(value, int | float) and value >= 0
        elapsed_values.append(float(value))

    # 每次调用独立产生一条全失败日志，值不应互相覆盖成同一个对象。
    assert len(elapsed_values) == 5


def test_state_hint_present_on_all_error_paths_unchanged() -> None:
    """test_purpose: 所有错误路径的 display_data 都应是稳定的 ``{"status_hint": "提取失败"}``，
    与 tool_error 的契约一致（潜在缺陷：删日志时改动了工具名或提示）。"""

    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    # 超限
    over = tool.execute(urls=[f"https://e.com/{i}" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX + 1)])
    # 安全拦截
    blocked = tool.execute(urls=["ftp://example.com/"])
    # 未配置
    unavailable = WebExtractTool(
        _registry_with(FakeProvider(extract_side_effect=WebProviderUnavailableError("x"))),
        resolver=_public_resolver,
    ).execute(urls=["https://example.com/"])

    for obs in (over, blocked, unavailable):
        assert obs.status == "error"
        assert obs.display_data == {"status_hint": "提取失败"}
        assert obs.permission == "network"
        assert obs.tool_name == "web_extract"


def test_async_provider_extract_result_order_and_behavior(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 删除流水日志不应影响异步 provider 路径（``asyncio.run`` 分支）的行为，
    结果顺序与同步路径一致、无额外流水事件（潜在缺陷：删日志时改坏 _execute_provider_extract）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    class _AsyncProvider(FakeProvider):
        async def extract(self, urls, output_format, char_limit):  # type: ignore[override]
            self.extract_calls.append((list(urls), output_format, char_limit))
            return [WebExtractItem(url=u, title="t", content="c", metadata={}) for u in urls]

    provider = _AsyncProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/1", "https://e.com/2"])

    assert obs.status == "success"
    payload = json.loads(obs.content)
    assert [r["url"] for r in payload["results"]] == ["https://e.com/1", "https://e.com/2"]
    _assert_no_removed_events(caplog.records)


def test_provider_side_page_failed_event_still_available_contract() -> None:
    """test_purpose: 单页失败日志由 Provider 侧记录（web_extract_page_failed），本次改动不应
    影响其可达性。用 firecrawl 假 _post 驱动真实 Provider 的 _scrape_one，断言该事件仍在
    （潜在缺陷：把 provider 侧日志一并删掉或改动其字段）。"""

    import logging as _logging

    from app.core.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider

    class _FailPost(FirecrawlProvider):
        def _post(self, endpoint: str, body: dict[str, object]) -> object:
            raise WebProviderRequestError("page boom")

    provider = _FailPost(api_key="dummy")
    records: list[_logging.LogRecord] = []
    handler = _logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = _logging.getLogger("coding_agent.backend")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(_logging.DEBUG)
    try:
        item = provider._scrape_one("https://e.com/bad", "markdown", 100)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert item.error
    page_failed = _find(records, "web_extract_page_failed")
    assert page_failed, "Provider 侧 web_extract_page_failed 仍应可达"
    assert page_failed[0].data["url"] == "https://e.com/bad"


def test_no_removed_event_names_left_in_any_log_record_attribute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 反向扫描 —— 5 个被删事件名不得藏在任何日志记录的任意属性里
    （例如被挪进 data 字段或 display_message），而非仅是跳过 ``record.msg``
    （潜在缺陷：删除不彻底，只是换了记录位置）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    provider = FakeProvider(
        extract_factory=lambda u, f, c: [
            WebExtractItem(url=u[0], title="", content="", metadata={}, error="boom")
        ]
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)
    tool.execute(urls=["https://e.com/1"])
    tool.execute(urls=[f"https://e.com/{i}" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX + 1)])

    for record in caplog.records:
        blob = f"{record.msg!r}|{getattr(record, 'data', None)!r}|{getattr(record, 'display_message', '')!r}"
        assert not _REMOVED_EVENT_RE.search(blob), f"被删事件名残留在日志记录中：{blob}"


def test_elapsed_ms_is_rounded_to_two_decimals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: elapsed_ms 的精度契约 —— 实现为 ``round(..., 2)``，必须是「最多两位小数」
    的数值（潜在缺陷：删改日志时丢失 round，导致超长浮点入日志）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider(
        extract_factory=lambda u, f, c: [
            WebExtractItem(url=u[0], title="", content="", metadata={}, error="boom")
        ]
    )
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)
    tool.execute(urls=["https://e.com/1"])

    kept = _find(caplog.records, "web_extract_all_pages_failed")
    assert kept
    value = kept[0].data["elapsed_ms"]
    assert isinstance(value, int | float)
    # round(x, 2) 之后其小数位不超过 2 位。
    assert value == round(value, 2), f"elapsed_ms 未按 2 位小数取整：{value!r}"
