import json
import logging
from datetime import datetime

from app.config.logging.record_mapper import map_log_record
from app.config.logging.handler.jsonl import JsonlLogLine


class JsonlFormatter(logging.Formatter):
    """将 Python LogRecord 格式化为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录。

        参数:
            record: Python logging 传入的记录。

        返回:
            单行 JSON 字符串。

        异常:
            TypeError: 如果日志属性无法序列化且无法字符串化。

        副作用:
            调用父类格式化异常文本。
        """
        mapped = map_log_record(record)
        line = JsonlLogLine(
            ts=datetime.fromisoformat(mapped.ts.replace("Z", "+00:00")),
            level=mapped.level,
            logger=mapped.logger,
            trace_id=mapped.trace_id,
            caller=mapped.caller,
            event=mapped.event,
            msg=mapped.msg,
            data=mapped.data,
            error=mapped.error,
            truncated=mapped.truncated,
        )
        return json.dumps(line.to_dict(), ensure_ascii=False, sort_keys=True)
