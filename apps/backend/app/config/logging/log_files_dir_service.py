"""日期日志文件路径解析。"""

from datetime import date, datetime, timedelta
from pathlib import Path

LOG_FILE_PREFIX = "logs-"
LOG_FILE_SUFFIX = ".log"
RECENT_LOG_SCAN_DAYS = 7


def dated_log_file(log_dir: Path, log_date: date) -> Path:
    """返回指定日期对应的日志文件路径。

    参数:
        log_dir: 日志目录。
        log_date: 需要解析的日期。

    返回:
        ``logs-YYYY-MM-DD.log`` 形式的日志文件路径。

    异常:
        无。

    副作用:
        无，不创建目录或文件。
    """

    return log_dir / f"{LOG_FILE_PREFIX}{log_date.isoformat()}{LOG_FILE_SUFFIX}"


def current_log_file(log_dir: Path) -> Path:
    """返回当前本地日期对应的日志文件路径。

    参数:
        log_dir: 日志目录。

    返回:
        当前日期的 ``logs-YYYY-MM-DD.log`` 路径。

    异常:
        无。

    副作用:
        读取系统本地日期，不创建目录或文件。
    """

    return dated_log_file(log_dir, date.today())


def parse_log_date(path: Path) -> date | None:
    """从日志文件名解析日期。

    参数:
        path: 待解析的文件路径。

    返回:
        文件名符合 ``logs-YYYY-MM-DD.log`` 时返回日期，否则返回 None。

    异常:
        无。非法日期会被视为无法解析。

    副作用:
        无。
    """

    name = path.name
    if not name.startswith(LOG_FILE_PREFIX) or not name.endswith(LOG_FILE_SUFFIX):
        return None
    raw_date = name[len(LOG_FILE_PREFIX) : -len(LOG_FILE_SUFFIX)]
    try:
        return date.fromisoformat(raw_date)
    except ValueError:
        return None


def list_log_files(
    log_dir: Path,
    log_date: date | None = None,
    start_time: str = "",
    end_time: str = "",
    max_days: int = RECENT_LOG_SCAN_DAYS,
) -> list[Path]:
    """根据日期或时间范围返回需要查询的日志文件列表。

    参数:
        log_dir: 日志目录。
        log_date: 显式指定日期；传入后只返回该日期文件。
        start_time: 可选 ISO 起始时间，用于推导日期范围。
        end_time: 可选 ISO 结束时间，用于推导日期范围。
        max_days: 未给日期或时间范围时扫描的最近天数。

    返回:
        按日期升序排列的日志文件路径列表；不存在的文件会被过滤。

    异常:
        ValueError: 如果 ``max_days`` 小于 1 或时间范围非法。

    副作用:
        读取日志目录中文件是否存在。
    """

    if max_days < 1:
        raise ValueError("max_days must be greater than zero")
    candidates = _candidate_files(log_dir, log_date, start_time, end_time, max_days)
    return [path for path in candidates if path.exists()]


def _candidate_files(
    log_dir: Path,
    log_date: date | None,
    start_time: str,
    end_time: str,
    max_days: int,
) -> list[Path]:
    """构造候选日志文件列表。

    参数:
        log_dir: 日志目录。
        log_date: 显式指定日期。
        start_time: ISO 起始时间。
        end_time: ISO 结束时间。
        max_days: 默认最近扫描天数。

    返回:
        可能存在的日期日志文件路径列表。

    异常:
        ValueError: 如果时间范围非法。

    副作用:
        读取系统本地日期。
    """

    if log_date is not None:
        return [dated_log_file(log_dir, log_date)]
    start_date = _local_date_from_iso_text(start_time)
    end_date = _local_date_from_iso_text(end_time)
    if start_date is not None or end_date is not None:
        start = start_date or end_date
        end = end_date or start_date
        if start is None or end is None:
            return []
        if start > end:
            raise ValueError("start_time must not be after end_time")
        return [dated_log_file(log_dir, item) for item in _date_range(start, end)]
    today = date.today()
    start = today - timedelta(days=max_days - 1)
    return [dated_log_file(log_dir, item) for item in _date_range(start, today)]


def _local_date_from_iso_text(value: str) -> date | None:
    """从可选 ISO 时间文本提取本地日期。

    参数:
        value: ISO 日期或日期时间文本。

    返回:
        非空合法文本对应的本地运行环境日期；空字符串返回 None。

    异常:
        ValueError: 如果非空文本不是合法 ISO 日期或日期时间。

    副作用:
        无。
    """

    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone().date()


def _date_range(start: date, end: date) -> list[date]:
    """返回闭区间日期列表。

    参数:
        start: 起始日期。
        end: 结束日期。

    返回:
        从 start 到 end 的所有日期。

    异常:
        无。调用方负责保证 start 不晚于 end。

    副作用:
        无。
    """

    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]
