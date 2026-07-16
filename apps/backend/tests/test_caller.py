"""caller 字段计算测试。"""

import logging
import unittest

from app.config.logging.caller import CallerFilter, compute_caller


class _CallerCapture(logging.Handler):
    """记录 emit 的 LogRecord 用于断言 caller 字段。"""

    def __init__(self) -> None:
        """初始化记录列表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """保存日志记录。

        参数:
            record: 待保存的日志记录。

        返回:
            无。

        异常:
            无。

        副作用:
            追加到 records。
        """
        self.records.append(record)


class _Sample:
    """用于验证类名解析的样例类。"""

    def do_work(self, logger: logging.Logger) -> None:
        """在实例方法内打日志。

        参数:
            logger: 测试 logger。

        返回:
            无。

        异常:
            无。

        副作用:
            写入一条日志。
        """
        logger.info("sample_event")


def _module_level_work(logger: logging.Logger) -> None:
    """模块级函数内打日志。

    参数:
        logger: 测试 logger。

    返回:
        无。

    异常:
        无。

    副作用:
        写入一条日志。
    """
    logger.info("module_event")


class CallerFilterTests(unittest.TestCase):
    """校验 caller 字段计算。"""

    def test_caller_includes_class_name_for_method(self) -> None:
        """校验实例方法日志包含 类名.方法。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 caller 不含类名。

        副作用:
            无。
        """
        handler = _CallerCapture()
        handler.addFilter(CallerFilter())
        logger = logging.getLogger("caller-test-class")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        try:
            _Sample().do_work(logger)
            record = handler.records[0]
            self.assertIn("tests.test_caller:_Sample.do_work:", record.caller)
        finally:
            logger.removeHandler(handler)

    def test_caller_module_level_function_has_no_class(self) -> None:
        """校验模块级函数日志不含类名。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 caller 错误地包含类名。

        副作用:
            无。
        """
        handler = _CallerCapture()
        handler.addFilter(CallerFilter())
        logger = logging.getLogger("caller-test-module")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers = [handler]
        try:
            _module_level_work(logger)
            record = handler.records[0]
            self.assertIn("tests.test_caller:_module_level_work:", record.caller)
            self.assertNotIn("None.", record.caller)
        finally:
            logger.removeHandler(handler)

    def test_compute_caller_falls_back_for_unknown_path(self) -> None:
        """校验未知路径时退化为原始路径信息。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果兜底格式异常。

        副作用:
            无。
        """
        record = logging.LogRecord(
            name="x",
            level=logging.INFO,
            pathname="/unknown/path.py",
            lineno=42,
            msg="e",
            args=(),
            exc_info=None,
        )
        result = compute_caller(record)
        self.assertIn("/unknown/path.py", result)
        self.assertIn(":42", result)


if __name__ == "__main__":
    unittest.main()
