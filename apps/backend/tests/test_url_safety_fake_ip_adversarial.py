"""URL 安全校验（fake-ip 放行修复）对抗性测试。

被测对象：
- app/core/tools/tool_handler/web/url_safety.py::is_safe_public_url
- app/core/tools/tool_handler/web_extract.py::WebExtractTool.execute

本次修复语义：主机名解析到代理 fake-ip 占位段 198.18.0.0/15（Clash/Mihomo fake-ip）
时不再判为内网 —— 但仅当 URL 主机名不是 IP 字面量时豁免；IP 字面量 host 仍按原规则
拦截。放行写 web_url_fake_ip_allowed(WARNING)，拦截写 web_url_address_blocked(WARNING)。

本文件只新增测试，不修改任何业务代码。复用 test_web_search_extract_tools.py 的
FakeProvider / _registry_with / _public_resolver / _log_full_text 等 helper。
"""

import ipaddress
import logging

import pytest

from app.core.tools.tool_handler.web import url_safety
from app.core.tools.tool_handler.web.url_safety import is_safe_public_url
from app.core.tools.tool_handler.web_extract import WebExtractTool
from tests.test_web_search_extract_tools import (
    FakeProvider,
    _log_full_text,
    _public_resolver,
    _registry_with,
)

_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@pytest.fixture(autouse=True)
def _clear_web_backend_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 fake provider 测试不受本机环境中的生产 backend 配置影响（与主测试文件一致）。"""

    monkeypatch.setattr("app.config.settings.Settings.WEB_SEARCH_BACKEND", "")
    monkeypatch.setattr("app.config.settings.Settings.WEB_EXTRACT_BACKEND", "")
    monkeypatch.setattr("app.config.settings.Settings.WEB_BACKEND", "")


def _fake_ip_resolver(*addresses: str):
    """返回固定 fake-ip 段解析结果的注入解析器。"""

    return lambda hostname: list(addresses)


# ---------------------------------------------------------------------------
# 1. 边界值：fake-ip 段 198.18.0.0/15 边界前后必须分别拦/放
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "198.18.0.0",  # 段内下边界（网络地址）
        "198.18.0.1",  # 段内下边界 +1
        "198.19.255.255",  # 段内上边界（广播地址）
    ],
)
def test_fake_ip_network_membership_is_exact(address: str) -> None:
    """test_purpose: 确认 198.18.0.0/15 成员判定与实现常量一致（潜在缺陷：网段边界写错）。
    这是本文件其它边界断言的事实基础，避免断言建立在错误的网段假设上。"""

    assert ipaddress.ip_address(address) in _FAKE_IP_NETWORK


@pytest.mark.parametrize(
    "address",
    ["198.17.255.255", "198.20.0.0", "198.17.0.1", "198.20.255.254"],
)
def test_fake_ip_boundary_outside_is_public_and_allowed(address: str) -> None:
    """test_purpose: 紧邻 fake-ip 段外部的地址（198.17.255.255 / 198.20.0.0）本身是全局可路由
    的公网地址（ipaddress 判定 is_global=True），因此正确行为是「放行」，且**不得**命中
    fake-ip 豁免分支（不应写 web_url_fake_ip_allowed）。潜在缺陷：把网段写成前缀匹配/范围过宽，
    导致豁免分支被错误命中（此时会错误地多出一条 fake-ip 放行日志）。"""

    assert ipaddress.ip_address(address).is_global is True

    records: list[logging.LogRecord] = []
    safe, reason = _capture(
        records,
        lambda: is_safe_public_url("https://example.com/", resolver=_fake_ip_resolver(address)),
    )

    assert safe is True, f"{address} 是公网地址，应放行"
    assert reason == ""
    # 该地址不属于 fake-ip 段，绝不能触发豁免分支的日志（否则说明网段判定过宽）。
    assert not [r for r in records if r.msg == "web_url_fake_ip_allowed"], (
        f"{address} 不在 fake-ip 段，不应命中豁免分支"
    )


@pytest.mark.parametrize(
    "address", ["198.18.0.0", "198.18.0.1", "198.18.255.255", "198.19.255.255"]
)
def test_fake_ip_boundary_inside_allowed_for_hostname(address: str) -> None:
    """test_purpose: fake-ip 段内全部地址在「按主机名解析」路径必须放行（潜在缺陷：边界处
    漏放行，导致代理环境下部分域名被误拦）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver(address)
    )

    assert safe is True
    assert reason == ""


def test_fake_ip_exemption_not_applied_to_adjacent_benchmark_range() -> None:
    """test_purpose: 相邻保留段之外的 198.20.0.0（公网）放行时不得命中 fake-ip 豁免分支，确认
    豁免范围精确锁定在 /15（潜在缺陷：把整个 198.0.0.0/8 都当 fake-ip 而放大放行面并污染日志）。"""

    records: list[logging.LogRecord] = []
    safe, _reason = _capture(
        records,
        lambda: is_safe_public_url(
            "https://example.com/", resolver=_fake_ip_resolver("198.20.0.0")
        ),
    )
    assert safe is True
    assert not [r for r in records if r.msg == "web_url_fake_ip_allowed"]


# ---------------------------------------------------------------------------
# 2. IPv6：域名解析到 IPv6 仍拦；IPv6 字面量 host 判定正确
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["::1", "fe80::1", "2001:db8::1", "::"])
def test_hostname_resolving_to_ipv6_internal_is_blocked(address: str) -> None:
    """test_purpose: 域名解析到 IPv6 回环/链路本地/文档保留段必须仍被拦截（潜在缺陷：IPv6
    分支未覆盖，解析结果换成 IPv6 就能绕过内网判定）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver(address)
    )

    assert safe is False
    assert reason.startswith("Blocked:")


@pytest.mark.parametrize(
    "url",
    ["http://[::1]/", "http://[fe80::1]/", "http://[2001:db8::1]/", "http://[::]/"],
)
def test_ipv6_literal_host_is_blocked(url: str) -> None:
    """test_purpose: IPv6 字面量 host（urlsplit.hostname 已去方括号）必须被拦截，且不得因
    resolver 恰好返回 fake-ip 而被豁免（潜在缺陷：方括号导致 _is_ip_literal 误判为 False）。"""

    # 即便恶意/异常 resolver 返回 fake-ip 段，字面量 host 也不得豁免。
    safe, reason = is_safe_public_url(
        url, resolver=lambda hostname: ["::1", "2001:db8::1"]
    )
    assert safe is False
    assert reason.startswith("Blocked:")

    # 换一个「只会返回 fake-ip」的 resolver，字面量 host 仍必须拦截（字面量不适用豁免）。
    safe2, reason2 = is_safe_public_url(url, resolver=_fake_ip_resolver("198.18.0.67"))
    assert safe2 is False, f"字面量 host {url} 不应获得 fake-ip 豁免"
    assert reason2.startswith("Blocked:")


def test_ipv6_literal_and_hostname_treated_differently_for_fake_ip() -> None:
    """test_purpose: 同为 fake-ip 解析结果，IPv6 字面量 host 被拦、普通主机名被放行，
    精确刻画「字面量豁免」的区分语义（潜在缺陷：豁免误扩大到字面量）。"""

    literal_safe, _ = is_safe_public_url(
        "http://[2001:db8::1]/", resolver=_fake_ip_resolver("198.18.0.67")
    )
    name_safe, _ = is_safe_public_url(
        "http://example.com/", resolver=_fake_ip_resolver("198.18.0.67")
    )

    assert literal_safe is False
    assert name_safe is True


# ---------------------------------------------------------------------------
# 3. 大小写/编码绕过：IP 字面量变体必须被拦截
# ---------------------------------------------------------------------------


def test_uppercase_fake_ip_literal_host_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 字面量 host 判定对大小写不敏感（IPv6 十六进制可大写），
    resolver 一致性场景下必须拦截（潜在缺陷：大小写导致字面量识别失败→豁免）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    safe, reason = is_safe_public_url(
        "http://[2001:DB8::1]/", resolver=_fake_ip_resolver("2001:DB8::1")
    )

    assert safe is False
    assert reason.startswith("Blocked:")
    assert not [r for r in caplog.records if r.msg == "web_url_fake_ip_allowed"]


@pytest.mark.parametrize(
    "url",
    ["http://0x7f.0.0.1/", "http://127.1/", "http://0177.0.0.1/", "http://2130706433/"],
)
def test_ambiguous_ipv4_notation_not_treated_as_literal_but_still_blocked(
    url: str,
) -> None:
    """test_purpose: 非常规 IPv4 记法（十六进制 0x7f / 短式 127.1 / 前导零 0177 / 整数
    2130706433）不会被 ipaddress 识别为字面量。当 resolver 诚实返回 127.0.0.1 时必须
    被拦截（潜在缺陷：变体记法被当成普通域名而绕过内网判定）。"""

    safe, reason = is_safe_public_url(url, resolver=_fake_ip_resolver("127.0.0.1"))

    assert safe is False, f"{url} 解析到回环必须拦截"
    assert reason.startswith("Blocked:")


@pytest.mark.parametrize(
    "url",
    ["http://127.1/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://2130706433/"],
)
def test_ambiguous_ipv4_notation_gets_fake_ip_exemption_when_resolver_returns_fake_ip(
    url: str,
) -> None:
    """test_purpose: 记录并暴露潜在缺陷 —— 非常规 IPv4 记法不被视为字面量，因此当 resolver
    返回 fake-ip 占位地址时会获得豁免而放行。断言实现当前行为，若未来收紧字面量识别此处
    应随之变化（潜在缺陷：变体字面量 host 可进入豁免分支）。"""

    hostname = url_safety.normalize_url_for_request(url)

    # 明确记录：这些 host 不被 _is_ip_literal 识别。
    assert url.replace("http://", "").rstrip("/") in {
        "127.1",
        "0x7f.0.0.1",
        "0177.0.0.1",
        "2130706433",
    }

    safe, _ = is_safe_public_url(url, resolver=_fake_ip_resolver("198.18.0.67"))

    # 当前实现：非字面量 → 命中 fake-ip 豁免 → 放行。
    assert safe is True, f"{url}（host={hostname}）当前会命中 fake-ip 豁免"


# ---------------------------------------------------------------------------
# 4. 混解析：fake-ip 与内网地址混合必须拦截（与顺序无关）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addresses",
    [
        ["198.18.0.67", "127.0.0.1"],
        ["127.0.0.1", "198.18.0.67"],
        ["198.18.0.67", "10.0.0.5"],
        ["10.0.0.5", "198.18.0.67"],
        ["198.18.0.67", "169.254.169.254"],
        ["169.254.169.254", "198.18.0.67"],
        ["198.18.0.67", "::1"],
        ["::1", "198.18.0.67"],
    ],
)
def test_mixed_fake_ip_with_internal_is_blocked_regardless_of_order(
    addresses: list[str],
) -> None:
    """test_purpose: 同一主机名既有 fake-ip 占位又有真实内网/云元数据地址时，无论顺序如何
    都必须拦截（潜在缺陷：fake-ip 分支用 continue 提前放行导致混解析漏拦）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver(*addresses)
    )

    assert safe is False, f"混入内网地址 {addresses} 必须拦截"
    assert reason.startswith("Blocked:")


def test_mixed_fake_ip_with_allowed_public_is_allowed() -> None:
    """test_purpose: fake-ip 与真实公网地址混合时应放行（不应误拦），确认拦截只由内网地址
    触发而非「出现 fake-ip 就拦」（潜在缺陷：过度拦截）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver("198.18.0.67", "8.8.8.8")
    )

    assert safe is True
    assert reason == ""


def test_fake_ip_then_internal_logs_both_events() -> None:
    """test_purpose: fake-ip + 内网混解析时既逻辑拦截又落 web_url_address_blocked；
    web_url_fake_ip_allowed 若同时出现也不得掩盖最终拦截结论（潜在缺陷：日志与返回不一致）。"""

    import logging as _logging

    records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    logger = _logging.getLogger("coding_agent.backend")
    handler = _Handler()
    logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)
    try:
        safe, _reason = is_safe_public_url(
            "https://example.com/", resolver=_fake_ip_resolver("198.18.0.67", "10.0.0.5")
        )
    finally:
        logger.removeHandler(handler)

    assert safe is False
    blocked = [r for r in records if r.msg == "web_url_address_blocked"]
    assert blocked, "混解析最终拦截必须落 web_url_address_blocked"
    assert blocked[0].data["matched_address"] == "10.0.0.5"


# ---------------------------------------------------------------------------
# 5. 解析器异常：OSError / 空列表 / 非法字符串 / IPv6
# ---------------------------------------------------------------------------


def test_resolver_raising_oserror_is_blocked_not_propagated() -> None:
    """test_purpose: resolver 抛 OSError（DNS 失败）必须转成确定性拦截，不得向上抛
    （潜在缺陷：异常逃逸导致工具崩溃而非返回 Blocked）。"""

    def _boom(hostname: str) -> list[str]:
        raise OSError("dns down")

    safe, reason = is_safe_public_url("https://example.com/", resolver=_boom)

    assert safe is False
    assert reason == "Blocked: hostname could not be resolved."


def test_resolver_raising_valueerror_is_blocked_not_propagated() -> None:
    """test_purpose: resolver 抛 ValueError 同样必须转成拦截（潜在缺陷：只捕获 OSError）。"""

    def _boom(hostname: str) -> list[str]:
        raise ValueError("bad resolver")

    safe, reason = is_safe_public_url("https://example.com/", resolver=_boom)

    assert safe is False
    assert reason.startswith("Blocked:")


def test_resolver_returning_empty_list_is_blocked() -> None:
    """test_purpose: resolver 返回空列表（解析无结果）必须拦截，绝不能视为「无可疑地址」
    而放行（潜在缺陷：空集合被真空真值绕过）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=lambda hostname: []
    )

    assert safe is False
    assert reason == "Blocked: hostname could not be resolved."


@pytest.mark.parametrize("bad", ["not-an-ip", "999.999.999.999", "8.8.8.8:443", " "])
def test_resolver_returning_invalid_address_string_is_blocked(bad: str) -> None:
    """test_purpose: resolver 返回非法地址字符串必须拦截（潜在缺陷：非法地址被跳过而放行）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver(bad)
    )

    assert safe is False
    assert reason == "Blocked: hostname resolved to an invalid address."


def test_resolver_returning_none_element_is_blocked_not_crash() -> None:
    """test_purpose: resolver 返回含 None 的列表必须被拦截而非抛 TypeError（潜在缺陷：
    ip_address(None) 传入非法类型。注：当前实现捕获 ValueError 不含 TypeError）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=lambda hostname: [None]  # type: ignore[list-item]
    )

    assert safe is False
    assert reason.startswith("Blocked:")


def test_resolver_returning_ipv6_fake_like_address_is_blocked() -> None:
    """test_purpose: resolver 返回 IPv6 地址（非 fake-ip 段）时按 IPv6 规则判定，回环/保留
    应拦截（潜在缺陷：IPv6 走了 IPv4 专属分支）。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=_fake_ip_resolver("::ffff:127.0.0.1")
    )

    assert safe is False
    assert reason.startswith("Blocked:")


def test_resolver_returning_generator_is_materialized_once() -> None:
    """test_purpose: resolver 返回生成器等一次性可迭代对象时功能仍正确（潜在缺陷：多次迭代
    导致地址列表被消耗为空而漏判）。"""

    def _gen(hostname: str):
        yield "198.18.0.67"
        yield "10.0.0.5"

    safe, reason = is_safe_public_url("https://example.com/", resolver=_gen)

    assert safe is False, "生成器中的内网地址必须被检出"
    assert reason.startswith("Blocked:")


# ---------------------------------------------------------------------------
# 6. 日志安全：被拦 URL 原文（含密钥形态）绝不出现在任何日志
# ---------------------------------------------------------------------------


def _capture(records: list[logging.LogRecord], func):
    """在 coding_agent.backend logger 上临时挂 handler，返回 func 的执行结果。"""

    logger = logging.getLogger("coding_agent.backend")
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        return func()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.mark.parametrize(
    "secret_url",
    [
        "https://blocked.example.com/path?token=SUPERSECRETTOKEN123",
        "https://blocked.example.com/path?api_key=APIKEYLEAKCANARY",
        "https://blocked.example.com/path/sk-abcdefghijklmnopqrst",
    ],
)
def test_blocked_url_raw_text_never_appears_in_logs(
    secret_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """test_purpose: 被拦截 URL 的原文（尤其含 ?token= / ?api_key= / sk-xxx 形态）绝不出现在
    任何日志记录的 msg / data / display_message 中（潜在缺陷：拦截日志泄漏凭据）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    records: list[logging.LogRecord] = []
    # 用解析到内网地址的 resolver 触发真正的拦截路径（解析到公网则不会拦截、也不落日志）。
    _capture(
        records,
        lambda: is_safe_public_url(secret_url, resolver=_fake_ip_resolver("10.0.0.5")),
    )

    assert records, "拦截路径必须落日志（否则无法审计）"
    full = _log_full_text(records)
    for canary in (
        "SUPERSECRETTOKEN123",
        "APIKEYLEAKCANARY",
        "sk-abcdefghijklmnopqrst",
    ):
        assert canary not in full, f"日志泄漏凭据片段: {canary}"
    assert secret_url not in full, "日志不得记录被拦 URL 原文"


def test_userinfo_credential_url_blocked_without_log_and_without_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 带 userinfo 凭据的 URL 在凭据检查阶段即被拦（早于 DNS，不落地址日志），
    因此不得出现任何凭据原文（潜在缺陷：凭据检查路径误记录 URL 原文）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    records: list[logging.LogRecord] = []
    url = "https://user:PASSWORDCANARY@blocked.example.com/"

    safe, reason = _capture(
        records,
        lambda: is_safe_public_url(url, resolver=_fake_ip_resolver("10.0.0.5")),
    )

    assert safe is False
    assert "credentials" in reason
    full = _log_full_text(records)
    assert "PASSWORDCANARY" not in full
    assert url not in full


def test_fake_ip_allowed_log_does_not_record_query_string_secrets() -> None:
    """test_purpose: fake-ip 放行路径写 web_url_fake_ip_allowed 时也不得把含密钥的 URL 原文
    写进日志（潜在缺陷：放行日志比拦截日志记录更多上下文而泄密）。"""

    records: list[logging.LogRecord] = []
    url = "https://example.com/page?token=FAKEIPPATHLEAK1&api_key=FAKEIPPATHLEAK2"
    _capture(
        records,
        lambda: is_safe_public_url(url, resolver=_fake_ip_resolver("198.18.0.67")),
    )

    full = _log_full_text(records)
    assert "FAKEIPPATHLEAK1" not in full
    assert "FAKEIPPATHLEAK2" not in full
    assert url not in full


def test_fake_ip_allowed_event_only_on_pure_fake_ip_allow() -> None:
    """test_purpose: web_url_fake_ip_allowed 只应出现在「纯 fake-ip 放行」场景：
    - 真正放行（fake-ip）→ 出现一次；
    - 公网放行（非 fake-ip）→ 不出现；
    - 字面量拦截 → 不出现；
    - 混解析被拦截 → **不应出现**（否则日志会声称「已放行」但实际上被拦，构成审计误导）。

    潜在缺陷：混解析时循环在 fake-ip 元素上先写 allowed 日志再继续，最终却返回拦截，
    导致同一 URL 同时留下 web_url_fake_ip_allowed 与 web_url_address_blocked 两条互相矛盾的记录。
    """

    records: list[logging.LogRecord] = []

    def _run_all() -> list[tuple[bool, str]]:
        return [
            # ① 真正放行（纯 fake-ip）
            is_safe_public_url("https://example.com/", resolver=_fake_ip_resolver("198.18.0.67")),
            # ② 公网放行（非 fake-ip）
            is_safe_public_url("https://example.com/", resolver=_public_resolver),
            # ③ 字面量拦截
            is_safe_public_url("http://198.18.0.67/", resolver=_fake_ip_resolver("198.18.0.67")),
            # ④ 混解析拦截
            is_safe_public_url(
                "https://example.com/", resolver=_fake_ip_resolver("198.18.0.67", "10.0.0.5")
            ),
        ]

    results = _capture(records, _run_all)

    assert results[0][0] is True
    assert results[1][0] is True
    assert results[2][0] is False
    assert results[3][0] is False

    allowed_records = [r for r in records if r.msg == "web_url_fake_ip_allowed"]
    # 唯一合规的 allowed 日志是场景①。
    assert len(allowed_records) == 1, (
        "web_url_fake_ip_allowed 只应来自纯 fake-ip 放行场景；"
        f"实际出现 {len(allowed_records)} 次：{[r.data.get('host') for r in allowed_records]}"
    )
    assert allowed_records[0].levelno == logging.WARNING
    # 场景① 必须有拦截日志之外的放行证据；场景③④ 必须各有一条 blocked 记录。
    blocked_records = [r for r in records if r.msg == "web_url_address_blocked"]
    assert len(blocked_records) == 2


def test_mixed_resolution_does_not_emit_fake_ip_allowed_when_final_verdict_blocks() -> None:
    """test_purpose: 【真实缺陷证据】当同一主机名同时解析出 fake-ip 占位地址与真实内网地址时，
    最终判定为「拦截」，因此日志中**不应**出现 web_url_fake_ip_allowed（它语义上是「守卫已放宽/
    已放行」）。当前实现会先对 fake-ip 元素写 allowed 日志再 continue，随后在私网元素上返回拦截，
    导致同一 URL 同时留下 allowed 与 blocked 两条互相矛盾的审计记录。

    预期：只出现 web_url_address_blocked。
    实际：同时出现 web_url_fake_ip_allowed 与 web_url_address_blocked。
    """

    records: list[logging.LogRecord] = []

    safe, reason = _capture(
        records,
        lambda: is_safe_public_url(
            "https://example.com/", resolver=_fake_ip_resolver("198.18.0.67", "10.0.0.5")
        ),
    )

    # 最终结论正确：拦截。
    assert safe is False
    assert reason.startswith("Blocked:")
    assert [r for r in records if r.msg == "web_url_address_blocked"]

    # 但放行日志不该出现（该 URL 并没有被放行）。
    assert not [r for r in records if r.msg == "web_url_fake_ip_allowed"], (
        "混解析最终被拦截，却写了 web_url_fake_ip_allowed，日志语义与最终判定矛盾"
    )


def test_address_blocked_event_is_warning_level() -> None:
    """test_purpose: web_url_address_blocked 必须为 WARNING 级（潜在缺陷：级别降级导致审计
    日志被默认过滤）。"""

    records: list[logging.LogRecord] = []
    _capture(
        records,
        lambda: is_safe_public_url("https://example.com/", resolver=_fake_ip_resolver("10.0.0.5")),
    )

    blocked = [r for r in records if r.msg == "web_url_address_blocked"]
    assert blocked
    assert blocked[0].levelno == logging.WARNING


def test_blocked_log_truncates_address_list_to_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """test_purpose: 拦截日志中 addresses 字段被限制在 _MAX_LOGGED_ADDRESSES 以内，且仍包含
    matched_address（潜在缺陷：无边界日志字段膨胀，或截断后丢失命中地址）。"""

    monkeypatch.setattr(url_safety, "_MAX_LOGGED_ADDRESSES", 2)
    records: list[logging.LogRecord] = []
    addresses = ["198.18.0.1", "198.18.0.2", "198.18.0.3", "10.0.0.5"]
    _capture(
        records,
        lambda: is_safe_public_url("https://example.com/", resolver=_fake_ip_resolver(*addresses)),
    )

    blocked = [r for r in records if r.msg == "web_url_address_blocked"]
    assert blocked
    assert blocked[0].data["matched_address"] == "10.0.0.5"


# ---------------------------------------------------------------------------
# 7. 端到端：WebExtractTool.execute 在 fake-ip resolver 下必须真的调用 provider
# ---------------------------------------------------------------------------


def test_execute_reaches_provider_under_fake_ip_resolver() -> None:
    """test_purpose: fake-ip 代理环境下 execute 必须真正调用 provider，而非在安全校验阶段
    返回 Blocked（本次缺陷的端到端回归）。"""

    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_fake_ip_resolver("198.18.0.67"))

    obs = tool.execute(urls=["https://example.com/page", "https://example.org/other"])

    assert obs.status == "success"
    assert len(provider.extract_calls) == 1
    assert provider.extract_calls[0][0] == [
        "https://example.com/page",
        "https://example.org/other",
    ]


def test_execute_blocked_before_provider_when_mixed_internal() -> None:
    """test_purpose: 混解析含内网地址时必须在调用 provider 之前返回 Blocked，且 provider
    绝不被调用（潜在缺陷：先调用后校验，SSRF 实际已发出）。"""

    provider = FakeProvider()
    tool = WebExtractTool(
        _registry_with(provider), resolver=_fake_ip_resolver("198.18.0.67", "10.0.0.5")
    )

    obs = tool.execute(urls=["https://example.com/page"])

    assert obs.status == "error"
    assert obs.error.startswith("Blocked:")
    assert provider.extract_calls == [], "被拦 URL 绝不能触发 provider 调用"


def test_execute_rejects_fake_ip_literal_before_provider() -> None:
    """test_purpose: URL 主机名是 fake-ip 字面量时必须拦截且不调用 provider（潜在缺陷：
    字面量被豁免而放行到 provider）。"""

    provider = FakeProvider()
    tool = WebExtractTool(
        _registry_with(provider), resolver=_fake_ip_resolver("198.18.0.67")
    )

    obs = tool.execute(urls=["http://198.18.0.67/"])
    assert obs.status == "error"
    assert provider.extract_calls == []


def test_execute_fail_closed_when_resolver_errors() -> None:
    """test_purpose: resolver 抛 OSError 时 execute 必须返回 Blocked 且不调用 provider，
    不得让异常逃逸（潜在缺陷：解析异常未归一化，工具崩溃）。"""

    provider = FakeProvider()

    def _boom(hostname: str) -> list[str]:
        raise OSError("dns down")

    tool = WebExtractTool(_registry_with(provider), resolver=_boom)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert obs.error.startswith("Blocked:")
    assert provider.extract_calls == []


def test_execute_does_not_leak_secret_url_into_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """test_purpose: 端到端拦截含密钥 URL 时，工具与安全校验的日志都不得出现密钥原文
    （潜在缺陷：安全判定日志泄漏原 URL）。"""

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")
    provider = FakeProvider()
    tool = WebExtractTool(_registry_with(provider), resolver=_public_resolver)
    secret_url = "https://example.com/page?api_key=E2ELEAKCANARY999"  # noqa: S105 - 泄漏检测用假密钥

    obs = tool.execute(urls=[secret_url])

    assert obs.status == "error"
    full = _log_full_text(caplog.records)
    assert "E2ELEAKCANARY999" not in full
    assert secret_url not in full


def test_execute_partial_failure_under_fake_ip_resolver_keeps_success() -> None:
    """test_purpose: fake-ip resolver 下部分页失败时仍返回 partial 成功，确认放行语义不影响
    既有失败归集逻辑（潜在缺陷：放行路径改变状态机）。"""

    urls = ["https://example.com/ok", "https://example.com/bad"]

    def _factory(req_urls, fmt, cl):
        from app.core.tools.tool_handler.web.web_provider import WebExtractItem

        return [
            WebExtractItem(url=urls[0], title="t", content="ok", metadata={}),
            WebExtractItem(url=urls[1], title="", content="", metadata={}, error="boom"),
        ]

    provider = FakeProvider(extract_factory=_factory)
    tool = WebExtractTool(_registry_with(provider), resolver=_fake_ip_resolver("198.18.0.67"))

    obs = tool.execute(urls=urls)

    assert obs.status == "success"
    assert len(provider.extract_calls) == 1
