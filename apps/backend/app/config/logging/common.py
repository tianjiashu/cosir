"""日期日志文件路径解析。"""

from datetime import date
from pathlib import Path

LOG_FILE_PREFIX = "logs-"
LOG_FILE_SUFFIX = ".log"


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
    return log_dir / f"{LOG_FILE_PREFIX}{date.today().isoformat()}{LOG_FILE_SUFFIX}"
