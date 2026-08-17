"""日志分页功能单元测试。

覆盖 storage 层 ``LogStore.query`` 分页（真实内存 sqlite）、keyword 子串过滤与 LIKE 转义、
``count_by_level`` 级别分布统计、service 层 ``has_more`` 计算、``LogQueryResult.to_dict()``
的 total/has_more/level_counts 回归点，以及纯函数
``_build_select`` / ``_build_count`` / ``_apply_filters`` / ``_validate_query`` / ``_escape_like``。

不修改任何业务代码；仅编写测试以暴露分页链路行为契约与潜在缺陷。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.models.log_entry_record import LogEntryRecord
from app.models.log_query import LogQuery
from app.models.log_query_result import LogQueryResult
from app.service.log_query_service import LogQueryService
from app.storage.crud.log_crud import (
    LogStore,
    _apply_filters,
    _build_count,
    _build_select,
    _escape_like,
    _validate_query,
)
from app.storage.store_engines import close_storage, init_storage

# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------

def _make_entry(ts: str, level: str, trace_id: str, event: str, n: int = 0) -> LogEntryRecord:
    """构造一条结构化日志，msg 中带序号以便分页断言。"""
    return LogEntryRecord(
        ts=ts,
        level=level,
        logger="coding_agent.backend",
        trace_id=trace_id,
        caller=f"caller.{n}",
        event=event,
        msg=f"msg-{n}",
        data={"n": n},
    )


def _seed_entries(count: int, trace_id: str = "trace-a") -> list[LogEntryRecord]:
    """生成 count 条按时间递增、序号 0..count-1 的日志。"""
    base = datetime(2026, 8, 13, 1, 0, 0, tzinfo=timezone.utc)
    out: list[LogEntryRecord] = []
    for i in range(count):
        ts = (base.replace(minute=i)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        out.append(_make_entry(ts, "INFO" if i % 2 == 0 else "WARNING", trace_id, "evt", i))
    return out


# ---------------------------------------------------------------------------
# 纯函数：_validate_query
# ---------------------------------------------------------------------------

# 测试目的：验证 offset < 0 时 _validate_query 抛 ValueError。可能发现的缺陷：offset 边界校验缺失。
def test_validate_query_rejects_negative_offset() -> None:
    with pytest.raises(ValueError):
        _validate_query(LogQuery(offset=-1, limit=10))


# 测试目的：验证 limit < 1 时抛 ValueError（既有契约，附带确认）。可能发现的缺陷：limit 校验缺失。
def test_validate_query_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        _validate_query(LogQuery(offset=0, limit=0))


# 测试目的：验证非法 order 抛 ValueError（既有契约，附带确认）。可能发现的缺陷：order 校验缺失。
def test_validate_query_rejects_invalid_order() -> None:
    with pytest.raises(ValueError):
        _validate_query(LogQuery(offset=0, limit=10, order="sideways"))


# 测试目的：验证合法参数不抛异常。可能发现的缺陷：误判合法查询为非法。
def test_validate_query_accepts_valid_query() -> None:
    _validate_query(LogQuery(offset=0, limit=10, order="asc"))
    _validate_query(LogQuery(offset=5, limit=10, order="desc"))


# ---------------------------------------------------------------------------
# 纯函数：_build_select / _build_count / _apply_filters 的 SQL 片段
# ---------------------------------------------------------------------------

# 测试目的：验证 _build_select 对 offset/limit 的切片参数正确进入 SQL。可能发现的缺陷：offset/limit 未真正下推。
def test_build_select_applies_offset_and_limit() -> None:
    stmt = _build_select(LogQuery(offset=20, limit=5, order="asc"))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " OFFSET 20" in compiled
    assert " LIMIT 5" in compiled


# 测试目的：验证默认查询 _build_select 不带 WAIT/ORDER 漂移。可能发现的缺陷：默认 offset 非 0。
def test_build_select_default_offset_zero() -> None:
    stmt = _build_select(LogQuery(limit=10))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " OFFSET 0" in compiled


# 测试目的：验证 _build_select 排序方向：desc 使用降序。可能发现的缺陷：order 参数未影响排序。
def test_build_select_order_desc() -> None:
    stmt = _build_select(LogQuery(limit=10, order="desc"))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " DESC" in compiled


# 测试目的：验证 _build_count 不含 ORDER BY / LIMIT / OFFSET（分页计数必须统计全部过滤后记录）。
# 可能发现的缺陷：count 误带 limit/offset，导致 total 偏小。
def test_build_count_has_no_limit_offset_order() -> None:
    stmt = _build_count(LogQuery(offset=15, limit=3, order="desc"))
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "count(" in compiled
    assert " LIMIT " not in compiled
    assert " OFFSET " not in compiled
    assert " ORDER BY " not in compiled


# 测试目的：验证 _apply_filters 空查询不加 WHERE；非空条件追加 WHERE（trace_id/level/event/时间）。
# 可能发现的缺陷：过滤条件未生效或错误地过滤空串。
def test_apply_filters_no_where_when_empty() -> None:
    stmt = _apply_filters(_build_select(LogQuery()), LogQuery())
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " WHERE " not in compiled


# 测试目的：验证 _apply_filters 对非空 trace_id/level/event_name 追加精确匹配 WHERE。
# 可能发现的缺陷：过滤字段映射错误（如 event_name 错配到错误的列）。
def test_apply_filters_appends_where_for_each_field() -> None:
    query = LogQuery(trace_id="trace-a", level="INFO", event_name="evt", start_time="2026-01-01T00:00:00.000Z", end_time="2026-12-31T00:00:00.000Z")
    stmt = _apply_filters(_build_select(query), query)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "trace_id = " in compiled
    assert "level = " in compiled
    assert "event = " in compiled
    assert "ts >= " in compiled
    assert "ts <= " in compiled


# 测试目的：验证 _apply_filters 不会为空串过滤条件生成 WHERE（避免过滤掉全量）。
# 可能发现的缺陷：空串被当作有效过滤条件，导致查询结果为空。
def test_apply_filters_ignores_blank_values() -> None:
    query = LogQuery(trace_id="", level="", event_name="", start_time="", end_time="")
    stmt = _apply_filters(_build_select(query), query)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " WHERE " not in compiled


# 测试目的：验证 _escape_like 转义 % / _ / \ 通配符。可能发现的缺陷：转义顺序错误导致漏转义。
def test_escape_like_escapes_wildcards() -> None:
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    assert _escape_like("a\\b") == "a\\\\b"
    assert _escape_like("plain") == "plain"


# 测试目的：验证 keyword 追加 msg LIKE 子串且带 escape。可能发现的缺陷：keyword 未进入 WHERE。
def test_apply_filters_appends_keyword_like() -> None:
    query = LogQuery(keyword="boom")
    stmt = _apply_filters(_build_select(query), query)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "msg LIKE" in compiled
    assert "%boom%" in compiled


# 测试目的：验证空 keyword 不产生 WHERE。可能发现的缺陷：空 keyword 被当作有效过滤条件。
def test_apply_filters_ignores_blank_keyword() -> None:
    query = LogQuery(keyword="")
    stmt = _apply_filters(_build_select(query), query)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert " WHERE " not in compiled


# ---------------------------------------------------------------------------
# LogQueryResult.to_dict 回归点
# ---------------------------------------------------------------------------

# 测试目的：验证 to_dict() 返回 total 与 has_more（本次修复关键回归点——此前漏回导致前端永远 0/False）。
# 可能发现的缺陷：to_dict 不含 total / has_more 字段。
def test_result_to_dict_includes_total_and_has_more() -> None:
    entries = [_make_entry("2026-08-13T01:00:00.000Z", "INFO", "t", "e", 0)]
    result = LogQueryResult(entries=entries, text="x", total=42, has_more=True)
    d = result.to_dict()
    assert d["total"] == 42
    assert d["has_more"] is True
    assert "entries" in d
    assert "text" in d


# 测试目的：验证 to_dict() 默认值 total=0 / has_more=False。可能发现的缺陷：默认值与契约不符。
def test_result_to_dict_defaults() -> None:
    result = LogQueryResult(entries=[], text="")
    d = result.to_dict()
    assert d["total"] == 0
    assert d["has_more"] is False


# 测试目的：验证 to_dict() 包含 level_counts。可能发现的缺陷：to_dict 漏回 level_counts。
def test_result_to_dict_includes_level_counts() -> None:
    result = LogQueryResult(
        entries=[],
        text="",
        total=3,
        has_more=False,
        level_counts={"ERROR": 1, "INFO": 2},
    )
    d = result.to_dict()
    assert d["level_counts"] == {"ERROR": 1, "INFO": 2}


# 测试目的：验证 level_counts 默认空字典。可能发现的缺陷：默认值为 None 导致前端空指针。
def test_result_to_dict_level_counts_defaults_empty() -> None:
    result = LogQueryResult(entries=[], text="")
    assert result.level_counts == {}
    assert result.to_dict()["level_counts"] == {}


# ---------------------------------------------------------------------------
# storage 层真实分页：LogStore.query
# ---------------------------------------------------------------------------

@pytest.fixture
def log_store(tmp_path: Path) -> LogStore:
    """用临时文件初始化日志库，返回已就绪的 LogStore，测试后释放。"""
    log_db = tmp_path / "logs.sqlite3"
    app_db = tmp_path / "app.sqlite3"
    cp_db = tmp_path / "cp.sqlite3"
    # 覆盖 Settings 静态路径，再初始化真实存储
    import app.config.settings as settings

    settings.Settings.LOG_DATABASE_FILE = log_db
    settings.Settings.DATABASE_FILE = app_db
    settings.Settings.CHECKPOINT_FILE = cp_db
    # 清空可能存在的引擎缓存（不同路径已是新文件，安全）
    init_storage()
    store = LogStore()
    yield store
    close_storage()
    # 还原为 None 以免污染其他测试进程态（模块级缓存已 dispose）
    settings.Settings.LOG_DATABASE_FILE = None
    settings.Settings.DATABASE_FILE = None
    settings.Settings.CHECKPOINT_FILE = None


# 测试目的：验证插入多条日志后，LogStore.query 按 offset/limit 正确切片，total 为过滤后总数。
# 可能发现的缺陷：offset 切片错误、total 计算错误（如未排除 limit 影响）。
def test_logstore_query_paginates_offset_limit(log_store: LogStore) -> None:
    entries = _seed_entries(10)
    log_store.insert_many(entries)

    entries_page, total = log_store.query(LogQuery(offset=0, limit=3, order="asc"))
    assert total == 10
    assert len(entries_page) == 3
    # 升序：第一条 msg 应为 msg-0
    assert entries_page[0].msg == "msg-0"

    entries_page2, total2 = log_store.query(LogQuery(offset=3, limit=3, order="asc"))
    assert total2 == 10
    assert [e.msg for e in entries_page2] == ["msg-3", "msg-4", "msg-5"]


# 测试目的：验证末页 has_more 语义依赖 service；storage 层返回 total 不受 limit 影响（末页切片数量 < limit）。
# 可能发现的缺陷：total 统计被 limit 截断。
def test_logstore_query_total_ignores_limit(log_store: LogStore) -> None:
    entries = _seed_entries(7)
    log_store.insert_many(entries)
    page, total = log_store.query(LogQuery(offset=5, limit=10, order="asc"))
    # 仅剩 2 条（msg-5, msg-6）
    assert total == 7
    assert len(page) == 2
    assert [e.msg for e in page] == ["msg-5", "msg-6"]


# 测试目的：验证 offset 超出总数时返回空 entries 但 total 正确。可能发现的缺陷：offset 越界导致异常或 total 错误。
def test_logstore_query_offset_beyond_total_returns_empty(log_store: LogStore) -> None:
    entries = _seed_entries(4)
    log_store.insert_many(entries)
    page, total = log_store.query(LogQuery(offset=100, limit=10, order="asc"))
    assert total == 4
    assert page == []


# 测试目的：验证 limit 超过总数时返回全部且 total 正确（边界：limit 大于数据量）。
# 可能发现的缺陷：limit 超过总数时切片异常或 total 计算错误。
def test_logstore_query_limit_exceeds_total(log_store: LogStore) -> None:
    entries = _seed_entries(5)
    log_store.insert_many(entries)
    page, total = log_store.query(LogQuery(offset=0, limit=100, order="asc"))
    assert total == 5
    assert len(page) == 5


# 测试目的：验证按 trace_id 过滤后 total 仅为该 trace 的数量（过滤生效）。
# 可能发现的缺陷：过滤未生效，total 仍为全表总数。
def test_logstore_query_filters_by_trace_id(log_store: LogStore) -> None:
    log_store.insert_many(_seed_entries(4, trace_id="trace-a"))
    log_store.insert_many(_seed_entries(3, trace_id="trace-b"))  # 序号会重复但 trace 不同
    page, total = log_store.query(LogQuery(trace_id="trace-a", offset=0, limit=50, order="asc"))
    assert total == 4
    assert all(e.trace_id == "trace-a" for e in page)


# 测试目的：验证空库查询返回 total=0 与空 entries。可能发现的缺陷：空库返回 None 或异常。
def test_logstore_query_empty_store(log_store: LogStore) -> None:
    page, total = log_store.query(LogQuery(offset=0, limit=10, order="asc"))
    assert total == 0
    assert page == []


# 测试目的：验证非法 offset（<0）在 query 入口即抛 ValueError。可能发现的缺陷：校验未在执行前触发。
def test_logstore_query_rejects_negative_offset(log_store: LogStore) -> None:
    with pytest.raises(ValueError):
        log_store.query(LogQuery(offset=-1, limit=10))


# 测试目的：验证 keyword 对 msg 字段做子串匹配过滤。可能发现的缺陷：keyword 未生效或匹配范围错误。
def test_logstore_query_filters_by_keyword(log_store: LogStore) -> None:
    log_store.insert_many(_seed_entries(5))  # msg-0 .. msg-4
    page, total = log_store.query(LogQuery(keyword="msg-1", offset=0, limit=50, order="asc"))
    assert total == 1
    assert len(page) == 1
    assert page[0].msg == "msg-1"


# 测试目的：验证 keyword 仅匹配 msg，不误匹配 caller/data。可能发现的缺陷：匹配范围扩大到其他列。
def test_logstore_query_keyword_only_matches_msg(log_store: LogStore) -> None:
    log_store.insert_many(_seed_entries(5))
    _, total = log_store.query(LogQuery(keyword="caller.1", offset=0, limit=50, order="asc"))
    assert total == 0


# 测试目的：验证 keyword 含 LIKE 通配符时按字面匹配。可能发现的缺陷：未转义导致 % 被当通配符误匹配。
def test_logstore_query_keyword_escapes_like_wildcards(log_store: LogStore) -> None:
    log_store.insert_many(_seed_entries(2))  # msg-0, msg-1 均不含字面 %
    _, total = log_store.query(LogQuery(keyword="%", offset=0, limit=50, order="asc"))
    assert total == 0


# 测试目的：验证 count_by_level 忽略 level 仍受 trace_id 约束。可能发现的缺陷：level 或 trace_id 失效。
def test_logstore_count_by_level_ignores_level_keeps_trace(log_store: LogStore) -> None:
    log_store.insert_many(
        [
            _make_entry("2026-08-13T01:00:00.000Z", "INFO", "t1", "e", 0),
            _make_entry("2026-08-13T01:01:00.000Z", "ERROR", "t1", "e", 1),
            _make_entry("2026-08-13T01:02:00.000Z", "INFO", "t2", "e", 2),
        ],
    )
    counts = log_store.count_by_level(LogQuery(trace_id="t1", level="ERROR", order="asc"))
    assert counts == {"INFO": 1, "ERROR": 1}


# 测试目的：验证 count_by_level 受 keyword 约束。可能发现的缺陷：level_counts 未随 keyword 收窄。
def test_logstore_count_by_level_respects_keyword(log_store: LogStore) -> None:
    log_store.insert_many(
        [
            _make_entry("2026-08-13T01:00:00.000Z", "INFO", "t1", "e", 0),
            _make_entry("2026-08-13T01:01:00.000Z", "WARNING", "t1", "e", 1),
        ],
    )
    counts = log_store.count_by_level(LogQuery(keyword="msg-1", order="asc"))
    assert counts == {"WARNING": 1}


# 测试目的：验证 keyword 含 LIKE 通配符 _ / \ 时按字面匹配（真实 DB），不误匹配全量。
# 可能发现的缺陷：escape 未覆盖 _ 或 \，导致 msg-1 的 "1" 被 _ 通配、或 \ 破坏模式。
def test_logstore_query_keyword_underscore_backslash_literal(log_store: LogStore) -> None:
    # msg-0 ~ msg-2 均不含字面下划线或反斜杠
    log_store.insert_many(_seed_entries(3))
    # 含字面下划线的关键词应匹配 0 条（不会被当通配符匹配任意单字符）
    _, total_und = log_store.query(LogQuery(keyword="_", offset=0, limit=50, order="asc"))
    assert total_und == 0
    # 含字面反斜杠的关键词应匹配 0 条
    _, total_bs = log_store.query(LogQuery(keyword="\\", offset=0, limit=50, order="asc"))
    assert total_bs == 0


# 测试目的：验证 keyword 含 LIKE 通配符时 count_by_level 仍按字面约束（不误统计全量）。
# 可能发现的缺陷：level_counts 内部的 keyword 过滤未转义，导致统计到全部记录。
def test_logstore_count_by_level_keyword_with_wildcard_escaped(log_store: LogStore) -> None:
    log_store.insert_many(
        [
            _make_entry("2026-08-13T01:00:00.000Z", "INFO", "t1", "e", 0),  # msg-0
            _make_entry("2026-08-13T01:01:00.000Z", "ERROR", "t1", "e", 1),  # msg-1
        ],
    )
    # 字面 "%" 在 msg 中不存在 -> 两级别计数都应被收窄为 0
    counts = log_store.count_by_level(LogQuery(keyword="%", order="asc"))
    assert counts == {}


# ---------------------------------------------------------------------------
# service 层 has_more 计算
# ---------------------------------------------------------------------------

class _FakeStore:
    """可控的假 store：按 query 的 offset/limit 切片内部数据集并返回 (entries, total)。"""

    def __init__(self, total_entries: list[LogEntryRecord]) -> None:
        self._all = total_entries

    def query(self, query: LogQuery) -> tuple[list[LogEntryRecord], int]:
        slice_ = self._all[query.offset : query.offset + query.limit]
        return slice_, len(self._all)

    def count_by_level(self, query: LogQuery) -> dict[str, int]:
        """按级别统计假数据集的计数（service._query 现在会调用此方法）。"""
        counts: dict[str, int] = {}
        for entry in self._all:
            counts[entry.level] = counts.get(entry.level, 0) + 1
        return counts


def _make_service_with_store(store: Any, monkeypatch: Any) -> LogQueryService:
    """通过 monkeypatch 注入假 store，构造 LogQueryService（绕过 storage 初始化依赖）。"""
    import app.service.depends as depends

    monkeypatch.setattr(depends, "get_log_store", lambda: store)
    return LogQueryService(max_limit=1000)


# 测试目的：验证 service._query 计算 has_more = offset + len(entries) < total（非末页为 True）。
# 可能发现的缺陷：has_more 计算符号错误（如用了 <= 或 >=）。
def test_service_has_more_true_on_non_last_page(monkeypatch: Any) -> None:
    all_entries = _seed_entries(10)
    service = _make_service_with_store(_FakeStore(all_entries), monkeypatch)

    result = service._query(LogQuery(offset=0, limit=3, order="asc"))
    assert result.total == 10
    assert result.has_more is True
    assert len(result.entries) == 3


# 测试目的：验证末页 has_more=False（offset+len == total）。可能发现的缺陷：边界处 has_more 误判为 True。
def test_service_has_more_false_on_last_page(monkeypatch: Any) -> None:
    all_entries = _seed_entries(10)
    service = _make_service_with_store(_FakeStore(all_entries), monkeypatch)

    # 第二页：offset=3, limit=3 -> 取 3..5，剩余 6..9 在第三页，has_more 应为 True
    result_mid = service._query(LogQuery(offset=3, limit=3, order="asc"))
    assert result_mid.has_more is True

    # 最后一页：offset=9, limit=3 -> 取 1 条，offset+len=10 == total -> has_more False
    result_last = service._query(LogQuery(offset=9, limit=3, order="asc"))
    assert result_last.has_more is False
    assert len(result_last.entries) == 1


# 测试目的：验证 limit 超过总数时 has_more=False（边界）。可能发现的缺陷：limit 过大时 has_more 误判。
def test_service_has_more_false_when_limit_exceeds_total(monkeypatch: Any) -> None:
    all_entries = _seed_entries(5)
    service = _make_service_with_store(_FakeStore(all_entries), monkeypatch)

    result = service._query(LogQuery(offset=0, limit=100, order="asc"))
    assert result.total == 5
    assert result.has_more is False
    assert len(result.entries) == 5


# 测试目的：验证 offset 超出总数时返回空 entries、total 正确、has_more=False。
# 可能发现的缺陷：offset 越界时 has_more 误判为 True 或计算异常。
def test_service_offset_beyond_total_returns_empty_has_more_false(monkeypatch: Any) -> None:
    all_entries = _seed_entries(4)
    service = _make_service_with_store(_FakeStore(all_entries), monkeypatch)

    result = service._query(LogQuery(offset=100, limit=10, order="asc"))
    assert result.total == 4
    assert result.entries == []
    assert result.has_more is False


# 测试目的：验证 service.query_by_trace 透传 offset 并正确计算 has_more。
# 可能发现的缺陷：offset 参数未透传，或 has_more 计算依赖于错误分量。
def test_service_query_by_trace_passes_offset(monkeypatch: Any) -> None:
    all_entries = _seed_entries(10)
    captured: dict[str, Any] = {}

    class _CaptureStore:
        def query(self, query: LogQuery) -> tuple[list[LogEntryRecord], int]:
            captured["query"] = query
            slice_ = all_entries[query.offset : query.offset + query.limit]
            return slice_, len(all_entries)

        def count_by_level(self, query: LogQuery) -> dict[str, int]:
            return {}

    service = _make_service_with_store(_CaptureStore(), monkeypatch)
    result = service.query_by_trace(trace_id="trace-a", limit=3, offset=6)
    assert captured["query"].offset == 6
    assert result.total == 10
    # offset 6 + 3 = 9 < 10 -> 还有下一页
    assert result.has_more is True
    assert [e.msg for e in result.entries] == ["msg-6", "msg-7", "msg-8"]


# 测试目的：验证 service.recent 透传 offset 并正确计算 has_more（降序）。
# 可能发现的缺陷：offset 参数未透传或降序切片计算错误。
def test_service_recent_passes_offset(monkeypatch: Any) -> None:
    all_entries = _seed_entries(10)
    captured: dict[str, Any] = {}

    class _CaptureStore:
        def query(self, query: LogQuery) -> tuple[list[LogEntryRecord], int]:
            captured["query"] = query
            slice_ = all_entries[query.offset : query.offset + query.limit]
            return slice_, len(all_entries)

        def count_by_level(self, query: LogQuery) -> dict[str, int]:
            return {}

    service = _make_service_with_store(_CaptureStore(), monkeypatch)
    result = service.recent(limit=4, offset=0)
    assert captured["query"].offset == 0
    assert captured["query"].order == "desc"
    assert result.total == 10
    assert result.has_more is True
    assert len(result.entries) == 4
