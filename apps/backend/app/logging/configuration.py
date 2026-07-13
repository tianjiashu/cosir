"""配置后端文件日志。"""

import logging
from pathlib import Path


def configure_logging(log_file: Path) -> logging.Logger:
    """配置并返回后端应用日志器。

    参数:
        log_file: 后端日志应写入的文件路径。

    返回:
        名为 ``coding_agent.backend`` 且已挂载文件处理器的日志器。

    异常:
        OSError: 当操作系统无法创建日志目录或日志文件时抛出。

    副作用:
        创建日志目录并配置一个进程内的日志处理器。
    """

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("coding_agent.backend")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename) == log_file
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
        logger.addHandler(handler)

    return logger
