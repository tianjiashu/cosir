"""后端逻辑日志文件路径解析。"""

from pathlib import Path

LOG_FILE_NAME = "backend.log"


def current_log_file(log_dir: Path) -> Path:
    """返回后端日志的逻辑文件路径。

    参数:
        log_dir: 日志目录。

    返回:
        ``backend.log`` 逻辑路径。实际日期和大小分片由文件 handler 生成。

    异常:
        无。

    副作用:
        不创建目录或文件。
    """
    return log_dir / LOG_FILE_NAME
